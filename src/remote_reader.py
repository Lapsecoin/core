"""Reads for the light client, taken from another node over HTTP.

RemoteReader answers the same interface LocalReader does (see
wallet_ui.py), so the wallet pages are the same code in both programs.
The difference is the cost model: a light client may be on a very small
data allowance, so nothing here asks twice for what it already has.

  - Answers are kept for `refresh` seconds, so a page that polls every few
    seconds costs one request per window however many times it asks.
  - The board is fetched with If-None-Match, so unchanged costs a 304 and
    no body.
  - Requests ask for gzip, and the endpoints they call send compact data.
  - Bytes used are counted, and shown, so the cost is never a surprise.

The remote node is trusted for data only. It never sees a secret: signing
happens here, and a node that lies can show a wrong balance or hide a
transaction, but cannot spend anything.

Light-safe: imports nothing that pulls in the VDF, the chain database, or
the swap code, and a test keeps it that way.
"""

import json
import logging
import os
import random
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from version import LOCAL_VERSION

log = logging.getLogger("ec.remote")

DEFAULT_SEEDS = ["https://lapsenode.vicnas.me"]

COOLDOWN_SECONDS     = 60
COOLDOWN_MAX_SECONDS = 600
MAX_NODES            = 30
DISCOVER_INTERVAL    = 6 * 3600


class RemoteError(Exception):
    """No node could answer. The message says why, in words fit to show."""


# Where Tor listens: the daemon, then Tor Browser.
TOR_PORTS = (9050, 9150)


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def resolve_proxy(proxy):
    """The proxy URL to give requests, or None for none.

    "tor" means Tor's local SOCKS port, whichever of the two is up. A
    socks5 URL is made socks5h, so names are resolved by the proxy and not
    by this machine, which would announce every node asked for to whoever
    runs the resolver. And a socks proxy with no login of its own is given
    a random one: Tor reads a different login as a request for a circuit of
    its own, so this client does not share one with the rest of what the
    machine sends through Tor, where the two could be told to be one user.
    """
    if not proxy:
        return None
    if proxy.strip().lower() == "tor":
        port = next((p for p in TOR_PORTS if _listening(p)), None)
        if port is None:
            raise RemoteError("--proxy tor: nothing is listening on 127.0.0.1:9050 "
                              "or 9150. Start Tor, or Tor Browser, first.")
        proxy = f"socks5h://127.0.0.1:{port}"
    parts = urlsplit(proxy)
    if parts.scheme == "socks5":
        parts = parts._replace(scheme="socks5h")
    if parts.scheme.startswith("socks5") and "@" not in parts.netloc:
        parts = parts._replace(netloc=f"lapse{secrets.token_hex(8)}:x@{parts.netloc}")
    return urlunsplit(parts)


def _norm(url):
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


class NodeSet:
    """The nodes to ask, and which to ask next.

    Seeds (what the user named, or the built-in defaults) are preferred to
    anything discovered, and within a tier the choice is random. Only
    nodes that answer HTTP are ever here, discovery lists no others. The
    current node is kept for as long as it works, so one session talks to
    one node and its answers stay consistent with each other; a failure
    strikes it and moves on, and a struck node sits out a growing
    cooldown before it is tried again.
    """

    def __init__(self, seeds):
        self._lock = threading.Lock()
        self._tier = {}                  # url -> 0 seed, 1 discovered
        self._strikes = {}               # url -> consecutive failures
        self._until = {}                 # url -> monotonic time it may be tried again
        self._current = None
        for s in seeds:
            self._tier[_norm(s)] = 0

    def add(self, urls):
        with self._lock:
            for u in urls:
                u = _norm(u)
                if u not in self._tier and len(self._tier) < MAX_NODES:
                    self._tier[u] = 1

    def pick(self):
        with self._lock:
            now = time.monotonic()
            if self._current and self._until.get(self._current, 0) <= now:
                return self._current
            ready = [u for u in self._tier if self._until.get(u, 0) <= now]
            if not ready:
                return None
            best = min(self._tier[u] for u in ready)
            self._current = random.choice([u for u in ready if self._tier[u] == best])
            return self._current

    def ok(self, url):
        with self._lock:
            self._strikes.pop(url, None)
            self._until.pop(url, None)

    def strike(self, url):
        with self._lock:
            n = self._strikes.get(url, 0) + 1
            self._strikes[url] = n
            self._until[url] = time.monotonic() + min(
                COOLDOWN_SECONDS * n, COOLDOWN_MAX_SECONDS)
            if self._current == url:
                self._current = None

    def all(self):
        with self._lock:
            return list(self._tier)

    def current(self):
        with self._lock:
            return self._current


