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

Who learns the wallet's address. Reading the board, the fee market and the
node list names nobody. Looking up a balance or a nonce, or submitting a
transaction, names the wallet's address to whoever answers, and that node
also sees the IP that asked. Those requests take the most private route
available, and none is ever refused for lack of one:

  1. Through Tor, if Tor is running here. Used when it is found, dropped
     when it is not, with nothing to configure.
  2. Through a relay (oblivious.py): sealed to one node and handed over by
     another, so the one that reads the request does not see the IP, and the
     one that sees the IP cannot read the request. Needs two nodes that
     advertise a key; a transaction is then, from the network's side, in
     the same position as one a node submits for itself, whose origin
     Dandelion already hides.
  3. Directly, to the node in use, preferring one reached over https.

The page header says which was used.

Light-safe: imports nothing that pulls in the VDF, the chain database, or
the node, and a test keeps it that way.
"""

import base64
import ipaddress
import json
import logging
import os
import random
import secrets
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

import oblivious
from public_url import parse_public_url
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
TOR_RECHECK_SECONDS = 30      # how often to look again for Tor
TOR_BAD_SECONDS = 60          # how long to do without it after it fails us
RELAY_ATTEMPTS = 3


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def resolve_proxy(proxy):
    """The proxy URL to give requests for one the user named, or None.

    A socks5 URL is made socks5h, so names are resolved by the proxy and not
    by this machine, which would announce every node asked for to whoever
    runs the resolver. And a socks proxy with no login of its own is given
    a random one: Tor reads a different login as a request for a circuit of
    its own, so this client does not share one with the rest of what the
    machine sends through Tor, where the two could be told to be one user.
    """
    if not proxy:
        return None
    parts = urlsplit(proxy)
    if parts.scheme == "socks5":
        parts = parts._replace(scheme="socks5h")
    if parts.scheme.startswith("socks5") and "@" not in parts.netloc:
        parts = parts._replace(netloc=f"lapse{secrets.token_hex(8)}:x@{parts.netloc}")
    return urlunsplit(parts)


def _subnet(host):
    """The /16 (IPv4) or /32 (IPv6) a host address falls in, or None if it is
    a name or not an address. Two nodes in one are likely one operator."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    return ipaddress.ip_network(f"{ip}/{16 if ip.version == 4 else 32}", strict=False)


class _Reply:
    """A relayed answer, shaped like the response the callers expect."""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.content = b""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _norm(url):
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


class NodeSet:
    """The nodes to ask, and which to ask next.

    Seeds (what the user named, or the built-in defaults) are preferred to
    anything discovered, and within a tier an encrypted (https) node to a
    plain one, then at random. Nodes found by discovery answer HTTP, or
    advertise an https address of their own. The current node is kept for
    as long as it works, so one session talks to one node and its answers
    stay consistent with each other; a failure strikes it and moves on, and
    a struck node sits out a growing cooldown before it is tried again.

    A request that names the wallet's address prefers a node it is safe
    with, one reached over https or one the user named themselves, and goes
    to any other only when none of those is up.
    """

    def __init__(self, seeds):
        self._lock = threading.Lock()
        self._tier = {}                  # url -> 0 seed, 1 discovered
        self._strikes = {}               # url -> consecutive failures
        self._until = {}                 # url -> monotonic time it may be tried again
        self._current = {False: None, True: None}   # by whether it is safe for an address
        for s in seeds:
            self._tier[_norm(s)] = 0

    def _safe_for_address(self, url):
        return url.startswith("https://") or self._tier[url] == 0

    def add(self, urls):
        with self._lock:
            for u in urls:
                u = _norm(u)
                if u not in self._tier and len(self._tier) < MAX_NODES:
                    self._tier[u] = 1

    def pick(self, for_address=False):
        """The node to ask next, or None if there is none to ask."""
        with self._lock:
            now = time.monotonic()
            healthy = lambda u: u and self._until.get(u, 0) <= now
            # One node for everything where there is one that will do.
            for slot in ((True,) if for_address else (True, False)):
                if healthy(self._current[slot]):
                    return self._current[slot]
            ready = [u for u in self._tier if self._until.get(u, 0) <= now]
            if not ready:
                return None
            rank = lambda u: ((0 if self._safe_for_address(u) else 1) if for_address else 0,
                              self._tier[u], 0 if u.startswith("https://") else 1)
            best = min(rank(u) for u in ready)
            choice = random.choice([u for u in ready if rank(u) == best])
            self._current[self._safe_for_address(choice)] = choice
            return choice

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
            for slot, cur in self._current.items():
                if cur == url:
                    self._current[slot] = None

    def all(self):
        with self._lock:
            return list(self._tier)

    def current(self):
        with self._lock:
            return self._current[False] or self._current[True]


