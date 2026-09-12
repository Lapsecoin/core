"""
Unit tests for peerpool.py

Covers: add (capacity, cooldown block), touch, strike (exponential cooldown,
banning at MAX_STRIKES), remove, evict_stale, get_all (excludes cooldown),
random, count, all_addrs.

All tests are local. No network, no disk.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from peerpool import (
    PeerPool, COOLDOWN_MAX_SECONDS, MAX_STRIKES, STALE_SECONDS, HTTP_REACHABLE_TTL
)


def make_pool(max_peers=10):
    return PeerPool(max_peers=max_peers)


# ---------------------------------------------------------------------------
# 1. add
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_new_peer_returns_true(self):
        p = make_pool()
        assert p.add("1.2.3.4:9000") is True

    def test_add_same_peer_twice_returns_false(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        assert p.add("1.2.3.4:9000") is False

    def test_add_increments_count(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        assert p.count() == 1

    def test_add_at_capacity_returns_false(self):
        p = make_pool(max_peers=2)
        p.add("1.2.3.4:9000")
        p.add("1.2.3.5:9000")
        assert p.add("1.2.3.6:9000") is False
        assert p.count() == 2

    def test_add_blocked_during_cooldown(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        # Strike once to create a cooldown
        p.strike(peer)
        # Remove from pool so add would otherwise succeed
        p.remove(peer)
        # Should be blocked by cooldown
        assert p.add(peer) is False

    def test_add_allowed_after_cooldown_expires(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        p.remove(peer)
        # Force cooldown to have expired
        p._fails[peer]["cooldown_until"] = 0.0
        assert p.add(peer) is True


# ---------------------------------------------------------------------------
# 2. touch
# ---------------------------------------------------------------------------

class TestTouch:
    def test_touch_clears_strikes(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        assert peer in p._fails
        p.touch(peer)
        assert peer not in p._fails

    def test_touch_updates_last_seen(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        old_ts = p._peers[peer]
        time.sleep(0.01)
        p.touch(peer)
        assert p._peers[peer] >= old_ts

    def test_touch_unknown_peer_is_noop(self):
        p = make_pool()
        p.touch("9.9.9.9:9000")  # must not raise


# ---------------------------------------------------------------------------
# 3. strike (exponential cooldown, banning)
# ---------------------------------------------------------------------------

class TestStrike:
    def test_first_strike_adds_cooldown(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        assert peer in p._fails
        assert p._fails[peer]["strikes"] == 1

    def test_cooldown_is_exponential(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        cd1 = p._fails[peer]["cooldown_until"]
        p.strike(peer)
        cd2 = p._fails[peer]["cooldown_until"]
        # Second cooldown must end later than first
        assert cd2 > cd1

    def test_max_strikes_bans_peer(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        for _ in range(MAX_STRIKES):
            p.strike(peer)
        assert peer not in p._peers

    def test_banned_peer_not_in_get_all(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        for _ in range(MAX_STRIKES):
            p.strike(peer)
        assert peer not in p.get_all()

    def test_cooldown_capped_at_max(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        # Strike many times (won't all execute since peer is removed at MAX_STRIKES,
        # but we test the cooldown value doesn't exceed the cap)
        for _ in range(MAX_STRIKES - 1):
            p.strike(peer)
        cd = p._fails.get(peer, {}).get("cooldown_until", 0)
        assert cd <= time.monotonic() + COOLDOWN_MAX_SECONDS + 1  # +1 for timing slop

    def test_strike_unknown_peer_is_noop(self):
        p = make_pool()
        p.strike("9.9.9.9:9000")  # must not raise


# ---------------------------------------------------------------------------
# 4. remove
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_existing_peer(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.remove(peer)
        assert p.count() == 0

    def test_remove_nonexistent_is_noop(self):
        p = make_pool()
        p.remove("9.9.9.9:9000")  # must not raise


# ---------------------------------------------------------------------------
# 5. evict_stale
# ---------------------------------------------------------------------------

class TestEvictStale:
    def test_evict_stale_removes_old_peers(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        # Backdate last-seen past the stale threshold
        p._peers[peer] = time.time() - STALE_SECONDS - 1
        p.evict_stale()
        assert p.count() == 0

    def test_evict_stale_keeps_fresh_peers(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.evict_stale()
        assert p.count() == 1

    def test_evict_stale_selective(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        p.add("1.2.3.5:9000")
        p._peers["1.2.3.4:9000"] = time.time() - STALE_SECONDS - 1
        p.evict_stale()
        assert p.count() == 1
        assert "1.2.3.5:9000" in p._peers

    def test_evict_stale_clears_cached_info(self):
        """Mirrors remove()/the strike-ban path: a peer's cached
        height/wallet must not survive it leaving the pool, or a later
        re-add would show stale info before any fresh GETINFO exchange."""
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.update_info(peer, height=1, wallet="x")
        p._peers[peer] = time.time() - STALE_SECONDS - 1
        p.evict_stale()
        p.add(peer)
        rows = p.snapshot()
        assert rows[0][3] is None
        assert rows[0][4] == ""


# ---------------------------------------------------------------------------
# 6. get_all
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_get_all_empty(self):
        p = make_pool()
        assert p.get_all() == []

    def test_get_all_returns_all_peers(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        p.add("1.2.3.5:9000")
        assert set(p.get_all()) == {"1.2.3.4:9000", "1.2.3.5:9000"}

    def test_get_all_excludes_peers_on_cooldown(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)  # puts peer on cooldown (still in _peers if < MAX_STRIKES)
        peers = p.get_all()
        # Peer is in _peers but excluded by cooldown check
        assert peer not in peers

    def test_get_all_includes_peer_after_cooldown(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        p._fails[peer]["cooldown_until"] = 0.0  # expire the cooldown
        assert peer in p.get_all()


# ---------------------------------------------------------------------------
# 7. random
# ---------------------------------------------------------------------------

class TestRandom:
    def test_random_none_when_empty(self):
        p = make_pool()
        assert p.random() is None

    def test_random_returns_peer_when_one_peer(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        assert p.random() == "1.2.3.4:9000"

    def test_random_returns_one_of_many(self):
        p = make_pool()
        addrs = {f"1.2.3.{i}:9000" for i in range(5)}
        for a in addrs:
            p.add(a)
        result = p.random()
        assert result in addrs


# ---------------------------------------------------------------------------
# 8. all_addrs
# ---------------------------------------------------------------------------

class TestAllAddrs:
    def test_all_addrs_includes_cooldown_peers(self):
        """all_addrs returns raw list including peers on cooldown (unlike get_all)."""
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)  # cooldown active
        # all_addrs includes it
        assert peer in p.all_addrs()
        # get_all excludes it
        assert peer not in p.get_all()

    def test_all_addrs_empty_when_no_peers(self):
        p = make_pool()
        assert p.all_addrs() == []


class TestSnapshot:
    def test_snapshot_empty_when_no_peers(self):
        p = make_pool()
        assert p.snapshot() == []

    def test_snapshot_reports_active_peer(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        rows = p.snapshot()
        assert len(rows) == 1
        addr, last_seen, active, height, wallet, version, http_reachable = rows[0]
        assert addr == "1.2.3.4:9000"
        assert last_seen > 0
        assert active is True
        assert height is None
        assert wallet == ""
        assert version == ""
        assert http_reachable is None

    def test_snapshot_reports_cooldown_peer_as_inactive(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.strike(peer)
        rows = p.snapshot()
        assert len(rows) == 1
        assert rows[0][0] == peer
        assert rows[0][2] is False

    def test_snapshot_includes_cached_info(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.update_info(peer, height=42, wallet="a.b.c")
        rows = p.snapshot()
        assert rows[0][3] == 42
        assert rows[0][4] == "a.b.c"

    def test_snapshot_includes_version(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.update_info(peer, height=1, wallet="a", version="0.1.1")
        rows = p.snapshot()
        assert rows[0][5] == "0.1.1"

    def test_http_reachable_defaults_to_none(self):
        p = make_pool()
        p.add("1.2.3.4:9000")
        assert p.snapshot()[0][6] is None

    def test_http_reachable_true(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.set_http_reachable(peer, True)
        assert p.snapshot()[0][6] is True

    def test_http_reachable_false(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.set_http_reachable(peer, False)
        assert p.snapshot()[0][6] is False

    def test_http_reachable_stale_result_reads_as_none(self):
        """A probe result older than HTTP_REACHABLE_TTL is treated as
        unknown rather than trusted indefinitely, a peer that went
        offline shouldn't stay marked reachable forever."""
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.set_http_reachable(peer, True, checked_at=time.time() - HTTP_REACHABLE_TTL - 1)
        assert p.snapshot()[0][6] is None

    def test_http_reachable_noop_for_unknown_peer(self):
        p = make_pool()
        p.set_http_reachable("1.2.3.4:9000", True)
        assert p.snapshot() == []

    def test_http_reachable_cleared_on_remove(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.set_http_reachable(peer, True)
        p.remove(peer)
        p.add(peer)
        assert p.snapshot()[0][6] is None


class TestNoteRelayedBuilder:
    def test_update_info_noop_for_unknown_peer(self):
        p = make_pool()
        p.update_info("1.2.3.4:9000", height=1, wallet="x")
        assert p.snapshot() == []

    def test_update_info_cleared_on_remove(self):
        p = make_pool()
        peer = "1.2.3.4:9000"
        p.add(peer)
        p.update_info(peer, height=1, wallet="x")
        p.remove(peer)
        p.add(peer)
        rows = p.snapshot()
        assert rows[0][3] is None
        assert rows[0][4] == ""
