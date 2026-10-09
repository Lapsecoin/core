"""The fee lock, as the chain enforces it: what makes a request valid, how a
claim shortens the window, and where the locked funds end up."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto
import gaslock
import state as state_mod
import storage as storage_mod
import tx as tx_mod
from chainstate import ChainState
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_block, make_tx, seed_balance

ESC = crypto.escrow_address()
LOCK = 10 * TICKS_PER_LAPSE
FEE = 1_000
REQ_MEMO = "[gas] ethereum 1000000 0x000000000000000000000000000000000000dEaD c2ln"
START = 100 * TICKS_PER_LAPSE


def claim_memo(txid):
    return f"[gas-claim] {txid[:gaslock.REF_LEN]} 0x{'0' * 40} c2ln"


def after_recycling(minted, amount):
    """total_minted after a block that recycles `amount`: the settlement
    runs before the builder's reward, so that reward is already computed on
    the reduced total."""
    reduced = minted - amount
    return reduced + state_mod.compute_reward(reduced)


def control_after(height):
    """The same chain with no fee request at all, for what rewards alone do."""
    c = Chain()
    c.mine_until(height)
    return c


class Chain:
    """A chain driven block by block with no validation of the blocks
    themselves, so what is exercised is the ledger rules."""

    def __init__(self):
        self.cs = ChainState.from_genesis()
        seed_balance(self.cs.state, 1, 100.0)       # the asker
        seed_balance(self.cs.state, 2, 100.0)       # a node
        seed_balance(self.cs.state, 3, 100.0)       # another node
        self.request = None

    @property
    def state(self):
        return self.cs.state

    def mine(self, txs=()):
        blk = make_block(self.cs.height + 1, self.cs.tip["hash"], list(txs), builder_index=9)
        self.cs = self.cs.apply_block(blk)
        return blk

    def ask(self, lock=LOCK, memo=REQ_MEMO, sender=1):
        outs = [{"to": ESC, "amount": lock}]
        t = make_tx(sender, 0, 0, self.state, fee=FEE, outputs_override=outs, memo=memo)
        self.mine([t])
        self.request = tx_mod.tx_hash(t)
        return t

    def claim(self, sender=2, ref=None):
        t = make_tx(sender, 0, 1, self.state, fee=FEE,
                    memo=claim_memo(ref or self.request))
        self.mine([t])
        return t

    def mine_until(self, height):
        while self.cs.height < height:
            self.mine()

    @property
    def escrow(self):
        return self.state.escrows.get(self.request)


class TestValidity:
    def _valid(self, t, chain=None):
        chain = chain or Chain()
        return tx_mod.validate(t, chain.state)

    def test_a_proper_request_is_valid(self):
        c = Chain()
        t = make_tx(1, 0, 0, c.state, fee=FEE, outputs_override=[{"to": ESC, "amount": LOCK}],
                    memo=REQ_MEMO)
        assert tx_mod.validate(t, c.state) == (True, None)

    def test_an_ordinary_payment_is_untouched(self):
        c = Chain()
        assert tx_mod.validate(make_tx(1, 2, 5, c.state, fee=FEE), c.state)[0]

    @pytest.mark.parametrize("memo", ["", "hello", "[gas]", "[gas] a b c", "[gas] a b c d e",
                                      "[gas-claim] x y z", "[board] hi"])
    def test_funds_sent_to_escrow_need_a_request_memo(self, memo):
        c = Chain()
        t = make_tx(1, 0, 0, c.state, fee=FEE, outputs_override=[{"to": ESC, "amount": LOCK}],
                    memo=memo)
        ok, err = tx_mod.validate(t, c.state)
        assert not ok and "escrow" in err

    def test_exactly_one_escrow_output(self):
        c = Chain()
        t = make_tx(1, 0, 0, c.state, fee=FEE, memo=REQ_MEMO,
                    outputs_override=[{"to": ESC, "amount": LOCK}, {"to": ESC, "amount": LOCK}])
        ok, err = tx_mod.validate(t, c.state)
        assert not ok and "exactly one" in err

    def test_the_lock_has_a_minimum(self):
        c = Chain()
        t = make_tx(1, 0, 0, c.state, fee=FEE, memo=REQ_MEMO,
                    outputs_override=[{"to": ESC, "amount": gaslock.MIN_LOCK - 1}])
        ok, err = tx_mod.validate(t, c.state)
        assert not ok and "minimum" in err
        ok_t = make_tx(1, 0, 0, c.state, fee=FEE, memo=REQ_MEMO,
                       outputs_override=[{"to": ESC, "amount": gaslock.MIN_LOCK}])
        assert tx_mod.validate(ok_t, c.state)[0]

    def test_the_escrow_address_can_never_send(self):
        t = {"from": ESC, "outputs": [{"to": address(1), "amount": 1}], "nonce": 1, "fee": 1}
        assert gaslock.check_lock(t, 1) == (False, "the escrow address can never be a sender")
        ok, err = tx_mod.validate(t, Chain().state)
        assert not ok

    def test_escrow_and_burn_addresses_differ(self):
        assert ESC != crypto.burn_address() and crypto.is_valid_address(ESC)


class TestSettlement:
    def test_the_lock_leaves_the_sender_and_sits_in_escrow(self):
        c = Chain()
        c.ask()
        assert c.state.get_balance(ESC) == LOCK
        assert c.state.get_balance(address(1)) == START - LOCK - FEE
        assert c.escrow == {"sender": address(1), "amount": LOCK, "height": c.cs.height, "claim": None}

    def test_with_no_claim_it_comes_back_after_the_window(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.mine_until(h + gaslock.CLAIM_WINDOW_BLOCKS - 1)
        assert c.escrow is not None and c.state.get_balance(ESC) == LOCK
        c.mine()                                     # block h + 5
        assert c.escrow is None and c.state.get_balance(ESC) == 0
        assert c.state.get_balance(address(1)) == START - FEE
        assert c.state.total_minted == control_after(c.cs.height).state.total_minted

    def test_a_claim_shortens_the_window_to_one_block_later(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.claim()                                    # block h + 1
        assert c.escrow["claim"] == h + 1
        c.mine()                                     # block h + 2 closes it
        assert c.escrow is None

    def test_a_claimed_lock_is_recycled_not_refunded_and_not_paid_to_anyone(self):
        c = Chain()
        c.ask()
        asker_before = c.state.get_balance(address(1))
        c.claim()
        minted = c.state.total_minted
        c.mine()
        assert c.state.get_balance(ESC) == 0
        assert c.state.get_balance(address(1)) == asker_before              # not refunded
        assert c.state.get_balance(address(2)) == START - FEE - 1           # claimer paid its own fee, got nothing
        assert c.state.total_minted == after_recycling(minted, LOCK)        # back in the pool

    def test_the_recycled_amount_is_mintable_again(self):
        c = Chain()
        c.ask()
        c.claim()
        c.mine()
        control = control_after(c.cs.height)
        gained = c.state.compute_can_mint() - control.state.compute_can_mint()
        assert LOCK * 0.99 < gained <= LOCK

    def test_it_settles_exactly_when_the_window_closes_not_before(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.mine_until(h + 2)
        c.claim()                                    # block h + 3 -> closes at h + 4
        c.mine()                                     # h + 4
        assert c.escrow is None

    def test_a_claim_in_the_last_block_still_counts(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.mine_until(h + gaslock.CLAIM_WINDOW_BLOCKS - 1)
        assert c.escrow is not None                  # block h + 4: still open
        minted = c.state.total_minted
        c.claim()                                    # block h + 5
        assert c.escrow is None                      # settled in that same block, as claimed
        assert c.state.total_minted == after_recycling(minted, LOCK)

    def test_a_claim_after_the_window_changes_nothing(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.mine_until(h + gaslock.CLAIM_WINDOW_BLOCKS)
        assert c.escrow is None
        c.claim()
        assert c.state.total_minted == control_after(c.cs.height).state.total_minted
        assert c.state.get_balance(address(1)) == START - FEE

    def test_a_claim_in_the_requests_own_block_does_not_count(self):
        c = Chain()
        t = make_tx(1, 0, 0, c.state, fee=FEE, memo=REQ_MEMO,
                    outputs_override=[{"to": ESC, "amount": LOCK}])
        txid = tx_mod.tx_hash(t)
        early = make_tx(2, 0, 1, c.state, fee=FEE, memo=claim_memo(txid))
        c.mine([t, early])
        c.request = txid
        assert c.escrow["claim"] is None

    def test_only_the_first_claim_matters(self):
        c = Chain()
        c.ask()
        h = c.cs.height
        c.claim(sender=2)
        c.claim(sender=3)
        assert c.escrow is None or c.escrow["claim"] == h + 1

    def test_a_claim_for_some_other_request_is_ignored(self):
        c = Chain()
        c.ask()
        c.claim(ref="ff" * 32)
        assert c.escrow["claim"] is None

    @pytest.mark.parametrize("memo", [
        "[gas-claim] ", "[gas-claim] short 0x00 c2ln", "[gas-claim] " + "G" * 24 + " a b",
        "[gas-claim] " + "ab" * 12 + " a", "[gas-claim] " + "ab" * 12 + " a b c",
    ])
    def test_a_malformed_claim_is_ignored(self, memo):
        c = Chain()
        c.ask()
        t = make_tx(2, 0, 1, c.state, fee=FEE, memo=memo)
        c.mine([t])
        assert c.escrow["claim"] is None

    def test_two_requests_settle_independently(self):
        c = Chain()
        a = c.ask(sender=1)
        first = c.request
        h = c.cs.height
        c.ask(sender=2, lock=LOCK * 2)
        second = c.request
        c.claim(sender=3, ref=first)                 # only the first is claimed
        assert c.state.escrows[first]["claim"] is not None
        assert c.state.escrows[second]["claim"] is None
        c.mine()
        assert first not in c.state.escrows and second in c.state.escrows
        c.mine_until(h + 1 + gaslock.CLAIM_WINDOW_BLOCKS)
        assert second not in c.state.escrows
        assert c.state.get_balance(address(2)) == START - FEE               # refunded in full

    def test_supply_still_adds_up_after_a_refund(self):
        c = Chain()
        c.ask()
        c.mine_until(c.cs.height + gaslock.CLAIM_WINDOW_BLOCKS)
        assert sum(c.state.all_balances().values()) == c.state.total_minted

    def test_supply_still_adds_up_after_a_recycle(self):
        c = Chain()
        c.ask()
        c.claim()
        c.mine()
        assert c.request not in c.state.escrows
        assert sum(c.state.all_balances().values()) == c.state.total_minted

    def test_supply_adds_up_while_a_lock_is_open(self):
        c = Chain()
        c.ask()
        assert sum(c.state.all_balances().values()) == c.state.total_minted     # escrow holds it


class TestStateHandling:
    def test_snapshots_do_not_share_open_locks(self):
        c = Chain()
        c.ask()
        snap = c.state.snapshot()
        c.state.escrows[c.request]["claim"] = 999
        assert snap.escrows[c.request]["claim"] is None

    def test_replaying_the_whole_chain_reaches_the_same_state(self):
        # Funded by block rewards alone, so the chain can be replayed from
        # genesis the way a fresh node does.
        cs = ChainState.from_genesis()

        def mine(txs=()):
            nonlocal cs
            cs = cs.apply_block(make_block(cs.height + 1, cs.tip["hash"], list(txs), builder_index=9))

        for _ in range(6):
            mine()
        small = gaslock.MIN_LOCK
        ask = make_tx(9, 0, 0, cs.state, fee=FEE, memo=REQ_MEMO,
                      outputs_override=[{"to": ESC, "amount": small}])
        mine([ask])
        txid = tx_mod.tx_hash(ask)
        mine([make_tx(9, 0, 1, cs.state, fee=FEE, memo=claim_memo(txid))])
        mine()
        ask2 = make_tx(9, 0, 0, cs.state, fee=FEE, memo=REQ_MEMO,
                       outputs_override=[{"to": ESC, "amount": small}])
        mine([ask2])                                  # still open at the tip

        replay = ChainState.from_chain(cs.chain)
        assert replay.state.escrows == cs.state.escrows and len(cs.state.escrows) == 1
        assert replay.state.all_balances() == cs.state.all_balances()
        assert replay.state.total_minted == cs.state.total_minted

    def test_open_locks_survive_a_restart(self, tmp_path):
        c = Chain()
        c.ask()
        store = storage_mod.Storage(str(tmp_path / "chain.db"))
        store.save_state(c.state)
        assert store.load_escrows() == c.state.escrows
        restored = type(c.state).from_snapshot(*store.load_state(), escrows=store.load_escrows())
        assert restored.escrows == c.state.escrows
        assert restored.get_balance(ESC) == LOCK

    def test_a_settled_lock_is_gone_from_disk_too(self, tmp_path):
        c = Chain()
        c.ask()
        store = storage_mod.Storage(str(tmp_path / "chain.db"))
        store.save_state(c.state)
        c.mine_until(c.cs.height + gaslock.CLAIM_WINDOW_BLOCKS)
        store.save_state(c.state)
        assert store.load_escrows() == {}

    def test_a_database_from_before_fee_requests_has_no_locks(self, tmp_path):
        store = storage_mod.Storage(str(tmp_path / "chain.db"))
        assert store.load_escrows() == {}


class TestBlockValidation:
    def test_a_block_with_a_bad_lock_is_rejected_and_a_good_one_accepted(self):
        import block as block_mod
        c = Chain()
        bad = make_tx(1, 0, 0, c.state, fee=FEE, memo="no request memo",
                      outputs_override=[{"to": ESC, "amount": LOCK}])
        good = make_tx(1, 0, 0, c.state, fee=FEE, memo=REQ_MEMO,
                       outputs_override=[{"to": ESC, "amount": LOCK}])
        blk = lambda t: make_block(1, c.cs.tip["hash"], [t])
        ok, err = block_mod._apply_transactions(blk(bad), c.state.snapshot())
        assert not ok and "escrow" in err
        assert block_mod._apply_transactions(blk(good), c.state.snapshot()) == (True, None)


class TestActivation:
    """Before the activation height the chain behaves as it always has."""

    def _at(self, monkeypatch, height):
        import params
        monkeypatch.setattr(params, "GAS_LOCK_ACTIVATION_HEIGHT", height)

    def test_a_payment_to_escrow_before_activation_is_an_ordinary_transfer(self, monkeypatch):
        self._at(monkeypatch, 10)
        c = Chain()
        bad = make_tx(1, 0, 0, c.state, fee=FEE, memo="no request memo",
                      outputs_override=[{"to": ESC, "amount": 5}])
        assert tx_mod.validate(bad, c.state)[0]               # fine, as on the old chain
        c.mine([bad])
        assert c.state.get_balance(ESC) == 5 and c.state.escrows == {}

    def test_nothing_is_tracked_or_settled_before_activation(self, monkeypatch):
        self._at(monkeypatch, 10)
        c = Chain()
        c.ask()                                               # a proper request, too early
        c.mine_until(8)
        assert c.state.escrows == {} and c.state.get_balance(ESC) == LOCK
        assert c.state.get_balance(address(1)) == START - LOCK - FEE     # never refunded

    def test_the_first_block_that_enforces_it_is_the_activation_height(self, monkeypatch):
        self._at(monkeypatch, 5)
        c = Chain()
        c.mine_until(3)
        bad = make_tx(1, 0, 0, c.state, fee=FEE, memo="nope",
                      outputs_override=[{"to": ESC, "amount": LOCK}])
        # state is at height 3, so this would join block 4: still the old rules
        assert tx_mod.validate(bad, c.state)[0]
        c.mine_until(4)
        # now it would join block 5: the lock rules apply
        ok, err = tx_mod.validate(bad, c.state)
        assert not ok and "escrow" in err

    def test_a_request_in_the_first_enforced_block_is_locked(self, monkeypatch):
        self._at(monkeypatch, 5)
        c = Chain()
        c.mine_until(4)
        c.ask()                                               # lands in block 5
        assert c.cs.height == 5 and c.escrow is not None

    def test_the_default_is_ahead_of_the_chain_at_the_time_it_was_chosen(self):
        import importlib
        import params
        importlib.reload(params)
        assert params.GAS_LOCK_ACTIVATION_HEIGHT > 25_644 + 5_000
