"""Public relay-pool client for the same-NAT last-resort fallback (see
Discovery._behind_same_nat in discovery.py).

Two peers who need a relay have no working channel to negotiate which one
to use, that's the whole reason they're here, direct contact between them
has already failed every other way. So instead of negotiating, both sides
independently compute the *same* ranked order over the *same* public relay
list and walk it in lock-step: try candidate 0 first, then 1, then 2, on
both ends, with nothing exchanged. As long as both fetched a reasonably
fresh copy of the list (see CACHE_TTL), they converge on trying the same
relay at the same step without ever telling each other which one they
picked.

This module only decides *which relay to try next*. It knows nothing
about the relay wire protocol itself (session establishment, framing) --
that's a separate concern, kept out of this file on purpose so the
selection/failover logic here can be tested without a real relay running
anywhere.
"""

import hashlib
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("ec.relaypool")

RELAY_POOL_URL = "https://relays.syncthing.net/endpoint"
FETCH_TIMEOUT  = 5
# Long enough that two peers fetching independently, even minutes apart,
# almost always see the same list (the public pool doesn't churn on a
# per-minute basis); short enough that a relay that's actually gone stops
# being handed out well within a session's lifetime.
CACHE_TTL = 600

_lock  = threading.Lock()
_cache = {"fetched_at": 0.0, "relays": []}


def fetch_relay_pool(force=False):
    """The live relay directory, refetched at most once per CACHE_TTL
    unless force=True. Returns a list of relay URL strings (e.g.
    "relay://1.2.3.4:22067/?id=..."), or whatever was last fetched
    successfully (possibly []) if the network call fails.

    Never raises: this exists to be called from the last-resort branch of
    an already-failed connection attempt, so a broken fetch here must
    degrade to "no relay available this round," never to an exception that
    takes down discovery's retry loop.
    """
    with _lock:
        age = time.monotonic() - _cache["fetched_at"]
        if not force and age < CACHE_TTL and _cache["relays"]:
            return list(_cache["relays"])
    try:
        req = urllib.request.Request(
            RELAY_POOL_URL, headers={"User-Agent": "lapsecoin-node/relay-pool"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = json.load(resp)
        relays = [r["url"] for r in data.get("relays", [])
                  if isinstance(r, dict) and isinstance(r.get("url"), str)]
    except Exception:
        log.debug("[relay] pool fetch failed", exc_info=True)
        with _lock:
            return list(_cache["relays"])
    with _lock:
        _cache["fetched_at"] = time.monotonic()
        _cache["relays"]     = relays
    log.info("[relay] fetched %d relay(s) from the public pool", len(relays))
    return list(relays)


def _pair_seed(genesis_hash: str, addr_a: str, addr_b: str) -> bytes:
    """A seed both sides of a pair compute identically. Sorted so it
    doesn't matter which of the two calls this "us" vs "them"; salted with
    genesis_hash purely so this network's ranking doesn't happen to line
    up with some other project reusing the same relay-pool client for the
    same pair of IPs, nothing security-sensitive depends on it."""
    a, b = sorted((addr_a, addr_b))
    return hashlib.sha256(f"{genesis_hash}|{a}|{b}".encode()).digest()


def rank_relays_for_pair(relays, genesis_hash: str, addr_a: str, addr_b: str):
    """Deterministic ordering of relays for this specific pair. Given the
    same relays list and the same two addresses (order doesn't matter),
    every caller gets back the exact same order back, on any machine, any
    time -- that's the entire mechanism that lets two peers "agree" on a
    relay without talking to each other."""
    seed = _pair_seed(genesis_hash, addr_a, addr_b)
    return sorted(relays, key=lambda url: hashlib.sha256(seed + url.encode()).digest())


class RelayCascade:
    """Per-pair failover cursor: which ranked candidate to try next.

    A pair that fails candidate 0 (relay down, refused, timed out) moves
    to candidate 1 on its *next* attempt, not a fresh random pick, so a
    dead relay costs exactly one wasted attempt per side, not a repeated
    one every retry. Since both sides rank identically (rank_relays_for_pair)
    and advance the same way, they stay in lock-step through the same
    sequence of relays even though neither ever learns what the other
    chose.

    One instance is enough per node (see discovery.py); pairs are looked
    up by a sorted key so it doesn't matter which side is "us".
    """

    def __init__(self):
        self._cursor = {}   # (genesis_hash, addr_a, addr_b) sorted -> attempt index
        self._lock   = threading.Lock()

    @staticmethod
    def _key(genesis_hash, addr_a, addr_b):
        a, b = sorted((addr_a, addr_b))
        return (genesis_hash, a, b)

    def next_candidate(self, genesis_hash, addr_a, addr_b, relays=None):
        """The relay to try next for this pair, or None if the pool is
        currently empty. Each call for the same pair advances the cursor;
        wrapping around the ranked list (rather than giving up once every
        relay's been tried once) since a relay that looked dead a few
        attempts ago may not be by the time reconnection wraps back to it.
        On a full wrap, forces a fresh fetch first: a cached list that's
        been exhausted once is the best available evidence it might be
        stale, worth spending one extra fetch to rule out before trying
        the same relays again."""
        key = self._key(genesis_hash, addr_a, addr_b)
        with self._lock:
            idx = self._cursor.get(key, 0)

        force = idx > 0 and relays is None and self._wrapped(key, idx)
        pool = relays if relays is not None else fetch_relay_pool(force=force)
        ranked = rank_relays_for_pair(pool, genesis_hash, addr_a, addr_b)
        if not ranked:
            return None

        with self._lock:
            self._cursor[key] = idx + 1
            self._last_len = getattr(self, "_last_len", {})
            self._last_len[key] = len(ranked)
        return ranked[idx % len(ranked)]

    def _wrapped(self, key, idx) -> bool:
        last_len = getattr(self, "_last_len", {}).get(key)
        return bool(last_len) and idx % last_len == 0

    def reset(self, genesis_hash, addr_a, addr_b):
        """Call once a session for this pair actually succeeds (direct
        reconnection recovered, or a relayed session was established), so
        the *next* time this pair needs a relay it starts back at the
        top-ranked candidate instead of continuing to advance through a
        cascade left over from a previous, unrelated outage."""
        key = self._key(genesis_hash, addr_a, addr_b)
        with self._lock:
            self._cursor.pop(key, None)
            getattr(self, "_last_len", {}).pop(key, None)