class RemoteReader:

    def __init__(self, seeds=None, *, proxy=None, refresh=15, timeout=(5, 20),
                 cache_file=None, session=None):
        self.nodes = NodeSet(seeds or DEFAULT_SEEDS)
        self.refresh = refresh
        self.timeout = timeout
        self.cache_file = cache_file
        self._http = session or requests.Session()
        self._http.headers["User-Agent"] = f"lapsecoin-dumb/{LOCAL_VERSION}"
        self.proxy = resolve_proxy(proxy)
        if self.proxy:
            self._http.proxies = {"http": self.proxy, "https": self.proxy}
        self._lock = threading.Lock()
        self._cache = {}                 # key -> (fetched_at, value)
        self._board = {}                 # chunks -> {"etag", "data", "at"}
        self._discovered_at = 0.0
        self.bytes_in = 0
        self.requests = 0
        self._load_known_nodes()

    # Nodes

    def _load_known_nodes(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file) as f:
                self.nodes.add(json.load(f))
        except (OSError, ValueError):
            pass

    def _discover(self):
        """Learn more nodes from the one in use, at most every few hours:
        only those that answer HTTP, which is all a light client can use."""
        if time.time() - self._discovered_at < DISCOVER_INTERVAL:
            return
        self._discovered_at = time.time()
        try:
            resp = self._request("GET", "/api/peers/http", discover=False)
            self.nodes.add(["http://" + a for a in resp.json().get("nodes", [])])
        except (RemoteError, ValueError, AttributeError):
            return
        if self.cache_file:
            try:
                with open(self.cache_file, "w") as f:
                    json.dump(self.nodes.all(), f)
            except OSError:
                pass

    # Transport

    def _request(self, method, path, *, params=None, body=None, headers=None,
                 discover=True):
        if discover:
            self._discover()
        last = "no node to ask"
        for _ in range(len(self.nodes.all()) + 1):
            base = self.nodes.pick()
            if base is None:
                break
            try:
                resp = self._http.request(method, base + path, params=params,
                                          json=body, headers=headers,
                                          timeout=self.timeout)
            except requests.RequestException as e:
                last = f"{base}: {e.__class__.__name__}"
                self.nodes.strike(base)
                continue
            self.requests += 1
            self.bytes_in += int(resp.headers.get("Content-Length") or len(resp.content))
            if resp.status_code == 429:
                raise RemoteError("The node is rate limiting this client; wait a moment.")
            if resp.status_code >= 500:
                last = f"{base}: HTTP {resp.status_code}"
                self.nodes.strike(base)
                continue
            self.nodes.ok(base)
            return resp
        raise RemoteError(f"No node could be reached ({last}).")

    def _get_json(self, path, params=None):
        resp = self._request("GET", path, params=params)
        if resp.status_code != 200:
            raise RemoteError(f"The node answered {resp.status_code} for {path}.")
        try:
            return resp.json()
        except ValueError:
            raise RemoteError(f"The node sent something that is not JSON for {path}.")

    def _cached(self, key, fetch, fresh=False):
        with self._lock:
            hit = self._cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < self.refresh:
            return hit[1]
        try:
            value = fetch()
        except RemoteError:
            if hit:                      # offline: stale beats nothing
                return hit[1]
            raise
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def invalidate(self):
        """Forget everything held. After a submit, what the node says has
        changed (the mempool, the nonce), and it should be asked again."""
        with self._lock:
            self._cache.clear()
            self._board.clear()

    # The reader interface (see wallet_ui)

    def fee_estimate(self):
        return self._cached(("fees",), lambda: self._get_json("/api/fees"))

    def account(self, addr=None, *, nick=None, fees=False, fresh=False):
        params = {}
        if addr:
            params["addr"] = addr
        if nick:
            params["nick"] = nick
        if fees:
            params["fees"] = 1
        key = ("state", tuple(sorted(params.items())))
        return self._cached(key, lambda: self._get_json("/api/state", params),
                            fresh=fresh)

    def submit(self, tx_dict):
        resp = self._request("POST", "/api/tx/send", body=tx_dict)
        self.invalidate()
        try:
            data = resp.json()
        except ValueError:
            return False, f"The node answered {resp.status_code}."
        if data.get("ok"):
            return True, data.get("tx_hash", "")
        return False, data.get("error") or f"The node answered {resp.status_code}."

    def address_page(self, addr, page):
        return self._cached(("addr", addr, page), lambda: self._get_json(
            f"/api/address/{addr}/page", {"page": page}))

    def _board_entry(self, chunks):
        """The board for `chunks`, asking the node only if the held copy is
        older than the refresh window, and then with If-None-Match so an
        unchanged board costs a 304."""
        chunks = max(chunks, 1)
        with self._lock:
            held = self._board.get(chunks)
        if held and time.monotonic() - held["at"] < self.refresh:
            return held
        headers = {"If-None-Match": f'"{held["etag"]}"'} if held else None
        try:
            resp = self._request("GET", "/api/board/page",
                                 params={"chunks": chunks}, headers=headers)
        except RemoteError:
            if held:
                return held
            raise
        if resp.status_code == 304 and held:
            held["at"] = time.monotonic()
            return held
        if resp.status_code != 200:
            if held:
                return held
            raise RemoteError(f"The node answered {resp.status_code} for the board.")
        data = resp.json()
        entry = {"etag": data.get("etag", ""), "data": data, "at": time.monotonic()}
        with self._lock:
            self._board[chunks] = entry
        return entry

    def profile(self, addr):
        """addr's board icon and nickname, as far as the board already
        fetched says. It never asks the node: asking is how a node learns
        whose address this is, and a viewer's own icon is not worth that.
        None when the address has no post on what was fetched."""
        with self._lock:
            held = [e["data"] for e in self._board.values()]
        for data in held:
            if addr in data.get("profiles", {}):
                return data["profiles"][addr]
        return None

    def board_etag(self, chunks):
        return self._board_entry(chunks)["etag"]

    def board_page(self, chunks):
        return self._board_entry(chunks)["data"]

    # For display

    def usage(self):
        """What this session has cost so far, and how it is reaching the
        node, for the page to show."""
        node = self.nodes.current()
        if self.proxy:
            route = "via proxy"
        elif node is None:
            route = "direct"
        elif node.startswith("https://"):
            route = "direct, encrypted"
        else:
            route = "direct, not encrypted"
        return {"bytes": self.bytes_in, "requests": self.requests,
                "node": node, "route": route}


def default_cache_file(directory="."):
    return os.path.join(directory, "lapsecoin_light_nodes.json")
