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


class TestNegativeAndOddInputs:
    def test_negative_volume_does_not_create_standing(self):
        now = time.time()
        assert trust.history_component(1, -1000, now, now=now) == 0.0

    def test_negative_balance_scores_zero_stake(self):
        assert trust.stake_component(AGED, -500) == 0.0

    def test_negative_age_scores_zero_stake(self):
        assert trust.stake_component(-10, FUNDED) == 0.0
