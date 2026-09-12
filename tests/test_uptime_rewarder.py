"""
Unit tests for uptime_rewarder.py

Covers the budget arithmetic, the eligibility filters, the payout split,
and the refund path for a payout that never confirmed.

This module had no tests at all until now, which is worth saying out loud:
it is the one place in the node that spends the operator's coins without
anybody pressing a button, and its budget is the only thing bounding how
much it can spend.

No network, no real VDF, no disk beyond a temp state file.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uptime_rewarder as rewarder_mod
from uptime_rewarder import UptimeRewarder
from params import TICKS_PER_LAPSE
from tests.fixtures import address


class _FakeState:
    def __init__(self, balances=None):
        self._b = dict(balances or {})

    def get_balance(self, addr):
        return self._b.get(addr, 0)


class _FakeView:
    def __init__(self, chain, balances):
        self.chain = chain
        self.height = chain[-1]["height"] if chain else 0
        self.state = _FakeState(balances)


class _FakeStorage:
    def __init__(self, confirmed=()):
        self._confirmed = set(confirmed)

    def get_tx_height(self, tx_hash):
        return 1 if tx_hash in self._confirmed else None


class _FakeMempool:
    def get(self, tx_hash):
        return None


class _FakeNode:
    """Enough of Node for the rewarder: a view, storage, a mempool, an
    address, the announced-address set, and a signing/submitting path that
    records rather than sends."""

    def __init__(self, addr, balances=None, chain=None, alive=(), confirmed=(),
                 submit_ok=True):
        self.addr = addr
        self.view = _FakeView(chain or [{"height": 0, "builder": None}], balances or {})
        self.storage = _FakeStorage(confirmed)
        self.mempool = _FakeMempool()
        self._alive = set(alive)
        self.submitted = []
        self.announced = 0
        self._submit_ok = submit_ok

    def announce_alive(self):
        self.announced += 1

    def active_addresses(self, window_seconds):
        return set(self._alive)

    def build_and_sign_tx_internal(self, outputs, fee=0, memo=""):
        return {"outputs": outputs, "fee": fee, "from": self.addr}, fee

    def submit_tx_from_api(self, tx_dict, timeout=5):
        self.submitted.append(tx_dict)
        if not self._submit_ok:
            return False, "rejected for the test"
        return True, "txhash%d" % len(self.submitted)


def _chain(builders):
    """A chain whose blocks were built by the given addresses in order."""
    return [{"height": i, "builder": b} for i, b in enumerate(builders)]


def make(tmp_path, **kw):
    node = _FakeNode(kw.pop("addr", address(0)), **{
        k: kw.pop(k) for k in list(kw) if k in
        ("balances", "chain", "alive", "confirmed", "submit_ok")})
    r = UptimeRewarder(node, pool=None,
                       state_file=str(tmp_path / "rewards.json"),
                       budget_lapse=kw.pop("budget_lapse", 0),
                       **kw)
    return r, node


class TestBudget:
    def test_a_fresh_node_pays_nothing(self, tmp_path):
        r, node = make(tmp_path, alive={address(1)})
        r.run_once()
        assert node.submitted == []
        assert r.status()["remaining_ticks"] == 0

    def test_the_budget_is_what_was_set(self, tmp_path):
        r, _ = make(tmp_path, budget_lapse=10)
        assert r.status()["remaining_ticks"] == 10 * TICKS_PER_LAPSE

    def test_adjusting_moves_it_and_floors_at_zero(self, tmp_path):
        r, _ = make(tmp_path, budget_lapse=10)
        r.adjust_budget(5)
        assert r.status()["remaining_ticks"] == 15 * TICKS_PER_LAPSE
        r.adjust_budget(-999)
        assert r.status()["remaining_ticks"] == 0

    def test_the_budget_survives_a_restart(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=7)
        again = UptimeRewarder(node, pool=None,
                               state_file=str(tmp_path / "rewards.json"))
        assert again.status()["remaining_ticks"] == 7 * TICKS_PER_LAPSE

    def test_a_payout_never_exceeds_the_remaining_budget(self, tmp_path):
        # The budget is the only bound on what this spends unattended.
        r, node = make(tmp_path, budget_lapse=1,
                       balances={address(0): 10_000 * TICKS_PER_LAPSE},
                       alive={address(1), address(2)})
        before = r.status()["remaining_ticks"]
        r.run_once()
        sent = sum(o["amount"] for o in node.submitted[0]["outputs"])
        assert sent <= before
        assert r.status()["remaining_ticks"] == before - sent

    def test_it_cannot_spend_more_than_the_wallet_holds(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=1000,
                       balances={address(0): 5},   # five ticks, enormous budget
                       alive={address(1)})
        r.run_once()
        if node.submitted:
            assert sum(o["amount"] for o in node.submitted[0]["outputs"]) <= 5


class TestAnnouncing:
    def test_it_announces_every_cycle_even_with_no_budget(self, tmp_path):
        # Being payable and choosing to pay are separate concerns.
        r, node = make(tmp_path, budget_lapse=0)
        r.run_once()
        r.run_once()
        assert node.announced == 2

    def test_a_failure_to_announce_does_not_stop_the_payout(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        node.announce_alive = lambda: (_ for _ in ()).throw(RuntimeError("no peers"))
        r.run_once()
        assert node.submitted, "an announce failure must not cost the cycle"


class TestEligibility:
    def _paid(self, node):
        return {o["to"] for o in node.submitted[0]["outputs"]} if node.submitted else set()

    def test_announced_addresses_are_paid(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1), address(2)})
        r.run_once()
        assert self._paid(node) == {address(1), address(2)}

    def test_we_never_pay_ourselves(self, tmp_path):
        # Our own balance is deliberately *below* the budget here, so the
        # balance filter cannot mask this: self-exclusion is the only thing
        # that can keep us out of our own payout.
        r, node = make(tmp_path, budget_lapse=1000,
                       balances={address(0): 50 * TICKS_PER_LAPSE},
                       alive={address(0), address(1)})
        r.run_once()
        assert node.submitted, "precondition: a payout actually happened"
        assert address(0) not in self._paid(node)
        assert self._paid(node) == {address(1)}

    def test_a_node_winning_more_than_us_is_skipped(self, tmp_path):
        # It can mine fine on its own; the budget is for those who cannot.
        chain = _chain([None, address(1), address(1), address(1)])
        r, node = make(tmp_path, budget_lapse=10, chain=chain,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1), address(2)})
        r.run_once()
        assert self._paid(node) == {address(2)}

    def test_a_node_already_richer_than_the_budget_is_skipped(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=1,
                       balances={address(0): 1000 * TICKS_PER_LAPSE,
                                 address(1): 500 * TICKS_PER_LAPSE},
                       alive={address(1), address(2)})
        r.run_once()
        assert self._paid(node) == {address(2)}

    def test_a_malformed_announced_address_is_skipped(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={"not.an.address", address(1)})
        r.run_once()
        assert self._paid(node) == {address(1)}

    def test_nobody_eligible_sends_nothing(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE}, alive=set())
        r.run_once()
        assert node.submitted == []


class TestSplit:
    def test_the_pool_is_split_equally(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=100,
                       balances={address(0): 10_000 * TICKS_PER_LAPSE},
                       alive={address(1), address(2), address(3)})
        r.run_once()
        amounts = {o["amount"] for o in node.submitted[0]["outputs"]}
        assert len(amounts) == 1, "every recipient gets the same share"

    def test_a_pool_rounding_to_zero_sends_nothing(self, tmp_path):
        # Otherwise the node signs and broadcasts a transaction paying
        # nobody anything, which tx.validate rejects for zero amounts.
        r, node = make(tmp_path, budget_lapse=0,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.adjust_budget(0.0000001)          # a few ticks; the hourly slice rounds to 0
        r.run_once()
        assert node.submitted == []

    def test_a_share_rounding_to_zero_sends_nothing(self, tmp_path):
        # A pool that survives rounding but cannot be split into whole
        # ticks across this many recipients.
        r, node = make(tmp_path, budget_lapse=1000,
                       balances={address(0): 3},        # three ticks to share out
                       alive={address(i) for i in range(1, 6)})
        r.run_once()
        assert node.submitted == []

    def test_the_payout_carries_no_fee(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.run_once()
        assert node.submitted[0]["fee"] == 0


class TestPendingAndRefund:
    def test_a_confirmed_payout_stays_debited(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.run_once()
        spent = 10 * TICKS_PER_LAPSE - r.status()["remaining_ticks"]
        assert spent > 0
        node.storage = _FakeStorage(confirmed={"txhash1"})
        node._alive = set()               # nothing to pay this cycle
        r.run_once()
        assert r.status()["remaining_ticks"] == 10 * TICKS_PER_LAPSE - spent
        assert r.status()["pending"] is None

    def test_an_unconfirmed_payout_is_refunded(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.run_once()
        node._alive = set()               # storage still says unconfirmed
        r.run_once()
        assert r.status()["remaining_ticks"] == 10 * TICKS_PER_LAPSE
        assert r.status()["pending"] is None

    def test_sitting_in_the_mempool_does_not_count_as_confirmed(self, tmp_path):
        # Only a block confirms. This read the mempool too and recorded a
        # still-pending payout as having gone through, never refunding it.
        r, node = make(tmp_path, budget_lapse=10,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.run_once()
        node.mempool.get = lambda h: {"still": "pending"}
        node._alive = set()
        r.run_once()
        assert r.status()["remaining_ticks"] == 10 * TICKS_PER_LAPSE

    def test_a_rejected_payout_leaves_the_budget_alone(self, tmp_path):
        r, node = make(tmp_path, budget_lapse=10, submit_ok=False,
                       balances={address(0): 1000 * TICKS_PER_LAPSE},
                       alive={address(1)})
        r.run_once()
        assert r.status()["remaining_ticks"] == 10 * TICKS_PER_LAPSE
        assert r.status()["pending"] is None


class TestStateFile:
    def test_an_unreadable_state_file_reinitialises(self, tmp_path):
        path = tmp_path / "rewards.json"
        path.write_text("{not json")
        node = _FakeNode(address(0))
        r = UptimeRewarder(node, pool=None, state_file=str(path), budget_lapse=3)
        assert r.status()["remaining_ticks"] == 3 * TICKS_PER_LAPSE

    def test_a_stale_enabled_flag_is_dropped(self, tmp_path):
        path = tmp_path / "rewards.json"
        path.write_text(json.dumps({"remaining_ticks": 5, "enabled": True}))
        node = _FakeNode(address(0))
        r = UptimeRewarder(node, pool=None, state_file=str(path))
        assert "enabled" not in r.status()
        assert r.status()["remaining_ticks"] == 5

    def test_writing_is_atomic(self, tmp_path):
        # os.replace, so a crash mid-write cannot leave a half-written file
        # where the budget lives.
        r, _ = make(tmp_path, budget_lapse=4)
        r.adjust_budget(1)
        assert not (tmp_path / "rewards.json.tmp").exists()
        assert json.loads((tmp_path / "rewards.json").read_text())["remaining_ticks"] \
            == 5 * TICKS_PER_LAPSE