class RemoteReader:

    def __init__(self, seeds=None, *, proxy=None, refresh=15, timeout=(5, 20),
                 cache_file=None, session=None):
        """proxy: None for none, "auto" to use Tor when it is running (and
        stop when it is not), or the URL of a proxy to use always."""
        self.nodes = NodeSet(seeds or DEFAULT_SEEDS)
        self.refresh = refresh
        self.timeout = timeout
        self.cache_file = cache_file
        self._http = session or requests.Session()
        self._http.headers["User-Agent"] = f"lapsecoin-dumb/{LOCAL_VERSION}"
        self._auto_tor = proxy == "auto"
        self.proxy = None if self._auto_tor else resolve_proxy(proxy)
        # One login for the session: Tor gives the client a circuit of its own.
        self._tor_login = f"lapse{secrets.token_hex(8)}:x"
        self._tor_url, self._tor_checked, self._tor_bad_until = None, 0.0, 0.0
        self._lock = threading.Lock()
        self._cache = {}                 # key -> (fetched_at, value)
        self._board = {}                 # chunks -> {"etag", "data", "at"}
        self._discovered_at = 0.0
        self._last_failure = None        # why the last node could not be used
        self._last_base = None           # the node that answered last
        # Nodes that take sealed requests (oblivious.py): relays are reached
        # at a URL, targets are named to a relay as ip:port, each with its key.
        self._relays, self._targets, self._pair = {}, {}, None
        self._bad = {}                   # relay url or target addr -> usable again at
        self._wallet_route = None        # how the last address-bearing request went
        self.bytes_in = 0
        self.requests = 0
        self._load_known_nodes()

    # Nodes

    def _load_known_nodes(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file) as f:
                self.nodes.add(self._acceptable(json.load(f)))
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _plain_url(peer):
        """http://host:port for a peer as peers are known ("ip:port"), or
        None. Only a public IP address qualifies: a list some node handed
        over must not be able to point this client at a machine on its own
        network or at itself."""
        try:
            host, port = peer.rsplit(":", 1)
            ip = ipaddress.ip_address(host.strip("[]"))
            if not ip.is_global or not 0 < int(port) < 65536:
                return None
        except (ValueError, AttributeError):
            return None
        return f"http://[{ip}]:{int(port)}" if ip.version == 6 else f"http://{ip}:{int(port)}"

    @classmethod
    def _acceptable(cls, entries):
        """Of what a file or another node says about nodes, the ones worth
        trying: https addresses that pass public_url.parse_public_url, and
        peers ("ip:port", or an http:// URL of one) at public IP addresses,
        https first. Anything else is dropped, whatever it is."""
        https, plain = [], []
        for e in entries:
            if not isinstance(e, str):
                continue
            if e.startswith("https://"):
                try:
                    https.append(parse_public_url(e))
                except ValueError:
                    pass
            else:
                url = cls._plain_url(e[len("http://"):] if e.startswith("http://") else e)
                if url:
                    plain.append(url)
        return https + plain

    def _learn_keys(self, keys, self_key):
        """Take note of the nodes that advertise a key for sealed requests.
        What a node says about its peers is a claim: a wrong key only makes
        a request to that peer fail, and it is then dropped."""
        if isinstance(keys, dict):
            for addr, key in keys.items():
                url = self._plain_url(addr) if isinstance(addr, str) else None
                try:
                    oblivious.parse_key(key)
                except ValueError:
                    continue
                if url:
                    self._relays[url] = key
                    self._targets[addr] = key
        try:
            if self._last_base and oblivious.parse_key(self_key):
                self._relays[self._last_base] = self_key
        except ValueError:
            pass

    def _discover(self):
        """Learn more nodes from the one in use, at most every few hours:
        those that answer HTTP, the https addresses they advertise, and which
        of them take sealed requests."""
        if time.time() - self._discovered_at < DISCOVER_INTERVAL:
            return
        self._discovered_at = time.time()
        try:
            resp = self._request("GET", "/api/peers/http", discover=False)
            data = resp.json()
            self.nodes.add(self._acceptable(
                list(data.get("https", [])) + list(data.get("nodes", []))))
            self._learn_keys(data.get("keys"), data.get("self_key"))
        except (RemoteError, ValueError, AttributeError, TypeError):
            return
        if self.cache_file:
            try:
                with open(self.cache_file, "w") as f:
                    json.dump(self.nodes.all(), f)
            except OSError:
                pass

    # Tor

    def _tor_proxy(self):
        """The Tor proxy URL if Tor is running here and has not just failed
        us, else None. Looked for again every little while, so starting Tor
        takes effect, and stopping it stops being used, with no setting."""
        now = time.monotonic()
        if now >= self._tor_checked:
            port = next((p for p in TOR_PORTS if _listening(p)), None)
            self._tor_url = f"socks5h://{self._tor_login}@127.0.0.1:{port}" if port else None
            self._tor_checked = now + TOR_RECHECK_SECONDS
        return None if now < self._tor_bad_until else self._tor_url

    def _active_proxy(self):
        return self._tor_proxy() if self._auto_tor else self.proxy

    # Transport

    def _send(self, method, base, path, params, body, headers):
        """One request to one node, through whatever proxy is in force. If
        Tor was found automatically and fails, it is dropped for a while and
        the request goes without it: Tor is a preference, never a condition."""
        proxy = self._active_proxy()
        self._http.proxies = {"http": proxy, "https": proxy} if proxy else {}
        try:
            return self._http.request(method, base + path, params=params, json=body,
                                      headers=headers, timeout=self.timeout)
        except requests.exceptions.ProxyError:
            if not (self._auto_tor and proxy):
                raise
            self._tor_bad_until = time.monotonic() + TOR_BAD_SECONDS
            self._http.proxies = {}
            return self._http.request(method, base + path, params=params, json=body,
                                      headers=headers, timeout=self.timeout)

    def _request(self, method, path, *, params=None, body=None, headers=None,
                 discover=True, names_wallet=False):
        """names_wallet: the request carries the wallet's address, so it
        takes the most private route there is (see the module docstring)."""
        if discover:
            self._discover()
        if names_wallet and not self._active_proxy():
            reply = self._via_relay(method, path, params, body)
            if reply is not None:
                self._wallet_route = "relay"
                return reply
        # Nodes that failed a moment ago sit out a cooldown, so there may be
        # none to ask by now: the reason they failed is still the answer.
        last = self._last_failure or "no node to ask"
        for _ in range(len(self.nodes.all()) + 1):
            base = self.nodes.pick(for_address=names_wallet)
            if base is None:
                break
            try:
                resp = self._send(method, base, path, params, body, headers)
            except requests.RequestException as e:
                last = self._last_failure = f"{base}: {e.__class__.__name__}"
                self.nodes.strike(base)
                continue
            self.requests += 1
            self.bytes_in += int(resp.headers.get("Content-Length") or len(resp.content))
            if resp.status_code == 429:
                raise RemoteError("The node is rate limiting this client; wait a moment.")
            if resp.status_code >= 500:
                last = self._last_failure = f"{base}: HTTP {resp.status_code}"
                self.nodes.strike(base)
                continue
            self.nodes.ok(base)
            self._last_base = base
            if names_wallet:
                self._wallet_route = "direct"
            return resp
        raise RemoteError(f"No node could be reached ({last}).")

    # Relay

    def _usable(self, who, now):
        return self._bad.get(who, 0) <= now

    def _choose_pair(self):
        """(relay, target) to send through: two different nodes, from
        different networks where there is a choice, as likely to be different
        operators as can be told. Kept for as long as it works, so a session
        does not hand its requests to more nodes than it needs to."""
        now = time.monotonic()
        with self._lock:
            if self._pair and all(self._usable(w, now) for w in (self._pair[0][0], self._pair[1][0])):
                return self._pair
            relays = [(u, k) for u, k in self._relays.items() if self._usable(u, now)]
            targets = [(a, k) for a, k in self._targets.items() if self._usable(a, now)]
            # A node is not its own relay.
            pairs = [(r, t) for r in relays for t in targets if r[0] != self._plain_url(t[0])]

            def apart(pair):
                relay_net = _subnet(urlsplit(pair[0][0]).hostname or "")
                return relay_net is None or relay_net != _subnet(pair[1][0].rsplit(":", 1)[0])
            apart = [p for p in pairs if apart(p)]
            choice = apart or pairs
            self._pair = random.choice(choice) if choice else None
            return self._pair

    def _via_relay(self, method, path, params, body):
        """The answer to a request sent sealed to one node through another,
        or None if there is no pair to send through or none worked. A failed
        pair is set aside for a while and the next tried."""
        full = path + ("?" + urlencode(params) if params else "")
        for _ in range(RELAY_ATTEMPTS):
            pair = self._choose_pair()
            if pair is None:
                return None
            (relay_url, _), (target, target_key) = pair
            try:
                blob, one_time = oblivious.seal_request(target_key, method, full, body)
            except ValueError:               # too large to send sealed
                return None
            try:
                resp = self._send("POST", relay_url, "/api/relay", None,
                                  {"to": target, "blob": base64.b64encode(blob).decode()}, None)
                self.requests += 1
                self.bytes_in += len(resp.content)
                if resp.status_code != 200 or not resp.content:
                    raise ValueError("the relay did not pass it on")
                status, answer = oblivious.open_response(one_time, resp.content)
            except (requests.RequestException, ValueError):
                # Either end may be at fault and the client cannot tell
                # which, so neither is used again for a while.
                with self._lock:
                    now = time.monotonic()
                    self._bad[relay_url] = self._bad[target] = now + COOLDOWN_SECONDS
                    self._pair = None
                continue
            if status == 429:
                raise RemoteError("The node is rate limiting this client; wait a moment.")
            return _Reply(status, answer)
        return None

    def _get_json(self, path, params=None, names_wallet=False):
        resp = self._request("GET", path, params=params, names_wallet=names_wallet)
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
        return self._cached(key, lambda: self._get_json("/api/state", params,
                                                        names_wallet=bool(addr)),
                            fresh=fresh)

    def submit(self, tx_dict):
        resp = self._request("POST", "/api/tx/send", body=tx_dict, names_wallet=True)
        self.invalidate()
        try:
            data = resp.json()
        except ValueError:
            return False, f"The node answered {resp.status_code}."
        if isinstance(data, dict) and data.get("ok"):
            return True, data.get("tx_hash", "")
        error = data.get("error") if isinstance(data, dict) else None
        return False, error or f"The node answered {resp.status_code}."

    def address_page(self, addr, page):
        return self._cached(("addr", addr, page), lambda: self._get_json(
            f"/api/address/{addr}/page", {"page": page}, names_wallet=True))

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
        """What this session has cost so far, and how the wallet's address is
        being reached for, so the page can say so."""
        node = self.nodes.current()
        proxy = self._active_proxy()
        if proxy:
            tor = (urlsplit(proxy).hostname in ("127.0.0.1", "localhost")
                   and urlsplit(proxy).port in TOR_PORTS)
            route = "via Tor" if tor else "via proxy"
        elif self._wallet_route == "relay":
            route = "via relay"
        elif node is None:
            route = "direct"
        elif node.startswith("https://"):
            route = "direct, encrypted"
        elif urlsplit(node).hostname in ("127.0.0.1", "localhost", "::1"):
            route = "direct, this machine"
        else:
            route = "direct, not encrypted"
        return {"bytes": self.bytes_in, "requests": self.requests,
                "node": node, "route": route}


def default_cache_file(directory="."):
    return os.path.join(directory, "lapsecoin_light_nodes.json")
