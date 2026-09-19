"""Trust scoring, and specifically the Sybil cost.

The property under test throughout: standing must not be transferable to
a fresh address. A reputation you can regenerate for free is not a
reputation, which is the hole the first version of this had and the
reason Bisq declines to run one at all.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import storage as storage_mod
import trade_storage
import trust
from trade_storage import Trade, TRADE_COMPLETED, TRADE_ABANDONED


LAPSE = 100_000_000
DAY = 86_400

_session_counter = [0]


def make_trade(peer_addr, status, my_addr="me", lapse_total=50 * LAPSE,
              session_id=None):
    """A Trade row standing in for what used to be a record_completed/
    record_abandonment call: local standing is now derived straight from
    these (see trust.local_tally), so this is the one thing a test needs
    to set up to control it."""
    if session_id is None:
        _session_counter[0] += 1
        session_id = f"s-{_session_counter[0]}"
    now = time.time()
    return Trade.create(
        session_id=session_id, order_id="o1", role="taker",
        my_lapse_addr=my_addr, my_xlm_addr="GME", peer_lapse_addr=peer_addr,
        peer_xlm_addr="GPEER", i_send="lapse", lapse_total=lapse_total,
        xlm_total=1, increment_count=1, confirm_depth=2, status=status,
        created_at=now, updated_at=now)


@pytest.fixture(autouse=True)
def fresh_db():
    storage_mod.db.init(":memory:")
    storage_mod.db.connect(reuse_if_open=True)
    storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
    storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
    trade_storage._initialised = True
    yield
    storage_mod.db.close()


AGED = trust.AGE_FULL_BLOCKS
FUNDED = trust.STAKE_FULL_TICKS


class TestSybilCost:
    """The centre of the design: history alone buys nothing."""

    def test_fresh_address_scores_zero_however_good_its_record(self):
        now = time.time()
        assert trust.score(completed_count=500, completed_ticks=10_000 * LAPSE,
                           abandoned_count=0, last_completed_at=now,
                           address_age_blocks=0, balance_ticks=FUNDED,
                           now=now) == 0.0

    def test_empty_address_scores_zero_however_old(self):
        now = time.time()
        assert trust.score(completed_count=500, completed_ticks=10_000 * LAPSE,
                           abandoned_count=0, last_completed_at=now,
                           address_age_blocks=AGED, balance_ticks=0,
                           now=now) == 0.0

    def test_both_age_and_stake_are_required(self):
        now = time.time()
        args = dict(completed_count=10, completed_ticks=100 * LAPSE,
                    abandoned_count=0, last_completed_at=now, now=now)
        neither = trust.score(address_age_blocks=0, balance_ticks=0, **args)
        age_only = trust.score(address_age_blocks=AGED, balance_ticks=0, **args)
        stake_only = trust.score(address_age_blocks=0, balance_ticks=FUNDED, **args)
        both = trust.score(address_age_blocks=AGED, balance_ticks=FUNDED, **args)
        assert neither == age_only == stake_only == 0.0
        assert both > 0.0

    def test_age_below_the_floor_counts_for_nothing(self):
        assert trust.stake_component(trust.AGE_FLOOR_BLOCKS - 1, FUNDED) == 0.0

    def test_stake_component_is_bounded(self):
        assert trust.stake_component(AGED * 100, FUNDED * 1000) <= 1.0

    def test_stake_rises_with_both_inputs(self):
        half_age = (trust.AGE_FLOOR_BLOCKS + AGED) // 2
        assert trust.stake_component(AGED, FUNDED) > \
               trust.stake_component(half_age, FUNDED)
        assert trust.stake_component(AGED, FUNDED) > \
               trust.stake_component(AGED, FUNDED // 2)


class TestHistory:
    def test_no_trades_is_zero(self):
        assert trust.history_component(0, 0, 0) == 0.0

    def test_completed_trades_earn_standing(self):
        now = time.time()
        assert trust.history_component(1, 10 * LAPSE, now, now=now) > 0

    def test_volume_has_diminishing_returns(self):
        now = time.time()
        small = trust.history_component(1, 1 * LAPSE, now, now=now)
        huge = trust.history_component(1, 10_000 * LAPSE, now, now=now)
        assert huge > small
        assert huge < small * 1000, "volume should be sublinear"

    def test_count_has_diminishing_returns(self):
        now = time.time()
        few = trust.history_component(2, 100 * LAPSE, now, now=now)
        many = trust.history_component(200, 100 * LAPSE, now, now=now)
        assert many > few
        assert many < few * 100

    def test_standing_decays_with_time(self):
        now = time.time()
        fresh = trust.history_component(10, 100 * LAPSE, now, now=now)
        stale = trust.history_component(
            10, 100 * LAPSE, now - trust.DECAY_HALFLIFE_SECONDS, now=now)
        assert stale == pytest.approx(fresh / 2, rel=0.01)

    def test_a_month_off_does_not_reset_standing(self):
        now = time.time()
        fresh = trust.history_component(10, 100 * LAPSE, now, now=now)
        month = trust.history_component(10, 100 * LAPSE, now - 30 * DAY, now=now)
        assert month > fresh * 0.5


class TestSlashing:
    def test_one_abandonment_zeroes_the_score(self):
        now = time.time()
        assert trust.score(completed_count=1000, completed_ticks=10_000 * LAPSE,
                           abandoned_count=1, last_completed_at=now,
                           address_age_blocks=AGED, balance_ticks=FUNDED,
                           now=now) == 0.0

    def test_slashing_is_not_undone_by_more_trades(self):
        """As long as an abandoned Trade row is still sitting there
        unresolved, not just once, historically, in the past."""
        make_trade("bad.peer", TRADE_ABANDONED, session_id="session-x")
        for _ in range(50):
            make_trade("bad.peer", TRADE_COMPLETED, lapse_total=100 * LAPSE)
        assert trust.get_detail("bad.peer", AGED, FUNDED)["score"] == 0.0

    def test_abandonment_records_its_evidence(self):
        make_trade("bad.peer", TRADE_ABANDONED, session_id="session-abc")
        detail = trust.get_detail("bad.peer", AGED, FUNDED)
        assert detail["abandoned_count"] == 1
        assert detail["last_abandon_session"] == "session-abc"

    def test_recovery_requires_a_new_address(self):
        """Which is the cost that makes the number mean anything."""
        make_trade("bad.peer", TRADE_ABANDONED)
        make_trade("fresh.peer", TRADE_COMPLETED, lapse_total=100 * LAPSE)
        assert trust.get_detail("bad.peer", AGED, FUNDED)["score"] == 0.0
        assert trust.get_detail("fresh.peer", AGED, FUNDED)["score"] > 0.0

    def test_a_late_settlement_lifts_the_slash(self):
        """The whole point of deriving this from Trade.status rather than
        an incremented counter: nothing needs reversing, a status flip
        (see swap_engine.Engine.recheck_abandoned) is the entire fix."""
        trade = make_trade("bad.peer", TRADE_ABANDONED, session_id="s-late")
        assert trust.get_detail("bad.peer", AGED, FUNDED)["score"] == 0.0

        trade.status = TRADE_COMPLETED
        trade.save()
        detail = trust.get_detail("bad.peer", AGED, FUNDED)
        assert detail["abandoned_count"] == 0
        assert detail["score"] > 0.0


class TestRecords:
    def test_unknown_peer_scores_zero(self):
        assert trust.get_detail("never.seen", AGED, FUNDED)["score"] == 0.0

    def test_unknown_peer_detail_is_marked_unknown(self):
        assert trust.get_detail("never.seen")["known"] is False

    def test_completed_accumulates(self):
        make_trade("peer", TRADE_COMPLETED, lapse_total=10 * LAPSE)
        make_trade("peer", TRADE_COMPLETED, lapse_total=5 * LAPSE)
        detail = trust.local_tally("peer")
        assert detail["completed_count"] == 2
        assert detail["completed_lapse"] == 15 * LAPSE

    def test_detail_explains_the_score(self):
        """A score with no stated reason is a verdict, not information."""
        make_trade("peer", TRADE_COMPLETED)
        detail = trust.get_detail("peer", AGED, FUNDED)
        assert detail["history"] > 0
        assert detail["stake"] > 0
        assert detail["score"] == pytest.approx(
            detail["history"] * detail["stake"], rel=1e-6)

class FakeStorage:
    def __init__(self, heights_by_addr=None):
        self.heights_by_addr = heights_by_addr or {}

    def get_tx_heights_for_addr(self, addr):
        return self.heights_by_addr.get(addr, [])


class FakeState:
    def __init__(self, balances=None):
        self.balances = balances or {}

    def get_balance(self, addr):
        return self.balances.get(addr, 0)


class FakeView:
    def __init__(self, height, balances=None):
        self.chain = [{"height": height}]
        self.state = FakeState(balances)


class FakeNode:
    """Just enough of a running node for the chain-facts helpers below:
    a tip height, a balance table, and an address-history index."""

    def __init__(self, addr, height=0, heights_by_addr=None, balances=None):
        self.addr = addr
        self.storage = FakeStorage(heights_by_addr)
        self.view = FakeView(height, balances)


class TestAddressAgeBlocks:
    def test_never_seen_address_is_age_zero(self):
        node = FakeNode("me", height=1000)
        assert trust.address_age_blocks(node, "stranger") == 0

    def test_age_is_blocks_since_first_seen(self):
        node = FakeNode("me", height=1000,
                        heights_by_addr={"peer": [(400, "h1"), (600, "h2")]})
        assert trust.address_age_blocks(node, "peer") == 600

    def test_age_is_never_negative(self):
        """A height read mid-reorg must not report negative age."""
        node = FakeNode("me", height=100, heights_by_addr={"peer": [(500, "h")]})
        assert trust.address_age_blocks(node, "peer") == 0


class TestMutualScores:
    """Feeds swap.opening_mover, so both halves have to be derivable from
    data neither side can lie about: shared settlement history and public
    stake, never anything the counterparty merely claims about itself."""

    def test_strangers_score_zero_both_ways(self):
        node = FakeNode("me", height=AGED, balances={"peer": FUNDED})
        mine, theirs = trust.mutual_scores(node, "peer")
        assert mine == 0.0
        assert theirs == 0.0

    def test_shared_history_is_weighted_by_each_sides_own_stake(self):
        """The completed-trade component is the same shared fact either
        way; only whose stake it is multiplied by differs."""
        make_trade("peer", TRADE_COMPLETED)
        node = FakeNode(
            "me", height=AGED,
            heights_by_addr={"peer": [(0, "h")], "me": [(0, "h")]},
            balances={"peer": FUNDED, "me": FUNDED // 4})
        mine, theirs = trust.mutual_scores(node, "peer")
        # Same history component, different stake multiplier, so the two
        # scores move together but are not required to be equal.
        assert mine > 0.0
        assert theirs > 0.0
        assert mine != theirs

    def test_richer_peer_scores_higher_from_my_side(self):
        make_trade("peer", TRADE_COMPLETED)
        node = FakeNode(
            "me", height=AGED,
            heights_by_addr={"peer": [(0, "h")], "me": [(0, "h")]},
            balances={"peer": FUNDED, "me": FUNDED})
        mine, _theirs = trust.mutual_scores(node, "peer")

        node_poor_peer = FakeNode(
            "me", height=AGED,
            heights_by_addr={"peer": [(0, "h")], "me": [(0, "h")]},
            balances={"peer": FUNDED // 100, "me": FUNDED})
        mine_poor, _ = trust.mutual_scores(node_poor_peer, "peer")
        assert mine > mine_poor

    def test_a_recorded_abandonment_zeroes_both_sides(self):
        """This node's own record of the peer having abandoned it is the
        only signal either side of the pair can act on; there is no
        channel carrying the reverse (see the docstring on mutual_scores),
        so it is applied to both rather than only to my_trust_of_peer."""
        make_trade("peer", TRADE_COMPLETED)
        make_trade("peer", TRADE_ABANDONED)
        node = FakeNode(
            "me", height=AGED,
            heights_by_addr={"peer": [(0, "h")], "me": [(0, "h")]},
            balances={"peer": FUNDED, "me": FUNDED})
        mine, theirs = trust.mutual_scores(node, "peer")
        assert mine == 0.0
        assert theirs == 0.0

    def test_feeds_opening_mover_consistently_from_both_perspectives(self):
        """The taker and the maker each call this from their own node
        about the other, and must land on complementary answers without
        exchanging anything: swap it, the caller becomes the peer.

        Both nodes derive the same completed-trade count for the other's
        address, which is the property that makes this work at all: a
        jointly-completed trade leaves each side its own Trade row for
        it, since each side only completes its own once its own inbound
        leg actually settled (see swap_engine._complete). Simulated here
        as two Trade rows in one shared test database, one per node's
        own local view (my_lapse_addr differs, peer_lapse_addr is what
        local_tally actually keys off).
        """
        import swap
        make_trade("peer", TRADE_COMPLETED, my_addr="taker")   # taker's own record of maker
        make_trade("taker", TRADE_COMPLETED, my_addr="peer")   # maker's own record of taker
        taker_node = FakeNode(
            "taker", height=AGED,
            heights_by_addr={"peer": [(0, "h")], "taker": [(0, "h")]},
            balances={"peer": FUNDED, "taker": FUNDED // 10})
        taker_i_open = swap.opening_mover(*trust.mutual_scores(taker_node, "peer"))

        maker_node = FakeNode(
            "peer", height=AGED,
            heights_by_addr={"taker": [(0, "h")], "peer": [(0, "h")]},
            balances={"taker": FUNDED // 10, "peer": FUNDED})
        maker_i_open = swap.opening_mover(*trust.mutual_scores(maker_node, "taker"))

        assert taker_i_open is True    # taker is the poorer, less established side
        assert maker_i_open is False   # and the maker correctly agrees it is not maker


class TestNegativeAndOddInputs:
    def test_negative_volume_does_not_create_standing(self):
        now = time.time()
        assert trust.history_component(1, -1000, now, now=now) == 0.0

    def test_negative_balance_scores_zero_stake(self):
        assert trust.stake_component(AGED, -500) == 0.0

    def test_negative_age_scores_zero_stake(self):
        assert trust.stake_component(-10, FUNDED) == 0.0


# ---------------------------------------------------------------------------
# Network-sourced trust: verified step receipts about an address this
# node never itself traded with
# ---------------------------------------------------------------------------

def make_receipt(receipt_id, addr_a, addr_b, session_id, outcome="settled",
                 asset="lapse", amount=LAPSE, verified=True, reporter=None):
    return trade_storage.StepReceipt.create(
        receipt_id=receipt_id, order_id="o1", session_id=session_id, n=1,
        reporter_lapse_addr=reporter or addr_a, addr_a=addr_a, addr_b=addr_b,
        asset=asset, from_addr=addr_a, to_addr=addr_b, amount=amount,
        memo="m", outcome=outcome, tx_hash=("tx1" if outcome == "settled" else ""),
        deadline_height=0, checked_at_height=0, pubkey="pk", signature="sig",
        received_at=time.time(), verified=verified)


class TestNetworkTally:
    def test_unverified_receipts_are_not_counted(self):
        make_receipt("r1", "a", "b", "s1", verified=None)
        abandoned, count, lapse_amt, _last = trust._network_tally("b")
        assert (abandoned, count, lapse_amt) == (0, 0, 0)

    def test_verified_settled_receipt_counts_toward_completed(self):
        make_receipt("r1", "a", "b", "s1", outcome="settled", amount=5 * LAPSE)
        abandoned, count, lapse_amt, _last = trust._network_tally("b")
        assert (abandoned, count, lapse_amt) == (0, 1, 5 * LAPSE)

    def test_verified_missed_receipt_counts_as_abandonment(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        abandoned, count, _lapse, _last = trust._network_tally("b")
        assert abandoned == 1

    def test_session_already_known_locally_is_not_double_counted(self):
        trade_storage.Trade.create(
            session_id="s1", order_id="o1", role="taker",
            my_lapse_addr="me", my_xlm_addr="GME", peer_lapse_addr="b",
            peer_xlm_addr="GB", i_send="lapse", lapse_total=LAPSE, xlm_total=10_000_000,
            increment_count=2, confirm_depth=2, status="completed",
            created_at=time.time(), updated_at=time.time())
        make_receipt("r1", "a", "b", "s1", outcome="settled")
        abandoned, count, lapse_amt, _last = trust._network_tally("b")
        assert (abandoned, count, lapse_amt) == (0, 0, 0)

    def test_get_detail_without_node_ignores_network_receipts(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        detail = trust.get_detail("b")
        assert detail["abandoned_count"] == 0

    def test_get_detail_with_node_folds_in_network_receipts(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        detail = trust.get_detail("b", node=object())
        assert detail["abandoned_count"] == 1
        assert detail["network_abandoned_count"] == 1
        assert detail["score"] == 0.0
        assert detail["known"] is True

    def test_network_and_local_history_add_together(self):
        make_trade("b", TRADE_COMPLETED, lapse_total=6 * LAPSE)
        make_trade("b", TRADE_COMPLETED, lapse_total=4 * LAPSE)
        make_receipt("r1", "a", "b", "s1", outcome="settled", amount=3 * LAPSE)
        detail = trust.get_detail("b", node=object())
        assert detail["completed_count"] == 3
        assert detail["completed_lapse"] == 13 * LAPSE
        assert detail["network_completed_count"] == 1


class TestVerifyPendingReceipts:
    def test_resolves_pending_receipts_up_to_the_limit(self, monkeypatch):
        import swap_engine
        make_receipt("r1", "a", "b", "s1", verified=None)
        make_receipt("r2", "a", "b", "s2", verified=None)
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: True)
        resolved = trust.verify_pending_receipts(node=object(), limit=1)
        assert resolved == 1
        verified_count = sum(1 for r in trade_storage.StepReceipt.select()
                             if r.verified is True)
        assert verified_count == 1

    def test_unreachable_leaves_it_pending(self, monkeypatch):
        import swap_engine
        make_receipt("r1", "a", "b", "s1", verified=None)

        def boom(engine, r):
            raise swap_engine.Unreachable("down")

        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain", boom)
        resolved = trust.verify_pending_receipts(node=object(), limit=10)
        assert resolved == 0
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        assert row.verified is None

    def test_already_verified_missed_claims_are_rechecked(self, monkeypatch):
        """Unlike settled, missed is not a monotonic fact: a late, honest
        payment can falsify it at any time after it was first true. This
        is the network-wide half of redemption, matching
        swap_engine.Engine.recheck_abandoned's local half."""
        import swap_engine
        make_receipt("r1", "a", "b", "s1", outcome="missed", verified=True)
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: False)  # the payment showed up
        resolved = trust.verify_pending_receipts(node=object(), limit=10)
        assert resolved == 1
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        assert row.verified is False
        # And the moment it flips, trust stops counting it.
        assert trust._network_tally("b")[0] == 0

    def test_already_verified_settled_claims_are_not_rechecked(self, monkeypatch):
        """Settled is monotonic, so it is not worth the extra chain call
        every pass: only unverified and previously-missed claims are
        candidates."""
        import swap_engine
        make_receipt("r1", "a", "b", "s1", outcome="settled", verified=True)
        calls = []
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: calls.append(r.receipt_id) or True)
        trust.verify_pending_receipts(node=object(), limit=10)
        assert calls == []

    def test_unverified_receipts_take_priority_over_rechecks(self, monkeypatch):
        import swap_engine
        make_receipt("r1", "a", "b", "s1", outcome="missed", verified=True)
        make_receipt("r2", "a", "b", "s2", outcome="settled", verified=None)
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: True)
        resolved = trust.verify_pending_receipts(node=object(), limit=1)
        assert resolved == 1
        row2 = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r2")
        assert row2.verified is True
