"""
Unit tests for mempool.py

Covers: add, remove, remove_many, get, get_txs_by_hashes, size, all_txs,
pending_nonce, pending_hashes, prune_stale.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mempool as mempool_mod
from mempool import Mempool
import state as state_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_tx, seed_balance


def fresh_mempool():
    return mempool_mod.Mempool()


def fresh_state():
    return state_mod.State()


def sample_tx(sender_index=0, recipient_index=1, amount=TICKS_PER_LAPSE):
    s = fresh_state()
    seed_balance(s, sender_index, 100.0)
    return make_tx(sender_index, recipient_index, amount, s)


# ---------------------------------------------------------------------------
# 1. add
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_returns_true_and_hash(self):
        mp = fresh_mempool()
        t = sample_tx()
        ok, result = mp.add(t)
        assert ok is True
        assert isinstance(result, str) and len(result) == 64

    def test_add_duplicate_returns_false(self):
        mp = fresh_mempool()
        t = sample_tx()
        mp.add(t)
        ok, reason = mp.add(t)
        assert ok is False
        assert reason == "duplicate"

    def test_add_increments_size(self):
        mp = fresh_mempool()
        assert mp.size() == 0
        mp.add(sample_tx())
        assert mp.size() == 1


# ---------------------------------------------------------------------------
# 2. remove / remove_many
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_by_hash(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        mp.remove(h)
        assert mp.size() == 0

    def test_remove_nonexistent_is_noop(self):
        mp = fresh_mempool()
        mp.remove("00" * 32)  # Should not raise

    def test_remove_many(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        mp.remove_many([h1, h2])
        assert mp.size() == 0

    def test_remove_many_partial(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        mp.remove_many([h1])
        assert mp.size() == 1


# ---------------------------------------------------------------------------
# 3. get / get_txs_by_hashes
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_existing_tx(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        result = mp.get(h)
        assert result == t

    def test_get_missing_returns_none(self):
        mp = fresh_mempool()
        assert mp.get("00" * 32) is None

    def test_get_txs_by_hashes_returns_present(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        results = mp.get_txs_by_hashes([h1, h2])
        assert len(results) == 2

    def test_get_txs_by_hashes_skips_missing(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        results = mp.get_txs_by_hashes([h, "missing" * 4])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 4. all_txs / pending_nonce / pending_hashes
# ---------------------------------------------------------------------------

class TestAllTxs:
    def test_all_txs_empty(self):
        mp = fresh_mempool()
        assert mp.all_txs() == []

    def test_all_txs_returns_all(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        mp.add(t1)
        mp.add(t2)
        all_t = mp.all_txs()
        assert len(all_t) == 2

    def test_pending_nonce_returns_zero_when_absent(self):
        mp = fresh_mempool()
        assert mp.pending_nonce(address(0)) == 0

    def test_pending_nonce_returns_highest_for_sender(self):
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t1 = make_tx(0, 1, 1, s)
        mp.add(t1)
        t2 = make_tx(0, 1, 1, s, nonce_override=t1["nonce"] + 1)
        mp.add(t2)
        assert mp.pending_nonce(address(0)) == t2["nonce"]

    def test_pending_hashes_returns_frozenset(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        hashes = mp.pending_hashes()
        assert isinstance(hashes, frozenset)
        assert h in hashes


# ---------------------------------------------------------------------------
# 5. prune_stale
# ---------------------------------------------------------------------------

class TestPruneStale:
    def test_prune_superseded_nonce(self):
        """Tx whose nonce is already used by state is pruned."""
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        s.apply_tx(t)  # nonce is now consumed
        pruned = mp.prune_stale(state=s)
        assert len(pruned) == 1
        assert mp.size() == 0

    def test_prune_stale_ttl(self):
        """Tx older than TTL is pruned."""
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        # Force very short TTL
        pruned = mp.prune_stale(state=s, ttl_seconds=0)
        assert len(pruned) == 1

    def test_prune_valid_tx_stays(self):
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        pruned = mp.prune_stale(state=s)
        assert len(pruned) == 0
        assert mp.size() == 1


class TestProbeOverlayMatchesASnapshot:
    """The probe is a read-through view now rather than a full copy of the
    ledger. Differential: it must answer exactly what a real snapshot
    would, for the operations tx.validate and apply_tx perform."""

    def _both(self, seed):
        base = state_mod.State()
        for addr, bal, nonce in seed:
            if bal:
                base.credit(addr, bal)
            if nonce:
                base.set_nonce(addr, nonce)
        return base

    def test_reads_match_for_known_and_unknown_addresses(self):
        base = self._both([("alice", 500, 2), ("bob", 10, 0)])
        pool = Mempool()
        probe = pool.probe_state_for("alice", base)
        snap = base.snapshot()
        for addr in ("alice", "bob", "nobody"):
            assert probe.get_balance(addr) == snap.get_balance(addr)
            assert probe.get_nonce(addr) == snap.get_nonce(addr)

    def test_writes_match_a_snapshot_and_leave_the_base_alone(self):
        base = self._both([("alice", 500, 2), ("bob", 10, 0)])
        probe = Mempool().probe_state_for("alice", base)
        snap = base.snapshot()
        for target in (probe, snap):
            target.debit("alice", 300)
            target.credit("bob", 300)
            target.set_nonce("alice", 3)
        for addr in ("alice", "bob"):
            assert probe.get_balance(addr) == snap.get_balance(addr)
            assert probe.get_nonce(addr) == snap.get_nonce(addr)
        # the underlying state is untouched by either
        assert base.get_balance("alice") == 500
        assert base.get_nonce("alice") == 2

    def test_a_spent_out_address_reads_zero_not_its_old_balance(self):
        # The overlay has to record the zero. Forgetting it would fall
        # through and read the larger underlying balance straight back,
        # which is a double spend.
        base = self._both([("alice", 500, 0)])
        probe = Mempool().probe_state_for("alice", base)
        probe.debit("alice", 500)
        assert probe.get_balance("alice") == 0
        assert base.get_balance("alice") == 500

    def test_debit_past_zero_still_refuses(self):
        base = self._both([("alice", 100, 0)])
        probe = Mempool().probe_state_for("alice", base)
        with pytest.raises(ValueError):
            probe.debit("alice", 101)

    def test_pending_txs_are_applied_in_nonce_order(self):
        base = self._both([(address(0), 10 * TICKS_PER_LAPSE, 0)])
        pool = Mempool()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, base)
        pool.add(t1)
        probe = pool.probe_state_for(address(0), base)
        assert probe.get_nonce(address(0)) == 1
        assert probe.get_balance(address(0)) == 9 * TICKS_PER_LAPSE
