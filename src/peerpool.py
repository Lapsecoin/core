"""Thread-safe peer address store with health tracking.

Pure data structure. No I/O, no threads, no queues. Every module that
touches peers reads from or writes to a PeerPool instance, but they
never call each other.
"""

import ipaddress
import logging
import secrets
import threading
import time

from params import MAX_PEERS

log = logging.getLogger("ec.peerpool")

COOLDOWN_SECONDS     = 60
COOLDOWN_MAX_SECONDS = 300
MAX_STRIKES          = 3
STALE_SECONDS        = 300
HTTP_REACHABLE_TTL   = 600  # how long a cached HTTP-probe result stays trusted

# Diversity cap: reject a new peer once this many already-held peers share
# its /24 (IPv4) or /64 (IPv6). Renting many distinct addresses from one
# contiguous block is cheap for an attacker; this bounds how much of the
# pool one such block can ever occupy, regardless of how many addresses
# it presents. Deliberately subnet-only (no ASN lookup): that would need a
# live external service or a bundled IP-to-ASN database, which this project
# has otherwise avoided in favor of self-contained UDP discovery.
MAX_PEERS_PER_SUBNET = 3


def _subnet_key(addr: str) -> str | None:
    """Return the /24 (IPv4) or /64 (IPv6) network the addr's host falls in,
    or None if the host isn't a parseable IP."""
    try:
        host, _port = addr.rsplit(":", 1)
        ip = ipaddress.ip_address(host)
    except (ValueError, AttributeError):
        return None
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def is_routable_peer_addr(addr: str) -> bool:
    """Reject loopback/private/link-local/multicast hosts. A malicious DHT
    or peer-exchange entry pointing at e.g. 127.0.0.1 or a 10.x address
    would otherwise make this node send UDP probes into its own host or
    internal network on the attacker's behalf."""
    try:
        host, _port = addr.rsplit(":", 1)
        ip = ipaddress.ip_address(host)
    except (ValueError, AttributeError):
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class PeerPool:

    def __init__(self, max_peers=None):
        self._max_peers = max_peers if max_peers is not None else MAX_PEERS
        self._peers     = {}          # addr -> last_seen (wall clock)
        self._fails     = {}          # addr -> {"strikes": int, "cooldown_until": monotonic}
        self._info      = {}          # addr -> {"height": int|None, "version": str}
        # Held peers per /24 or /64, kept in step with _peers so the
        # diversity cap is a lookup rather than a scan. See add().
        self._subnets   = {}          # subnet key -> count
        self._lock      = threading.Lock()

    def _forget(self, addr):
        """Drop addr from every index. Callers hold the lock."""
        if self._peers.pop(addr, None) is None:
            return
        self._info.pop(addr, None)
        subnet = _subnet_key(addr)
        if subnet is not None:
            remaining = self._subnets.get(subnet, 0) - 1
            if remaining > 0:
                self._subnets[subnet] = remaining
            else:
                self._subnets.pop(subnet, None)

    # ---- Core operations ----

    def add(self, addr, allow_private=False):
        """Add a peer. Returns True if it was new.

        allow_private bypasses the private/loopback/link-local rejection.
        Only for addresses a local operator entered deliberately (the
        private dashboard's manual add-peer form), never for anything
        sourced from the DHT, peer-exchange, or another peer."""
        if not allow_private and not is_routable_peer_addr(addr):
            return False
        now_mono = time.monotonic()
        with self._lock:
            if addr in self._peers:
                self._peers[addr] = time.time()
                return False
            if len(self._peers) >= self._max_peers:
                return False
            if now_mono < self._fails.get(addr, {}).get("cooldown_until", 0.0):
                return False
            # Counted, not recomputed. This used to parse every held peer's
            # address and build an ip_network object for it on every add,
            # and add runs on the PING path, so the cost of admitting one
            # peer was proportional to how many were already held.
            subnet = _subnet_key(addr)
            if subnet is not None:
                if self._subnets.get(subnet, 0) >= MAX_PEERS_PER_SUBNET:
                    return False
                self._subnets[subnet] = self._subnets.get(subnet, 0) + 1
            self._peers[addr] = time.time()
        log.debug("[peer] added  addr=%s", addr)
        return True

    def update_info(self, addr, height=None, version=""):
        """Cache a peer's last-known height and version, learned directly
        from a GETINFO/INFO exchange. No-op for an address that isn't a
        currently tracked peer (mirrors touch()'s same guard).

        No wallet. A peer's payout address used to be carried here, which
        made this a directory of IP to wallet and, through /api/peers, a
        public one. Where to pay a node now arrives as a relayed liveness
        note that says nothing about where it came from (see
        Node._handle_inbound_alive)."""
        with self._lock:
            if addr not in self._peers:
                return
            rec = self._info.setdefault(addr, {})
            rec["height"]  = height
            rec["version"] = version or ""

    def set_http_reachable(self, addr, ok, checked_at=None):
        """Record the outcome of an out-of-band HTTP reachability probe
        against addr's web UI (the actual probing happens elsewhere, see
        the module docstring). No-op for an address that isn't currently
        tracked, same guard as update_info."""
        with self._lock:
            if addr not in self._peers:
                return
            self._info.setdefault(addr, {})["http_reachable"] = bool(ok)
            self._info[addr]["http_checked_at"] = (
                checked_at if checked_at is not None else time.time())

    def touch(self, addr):
        """Update last-seen timestamp and clear strikes on successful contact."""
        with self._lock:
            if addr in self._peers:
                self._peers[addr] = time.time()
            self._fails.pop(addr, None)

    def strike(self, addr):
        """Record a failure. Enough strikes cause removal."""
        with self._lock:
            rec     = self._fails.get(addr, {"strikes": 0, "cooldown_until": 0.0})
            strikes = rec["strikes"] + 1
            banned  = strikes >= MAX_STRIKES
            cooldown = COOLDOWN_MAX_SECONDS if banned else min(
                COOLDOWN_SECONDS * (2 ** (strikes - 1)), COOLDOWN_MAX_SECONDS
            )
            self._fails[addr] = {"strikes": strikes,
                                  "cooldown_until": time.monotonic() + cooldown}
            if banned:
                self._forget(addr)
                log.warning("[peer] banned  addr=%s  strikes=%d", addr, strikes)

    def remove(self, addr):
        with self._lock:
            self._forget(addr)

    def evict_stale(self):
        """Remove peers not seen within STALE_SECONDS."""
        cutoff = time.time() - STALE_SECONDS
        with self._lock:
            stale = [p for p, t in self._peers.items() if t < cutoff]
            for p in stale:
                self._forget(p)
            remaining = len(self._peers)
        if stale:
            # Not debug. Losing peers is the thing an operator is trying to
            # explain when their node goes quiet, and at debug level the
            # only visible symptom was the peer count silently falling,
            # with nothing saying it had happened or to whom.
            log.info("[peer] dropped %d peer(s) not heard from in %ds: %s  (pool=%d)",
                     len(stale), int(STALE_SECONDS), ", ".join(stale), remaining)

    # ---- Queries ----

    def get_all(self):
        """Return list of all peer addresses (snapshot)."""
        now_mono = time.monotonic()
        with self._lock:
            return [
                p for p in self._peers
                if now_mono >= self._fails.get(p, {}).get("cooldown_until", 0.0)
            ]

    def random(self):
        """Pick a random peer, or None if empty."""
        peers = self.get_all()
        return secrets.choice(peers) if peers else None

    def count(self):
        with self._lock:
            return len(self._peers)

    def all_addrs(self):
        """Raw list of all addresses (including those on cooldown). For cache/API."""
        with self._lock:
            return list(self._peers.keys())

    def snapshot(self):
        """Return [(addr, last_seen, active, height, version,
        http_reachable)] for display. active is False while a peer
        is in cooldown after repeated failures. height/version are the
        last-known confirmed values from a GETINFO exchange (see
        update_info), or (None, "") if none has completed yet.

        No wallet, by design and no longer by omission. A peer's payout
        address is not this node's business to know or to publish: it
        arrives as a relayed liveness note whose sender is not its author.
        http_reachable is
        True/False from the most recent HTTP probe (see set_http_reachable)
        if one completed within the last HTTP_REACHABLE_TTL seconds,
        otherwise None, stale or never-checked, treated the same as
        "don't know" rather than assumed reachable."""
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            result = []
            for addr, last_seen in self._peers.items():
                info = self._info.get(addr, {})
                checked_at = info.get("http_checked_at")
                http_reachable = (
                    info.get("http_reachable")
                    if checked_at is not None
                    and now_wall - checked_at <= HTTP_REACHABLE_TTL
                    else None
                )
                result.append((
                    addr, last_seen,
                    now_mono >= self._fails.get(addr, {}).get("cooldown_until", 0.0),
                    info.get("height"),
                    info.get("version", ""),
                    http_reachable,
                ))
            return result
