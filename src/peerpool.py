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

from params import MAX_PEERS, PEERS_PER_MESSAGE_LIMIT

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
        # addr -> the peer whose PEERS message told us about it, or None
        # (DHT, torrent swarm, or the operator's --peer flag). Real
        # provenance, kept only for display (see snapshot()); nothing here
        # decides admission or trust on the strength of who vouched for it.
        self._learned_from = {}
        # introducer addr -> the most recent full list it claimed, even
        # for addresses this node never admitted itself (couldn't reach,
        # or hasn't tried yet). Wholesale-replaced on each PEERS message
        # from that introducer, never appended to, so a peer this node
        # dropped stops being shown as "claimed" the moment the
        # introducer's next list omits it, the same way the table stops
        # listing a peer this node itself lost. See record_claim().
        self._claimed = {}
        self.max_height_observed = 0
        # Held peers per /24 or /64, kept in step with _peers so the
        # diversity cap is a lookup rather than a scan. See add().
        self._subnets   = {}          # subnet key -> count
        self._lock      = threading.Lock()
        # addr -> monotonic time an in-progress connection attempt began.
        # Display-only, like _learned_from: nothing here reads it back to
        # decide anything, it exists so the network graph can show a
        # candidate while discovery is still trying it (ping, relayed
        # punch, direct punch can together take tens of seconds) instead
        # of a peer only ever appearing at the moment it's already admitted.
        self._attempting = {}

    def _forget(self, addr):
        """Drop addr from every index. Callers hold the lock."""
        if self._peers.pop(addr, None) is None:
            return
        self._info.pop(addr, None)
        self._learned_from.pop(addr, None)
        self._claimed.pop(addr, None)
        subnet = _subnet_key(addr)
        if subnet is not None:
            remaining = self._subnets.get(subnet, 0) - 1
            if remaining > 0:
                self._subnets[subnet] = remaining
            else:
                self._subnets.pop(subnet, None)

    # ---- Core operations ----

    def _evict_worst_cooldown_locked(self, now_mono) -> bool:
        """Forget the currently-held peer serving the longest cooldown, if
        any peer is in cooldown at all. Callers hold the lock. Returns
        whether one was found and evicted."""
        worst_addr, worst_strikes = None, -1
        for a, rec in self._fails.items():
            if a not in self._peers or now_mono >= rec.get("cooldown_until", 0.0):
                continue
            strikes = rec.get("strikes", 0)
            if strikes > worst_strikes:
                worst_addr, worst_strikes = a, strikes
        if worst_addr is None:
            return False
        self._forget(worst_addr)
        log.info("[peer] evicted %s (cooldown, %d strikes) to make room",
                 worst_addr, worst_strikes)
        return True

    def add(self, addr, allow_private=False, learned_from=None):
        """Add a peer. Returns True if it was new.

        allow_private bypasses the private/loopback/link-local rejection.
        Only for addresses a local operator entered deliberately (the
        private dashboard's manual add-peer form), never for anything
        sourced from the DHT, peer-exchange, or another peer.

        learned_from is the peer whose PEERS message named addr, or None
        when it came from the DHT, a torrent swarm lookup, or the
        operator directly. Recorded, not acted on: it doesn't affect
        whether addr is admitted, only what snapshot() can later say
        about where it came from."""
        if not allow_private and not is_routable_peer_addr(addr):
            return False
        now_mono = time.monotonic()
        with self._lock:
            if addr in self._peers:
                self._peers[addr] = time.time()
                return False
            if len(self._peers) >= self._max_peers:
                # Full doesn't mean no room: a held peer currently serving
                # a cooldown (one or two strikes, not yet banned) is dead
                # weight occupying a slot while doing nothing useful for
                # it. Reclaim the worst such slot, most strikes first,
                # rather than turning away a candidate that's at least
                # reachable enough to have gotten this far. A pool that's
                # genuinely full of healthy peers still refuses: this
                # never evicts a peer that isn't already known-bad, so it
                # can't be used to churn out good peers by presenting
                # more candidates than there's room for.
                if not self._evict_worst_cooldown_locked(now_mono):
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
            if learned_from is not None:
                self._learned_from[addr] = learned_from
        log.debug("[peer] added  addr=%s", addr)
        return True

    def record_claim(self, introducer, addrs):
        """Record the most recent full peer list introducer sent, for
        display even where addrs weren't all admitted (unreachable, or
        not yet tried). Only for an introducer this node currently holds:
        the caller (main.py's on_peers) already gates on that before
        calling here, this re-checks it rather than trust the caller, the
        same defense-in-depth as the length cap on the list itself."""
        addrs = [a for a in addrs if isinstance(a, str) and ":" in a][:PEERS_PER_MESSAGE_LIMIT]
        with self._lock:
            if introducer not in self._peers:
                return
            self._claimed[introducer] = addrs

    def claims(self):
        """{introducer: [claimed addr, ...]}, a shallow copy for display."""
        with self._lock:
            return {k: list(v) for k, v in self._claimed.items()}

    def mark_attempting(self, addr):
        """Record that discovery just started trying to reach addr."""
        with self._lock:
            self._attempting[addr] = time.monotonic()

    def unmark_attempting(self, addr):
        """The attempt for addr is over, admitted or not. A no-op if it
        was never marked (or already admitted, which unmark doesn't race
        with: admission and unmark both happen from the end of the same
        _try_candidate call)."""
        with self._lock:
            self._attempting.pop(addr, None)

    def attempting(self):
        """Addrs discovery is currently trying to reach, for display only.
        Never includes an already-held peer: admission always happens
        before the caller unmarks it, so by the time an addr leaves this
        set it's either in the pool already or genuinely given up on."""
        with self._lock:
            held = set(self._peers)
            return [a for a in self._attempting if a not in held]

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
            if height is not None and height > self.max_height_observed:
                self.max_height_observed = height

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

    def is_known(self, addr):
        """True if addr is a currently held peer (on cooldown or not).

        Membership, not trust in what it says: a peer already survived
        ping/pong admission and the subnet diversity cap to get here, so
        it's a much smaller set to abuse than "anyone on the internet who
        knows the genesis hash" (see on_peers in main.py, the only caller
        that matters: it uses this to decide whether to believe a PEERS
        message at all)."""
        with self._lock:
            return addr in self._peers

    def snapshot(self):
        """Return [(addr, last_seen, active, height, version,
        http_reachable, introduced_by)] for display. active is False while
        a peer is in cooldown after repeated failures. height/version are
        the last-known confirmed values from a GETINFO exchange (see
        update_info), or (None, "") if none has completed yet.

        No wallet, by design and no longer by omission. A peer's payout
        address is not this node's business to know or to publish: it
        arrives as a relayed liveness note whose sender is not its author.
        http_reachable is
        True/False from the most recent HTTP probe (see set_http_reachable)
        if one completed within the last HTTP_REACHABLE_TTL seconds,
        otherwise None, stale or never-checked, treated the same as
        "don't know" rather than assumed reachable. introduced_by is the
        peer whose PEERS message named this one, or None when it came from
        the DHT, a torrent swarm lookup, or the operator directly (see
        add())."""
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
                    self._learned_from.get(addr),
                ))
            return result
