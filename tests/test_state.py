"""
Unit tests for state.py

Covers: credit, debit, set_nonce, apply_tx, compute_block_reward,
apply_reward_distribution, snapshot, from_snapshot, and the emission formula.

  - can_mint = SUPPLY_CAP - total_minted
  - reward = (can_mint * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR
  - the full block reward mints to the builder (no burn-based split)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state as state_mod
from state import compute_reward
from params import (
    EMISSION_DECAY_NUMERATOR, EMISSION_DECAY_DENOMINATOR, SUPPLY_CAP, TICKS_PER_LAPSE
)
from tests.fixtures import address, make_tx, seed_balance


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. credit / debit / nonce
# ---------------------------------------------------------------------------

class TestCreditDebit:
    def test_credit_increases_balance(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        assert s.get_balance(addr) == 100

    def test_credit_accumulates(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        s.credit(addr, 200)
        assert s.get_balance(addr) == 300

    def test_credit_zero_raises(self):
        s = fresh_state()
        with pytest.raises(ValueError):
            s.credit(address(0), 0)

    def test_credit_negative_raises(self):
        s = fresh_state()
        with pytest.raises(ValueError):
            s.credit(address(0), -1)

    def test_debit_reduces_balance(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 500)
        s.debit(addr, 200)
        assert s.get_balance(addr) == 300

    def test_debit_exact_balance_succeeds(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        s.debit(addr, 100)
        assert s.get_balance(addr) == 0

    def test_debit_overdraft_raises(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        with pytest.raises(ValueError, match="negative"):
            s.debit(addr, 101)

    def test_debit_zero_raises(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        with pytest.raises(ValueError):
            s.debit(addr, 0)

    def test_get_balance_unknown_address_returns_zero(self):
        s = fresh_state()
        assert s.get_balance("unknown.addr") == 0

    def test_get_nonce_unknown_address_returns_zero(self):
        s = fresh_state()
        assert s.get_nonce("unknown.addr") == 0

    def test_set_nonce_stores_value(self):
        s = fresh_state()
        addr = address(0)
        s.set_nonce(addr, 7)
        assert s.get_nonce(addr) == 7


# ---------------------------------------------------------------------------
# 2. apply_tx
# ---------------------------------------------------------------------------

class TestApplyTx:
    def test_apply_tx_debits_sender(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        addr0 = address(0)
        bal_before = s.get_balance(addr0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t)
        expected = bal_before - TICKS_PER_LAPSE - t["fee"]
        assert s.get_balance(addr0) == expected

    def test_apply_tx_credits_recipient(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t)
        assert s.get_balance(address(1)) == TICKS_PER_LAPSE

    def test_apply_tx_advances_nonce(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t)
        assert s.get_nonce(address(0)) == t["nonce"]

    def test_multiple_outputs_all_credited(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        outputs = [
            {"to": address(1), "amount": TICKS_PER_LAPSE},
            {"to": address(2), "amount": 2 * TICKS_PER_LAPSE},
        ]
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, outputs_override=outputs)
        s.apply_tx(t)
        assert s.get_balance(address(1)) == TICKS_PER_LAPSE
        assert s.get_balance(address(2)) == 2 * TICKS_PER_LAPSE


# ---------------------------------------------------------------------------
# 3. Emission formula
# ---------------------------------------------------------------------------

class TestEmission:
    def test_initial_reward_positive(self):
        reward = compute_reward(0)
        assert reward > 0

    def test_reward_decreases_as_minted_increases(self):
        r1 = compute_reward(0)
        r2 = compute_reward(SUPPLY_CAP // 2)
        assert r2 < r1

    def test_reward_zero_when_cap_exhausted(self):
        assert compute_reward(SUPPLY_CAP) == 0

    def test_reward_formula(self):
        """can_mint = SUPPLY_CAP - total_minted;
        reward = (can_mint * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR"""
        minted = 5_000_000 * TICKS_PER_LAPSE
        can_mint = SUPPLY_CAP - minted
        expected = (can_mint * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR
        assert compute_reward(minted) == expected

    def test_state_compute_block_reward_uses_state_totals(self):
        s = fresh_state()
        s.total_minted = SUPPLY_CAP // 4
        r_state = s.compute_block_reward()
        r_direct = compute_reward(s.total_minted)
        assert r_state == r_direct

    def test_negative_can_mint_returns_zero(self):
        """Guard against can_mint going negative (shouldn't happen normally)."""
        assert compute_reward(SUPPLY_CAP + TICKS_PER_LAPSE) == 0

    def test_compute_can_mint_is_the_shared_pool_formula(self):
        """compute_reward is derived from compute_can_mint, not a second copy."""
        minted = 5_000_000 * TICKS_PER_LAPSE
        pool   = state_mod.compute_can_mint(minted)
        assert pool == SUPPLY_CAP - minted
        assert compute_reward(minted) == (pool * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR

    def test_compute_can_mint_floors_at_zero(self):
        assert state_mod.compute_can_mint(SUPPLY_CAP + TICKS_PER_LAPSE) == 0

    def test_state_compute_can_mint_uses_state_totals(self):
        s = fresh_state()
        s.total_minted = SUPPLY_CAP // 4
        assert s.compute_can_mint() == state_mod.compute_can_mint(s.total_minted)


# ---------------------------------------------------------------------------
# 4. apply_reward_distribution
# ---------------------------------------------------------------------------

class TestApplyRewardDistribution:
    def test_reward_credits_builder(self):
        s = fresh_state()
        builder = address(0)
        reward = 1000
        s.apply_reward_distribution([(builder, reward)])
        assert s.get_balance(builder) == reward

    def test_reward_increments_total_minted(self):
        s = fresh_state()
        builder = address(0)
        reward = 5000
        s.apply_reward_distribution([(builder, reward)])
        assert s.total_minted == reward

    def test_reward_split_among_contributors(self):
        s = fresh_state()
        a0, a1 = address(0), address(1)
        dist = [(a0, 700), (a1, 300)]
        s.apply_reward_distribution(dist)
        assert s.get_balance(a0) == 700
        assert s.get_balance(a1) == 300
        assert s.total_minted == 1000

    def test_zero_amount_not_credited(self):
        s = fresh_state()
        s.apply_reward_distribution([(address(0), 0)])
        assert s.get_balance(address(0)) == 0
        assert s.total_minted == 0


# ---------------------------------------------------------------------------
# 5. Snapshot / from_snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_is_independent(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        snap = s.snapshot()
        s.credit(address(0), 999)
        assert snap.get_balance(address(0)) != s.get_balance(address(0))

    def test_snapshot_preserves_totals(self):
        s = fresh_state()
        s.total_minted = 12345
        snap = s.snapshot()
        assert snap.total_minted == 12345

    def test_snapshot_modification_does_not_affect_original(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        snap = s.snapshot()
        snap.credit(address(1), 1_000_000)
        assert s.get_balance(address(1)) == 0

    def test_from_snapshot_restores_state(self):
        s = fresh_state()
        seed_balance(s, 0, 10.0)
        balances = s.all_balances()
        nonces   = s.all_nonces()
        s2 = state_mod.State.from_snapshot(balances, nonces, s.total_minted)
        assert s2.get_balance(address(0)) == s.get_balance(address(0))
        assert s2.total_minted == s.total_minted

    def test_all_balances_returns_copy(self):
        s = fresh_state()
        seed_balance(s, 0, 1.0)
        bals = s.all_balances()
        bals["injected"] = 999
        assert "injected" not in s.all_balances()


class TestZeroBalancesAreNotHolders:
    """get_all_balances' docstring has always promised every entry is a
    real holder, and the holder count, the wealth histogram and the
    distribution pages all read it that way. Nothing used to remove an
    address that spent its last tick, so every spent-out address counted
    as a holder forever and sat in the smallest bucket."""

    def test_spending_everything_removes_the_holder(self):
        s = state_mod.State()
        s.credit("alice", 500)
        s.credit("bob", 10)
        assert len(s.get_all_balances()) == 2
        s.debit("alice", 500)
        assert s.get_all_balances() == [10]
        assert s.all_balances() == {"bob": 10}

    def test_balance_still_reads_as_zero(self):
        s = state_mod.State()
        s.credit("alice", 500)
        s.debit("alice", 500)
        assert s.get_balance("alice") == 0

    def test_the_nonce_survives_so_replays_stay_blocked(self):
        # The balance going is not the account going. Dropping the nonce
        # with it would let a spent-out address replay its old
        # transactions.
        s = state_mod.State()
        s.credit("alice", 500)
        s.set_nonce("alice", 7)
        s.debit("alice", 500)
        assert s.get_nonce("alice") == 7

    def test_a_partial_spend_keeps_the_holder(self):
        s = state_mod.State()
        s.credit("alice", 500)
        s.debit("alice", 499)
        assert s.all_balances() == {"alice": 1}

    def test_a_restart_does_not_bring_a_spent_out_address_back(self):
        # The row on disk is real and has to stay: it carries the nonce.
        # What it must not do is come back as a balance, which would undo
        # the invariant above on the first restart and nowhere else, so
        # only a node that had run for a while would ever show it.
        restored = state_mod.State.from_snapshot(
            {"alice": 0, "bob": 10}, {"alice": 7, "bob": 0}, 0)
        assert restored.all_balances() == {"bob": 10}
        assert restored.get_all_balances() == [10]
        assert restored.get_balance("alice") == 0
        assert restored.get_nonce("alice") == 7


class TestDirtyTracking:
    """Only what moved is written to disk; see storage._save_state_delta_inner."""

    def test_touched_addresses_are_reported(self):
        s = state_mod.State()
        s.credit("alice", 10)
        s.set_nonce("bob", 1)
        assert s.dirty_addresses() == frozenset({"alice", "bob"})

    def test_marking_persisted_clears_it(self):
        s = state_mod.State()
        s.credit("alice", 10)
        s.mark_persisted()
        assert s.dirty_addresses() == frozenset()

    def test_a_removed_address_is_still_reported_as_touched(self):
        # Its row has to be updated too, which means the writer has to hear
        # about an address that is no longer in the ledger at all.
        s = state_mod.State()
        s.credit("alice", 10)
        s.mark_persisted()
        s.debit("alice", 10)
        assert "alice" in s.dirty_addresses()

    def test_a_snapshot_carries_unwritten_changes_forward(self):
        # validate_and_apply hands its probe straight on to become the
        # committed state, so anything still unwritten when the probe was
        # taken has to travel with it or those rows never reach disk.
        s = state_mod.State()
        s.credit("alice", 10)
        probe = s.snapshot()
        probe.credit("bob", 5)
        assert probe.dirty_addresses() == frozenset({"alice", "bob"})

    def test_marking_a_snapshot_persisted_leaves_the_original_alone(self):
        s = state_mod.State()
        s.credit("alice", 10)
        probe = s.snapshot()
        probe.mark_persisted()
        assert s.dirty_addresses() == frozenset({"alice"})
