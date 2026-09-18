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
from trade_storage import PeerRecord


LAPSE = 100_000_000
DAY = 86_400


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
        trust.record_abandonment("bad.peer", "session-x")
        for _ in range(50):
            trust.record_completed("bad.peer", 100 * LAPSE)
        assert trust.get_score("bad.peer", AGED, FUNDED) == 0.0

    def test_abandonment_records_its_evidence(self):
        trust.record_abandonment("bad.peer", "session-abc")
        detail = trust.get_detail("bad.peer", AGED, FUNDED)
        assert detail["abandoned_count"] == 1
        assert detail["last_abandon_session"] == "session-abc"

    def test_recovery_requires_a_new_address(self):
        """Which is the cost that makes the number mean anything."""
        trust.record_abandonment("bad.peer")
        trust.record_completed("fresh.peer", 100 * LAPSE)
        assert trust.get_score("bad.peer", AGED, FUNDED) == 0.0
        assert trust.get_score("fresh.peer", AGED, FUNDED) > 0.0


class TestRecords:
    def test_unknown_peer_scores_zero(self):
        assert trust.get_score("never.seen", AGED, FUNDED) == 0.0

    def test_unknown_peer_detail_is_marked_unknown(self):
        assert trust.get_detail("never.seen")["known"] is False

    def test_completed_accumulates(self):
        trust.record_completed("peer", 10 * LAPSE)
        trust.record_completed("peer", 5 * LAPSE)
        row = PeerRecord.get(PeerRecord.lapse_addr == "peer")
        assert row.completed_count == 2
        assert row.completed_lapse == 15 * LAPSE

    def test_detail_explains_the_score(self):
        """A score with no stated reason is a verdict, not information."""
        trust.record_completed("peer", 50 * LAPSE)
        detail = trust.get_detail("peer", AGED, FUNDED)
        assert detail["history"] > 0
        assert detail["stake"] > 0
        assert detail["score"] == pytest.approx(
            detail["history"] * detail["stake"], rel=1e-6)

    def test_all_scores_without_chain_facts_is_zero_not_a_fallback(self):
        """Falling back to counting trades when the chain cannot be read
        would quietly restore the Sybil hole."""
        trust.record_completed("peer", 50 * LAPSE)
        assert trust.all_scores()["peer"] == 0.0

    def test_all_scores_uses_supplied_chain_facts(self):
        trust.record_completed("peer", 50 * LAPSE)
        scores = trust.all_scores(stake_lookup=lambda a: (AGED, FUNDED))
        assert scores["peer"] > 0.0


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


class TestStakeLookupFor:
    def test_returns_age_and_balance(self):
        node = FakeNode("me", height=1000,
                        heights_by_addr={"peer": [(200, "h")]},
                        balances={"peer": 5 * LAPSE})
        lookup = trust.stake_lookup_for(node)
        assert lookup("peer") == (800, 5 * LAPSE)


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
        trust.record_completed("peer", 50 * LAPSE)
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
        trust.record_completed("peer", 50 * LAPSE)
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
        trust.record_completed("peer", 50 * LAPSE)
        trust.record_abandonment("peer")
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

        Both nodes' PeerRecord tables get the same completed-trade count
        under the other's address, which is the property that makes this
        work at all: a jointly-completed trade produces exactly that on
        both sides, since each side only records it once its own inbound
        leg actually settled (see swap_engine._complete).
        """
        import swap
        trust.record_completed("peer", 50 * LAPSE)    # taker's record of maker
        trust.record_completed("taker", 50 * LAPSE)   # maker's record of taker
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
