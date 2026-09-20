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
import swap_engine
import trade_storage
import trust
from trade_storage import Trade, TRADE_COMPLETED, TRADE_STALLED


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


def make_delinquent_trade(peer_addr, my_addr="me", lapse_total=50 * LAPSE,
                          session_id=None):
    """A STALLED Trade plus the one Increment that makes
    swap_engine.is_delinquent actually say so: this node's leg settled,
    long enough ago (deadline_height=1) that any height a test then
    checks against, if it clears ABANDON_AFTER_BLOCKS, counts against
    the peer. Standing here is computed from exactly these rows, never
    from the status alone, so a test exercising it has to set both up."""
    trade = make_trade(peer_addr, TRADE_STALLED, my_addr=my_addr,
                       lapse_total=lapse_total, session_id=session_id)
    trade_storage.Increment.create(
        id=f"{trade.session_id}:1", session_id=trade.session_id, n=1,
        lapse_amount=lapse_total, xlm_amount=1, i_move_first=True,
        out_state=trade_storage.LEG_SETTLED, in_state=trade_storage.LEG_PENDING,
        created_at=time.time(), deadline_height=1)
    return trade


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
# Comfortably past SEASONING_FLOOR_BLOCKS, for a test that wants a
# balance to count as real rather than "just moved in" (see
# trust.stake_component / trust.SEASONING_FLOOR_BLOCKS).
SEASONED = trust.SEASONING_FLOOR_BLOCKS * 10


class TestSybilCost:
    """The centre of the design: history alone buys nothing, and (since
    the standing-floor/seasoning rework) neither does pure stake claimed
    for free or reused in sequence - see TestStandingFloor,
    TestSeasoning and TestDiversityWeighting below for the properties
    added alongside this one."""

    def test_fresh_address_scores_zero_however_good_its_record(self):
        now = time.time()
        assert trust.score(per_counterparty=[(10_000 * LAPSE, 500)],
                           abandoned_count=0, last_completed_at=now,
                           address_age_blocks=0, balance_ticks=FUNDED,
                           blocks_since_last_topup=SEASONED, now=now) == 0.0

    def test_empty_address_scores_zero_however_old(self):
        now = time.time()
        assert trust.score(per_counterparty=[(10_000 * LAPSE, 500)],
                           abandoned_count=0, last_completed_at=now,
                           address_age_blocks=AGED, balance_ticks=0,
                           blocks_since_last_topup=SEASONED, now=now) == 0.0

    def test_both_age_and_stake_are_required(self):
        now = time.time()
        args = dict(per_counterparty=[(100 * LAPSE, 10)],
                    abandoned_count=0, last_completed_at=now,
                    blocks_since_last_topup=SEASONED, now=now)
        neither = trust.score(address_age_blocks=0, balance_ticks=0, **args)
        age_only = trust.score(address_age_blocks=AGED, balance_ticks=0, **args)
        stake_only = trust.score(address_age_blocks=0, balance_ticks=FUNDED, **args)
        both = trust.score(address_age_blocks=AGED, balance_ticks=FUNDED, **args)
        assert neither == age_only == stake_only == 0.0
        assert both > 0.0

    def test_age_below_the_floor_counts_for_nothing(self):
        assert trust.stake_component(
            trust.AGE_FLOOR_BLOCKS - 1, FUNDED, SEASONED) == 0.0

    def test_stake_component_is_bounded(self):
        assert trust.stake_component(AGED * 100, FUNDED * 1000, SEASONED) <= 1.0

    def test_stake_rises_with_both_inputs(self):
        half_age = (trust.AGE_FLOOR_BLOCKS + AGED) // 2
        assert trust.stake_component(AGED, FUNDED, SEASONED) > \
               trust.stake_component(half_age, FUNDED, SEASONED)
        assert trust.stake_component(AGED, FUNDED, SEASONED) > \
               trust.stake_component(AGED, FUNDED // 2, SEASONED)

    def test_balance_below_the_minimum_counts_for_nothing(self):
        """A hard floor, not a ramp from zero: a linear-from-zero curve
        rewards ANY nonzero balance a little, which is exactly backwards
        for Sybil resistance (it makes spreading a fixed budget thin
        across many addresses profitable). Below STAKE_MIN_TICKS must
        score identically to holding nothing at all."""
        assert trust.stake_component(AGED, trust.STAKE_MIN_TICKS - 1, SEASONED) == 0.0
        assert trust.stake_component(AGED, 1, SEASONED) == 0.0
        assert trust.stake_component(AGED, 0, SEASONED) == 0.0

    def test_spreading_a_fixed_budget_thin_is_worse_than_concentrating(self):
        """The point of the hard floor: N addresses each holding
        budget/N must never together outscore one address holding the
        whole budget, or thin-spreading would be the profitable move."""
        budget = 20 * trust.STAKE_MIN_TICKS
        concentrated = trust.stake_component(AGED, budget, SEASONED)
        n = 10
        thin = trust.stake_component(AGED, budget // n, SEASONED)
        assert thin == 0.0 or n * thin < concentrated


class TestHistory:
    """history_component now takes per_counterparty, an iterable of
    (ticks, count) - one entry per distinct address this history is
    drawn from - rather than flat totals, and sums a concave term per
    entry instead of taking one sqrt/log1p of a pooled total. See
    TestDiversityWeighting for why (an earlier, pooled-then-scaled
    version of this passed every test in this class and still let a
    wash-trading ring outscore genuine diversity; these numbers alone
    do not catch that, which is the whole reason that class exists)."""

    def test_no_trades_is_zero(self):
        assert trust.history_component([], 0) == 0.0

    def test_completed_trades_earn_standing(self):
        now = time.time()
        assert trust.history_component([(10 * LAPSE, 1)], now, now=now) > 0

    def test_volume_has_diminishing_returns(self):
        # Kept well under HISTORY_SATURATION_CEILING so the ceiling
        # itself (tested separately below) doesn't mask this check.
        now = time.time()
        small = trust.history_component([(1 * LAPSE, 1)], now, now=now)
        huge = trust.history_component([(4_000 * LAPSE, 1)], now, now=now)
        assert huge > small
        assert huge < small * 1000, "volume should be sublinear"

    def test_count_has_diminishing_returns(self):
        now = time.time()
        few = trust.history_component([(100 * LAPSE, 2)], now, now=now)
        many = trust.history_component([(100 * LAPSE, 100)], now, now=now)
        assert many > few
        assert many < few * 100

    def test_standing_decays_with_time(self):
        now = time.time()
        fresh = trust.history_component([(100 * LAPSE, 10)], now, now=now)
        stale = trust.history_component(
            [(100 * LAPSE, 10)], now - trust.DECAY_HALFLIFE_SECONDS, now=now)
        assert stale == pytest.approx(fresh / 2, rel=0.01)

    def test_a_month_off_does_not_reset_standing(self):
        now = time.time()
        fresh = trust.history_component([(100 * LAPSE, 10)], now, now=now)
        month = trust.history_component([(100 * LAPSE, 10)], now - 30 * DAY, now=now)
        assert month > fresh * 0.5

    def test_saturates_rather_than_growing_unbounded(self):
        """Diversity-weighting (see TestDiversityWeighting) makes faking
        many distinct-looking relationships cost real per-identity setup,
        but says nothing about magnitude: a ring willing to cycle a
        large, genuinely-held balance many times (cheap - the LapseCoin
        leg of a trade carries no mandatory fee, see tx.py) can still
        post an arbitrarily large raw number even after diversity-
        weighting. Unlike swap.exposure_cap_stroops, which already
        refuses to let a raw score push a step past MAX_TRUST_MULTIPLIER
        regardless of magnitude, swap.opening_mover compares two raw
        scores directly with nothing downstream to cap it - so the cap
        has to live here."""
        now = time.time()
        modest = trust.history_component([(50 * LAPSE, 5)], now, now=now)
        enormous = trust.history_component(
            [(200_000 * LAPSE, 200)], now, now=now)
        assert enormous == trust.HISTORY_SATURATION_CEILING
        assert modest < enormous


class TestDiversityWeighting:
    """Two self-owned addresses can loop real, chain-settled trades with
    EACH OTHER indefinitely for near-zero cost (the LapseCoin leg of a
    trade carries no mandatory fee - tx.py, swap legs use fee=0 - and
    the same balance round-trips intact each time rather than being
    spent), building a large, portable completed-trade tally that gets
    presented to a genuine stranger via network-verified receipts. Real
    payments, fake evidence of GENERAL trustworthiness. Summing a
    concave (sqrt x log1p) term per distinct counterparty, rather than
    pooling every counterparty's ticks/count into one sqrt/log1p call,
    is what makes spreading the same resources across more distinct
    partners score higher: sqrt(a) + sqrt(b) > sqrt(a+b) for positive a,
    b. This does not (and cannot) prove two addresses are controlled by
    different people - nothing on-chain can - it only makes faking N
    independent-looking relationships cost the same age+balance+
    seasoning setup as N real ones, closing the specific shortcut of
    reusing the same two keys forever.

    An earlier version of this weighting used mean(per_peer) *
    log1p(distinct_count) instead of summing the per-peer terms
    directly, which looked plausible and was wrong: the mean canceled
    out almost exactly the concentration bonus sqrt's concavity gives a
    single fat relationship, so spreading the SAME total volume/count
    across 20 partners scored LOWER than concentrating it in one -
    backwards from the goal. Caught only by computing both by hand and
    comparing; test_diversifying_beats_concentrating_for_equal_totals
    exists specifically so that regression cannot come back silently.
    """

    def test_diversifying_beats_concentrating_for_equal_totals(self):
        # Kept well under HISTORY_SATURATION_CEILING (50) on both sides -
        # once either hits the ceiling the comparison stops meaning
        # anything, since both would just read 50.
        now = time.time()
        total_ticks, total_count, n = 100 * LAPSE, 30, 10
        one_partner = trust.history_component(
            [(total_ticks, total_count)], now, now=now)
        ten_partners = trust.history_component(
            [(total_ticks // n, total_count // n) for _ in range(n)],
            now, now=now)
        assert one_partner < 50 and ten_partners < 50, \
            "raise the saturation ceiling check first if this fires"
        assert ten_partners > one_partner

    def test_looping_the_same_partner_forever_does_not_buy_diversity(self):
        """distinct_counterparties stuck at 1 caps the diversity effect
        permanently: every additional unit of credit has to come from
        an actually-new counterparty, which costs the full age+balance+
        seasoning setup again (see trust.get_detail)."""
        now = time.time()
        one_partner_looped = trust.history_component(
            [(80 * LAPSE, 10)], now, now=now)
        two_real_partners = trust.history_component(
            [(80 * LAPSE, 10), (80 * LAPSE, 10)], now, now=now)
        assert one_partner_looped < 50 and two_real_partners < 50, \
            "raise the saturation ceiling check first if this fires"
        # A second distinct partner contributing the SAME modest amount
        # the first one did beats looping the first one indefinitely.
        assert two_real_partners > one_partner_looped


class TestSeasoning:
    """stake_component's balance term now requires BOTH a hard floor
    (STAKE_MIN_TICKS) and seasoning (SEASONING_FLOOR_BLOCKS since the
    last significant inbound transfer) before a balance counts as real
    stake. The floor alone stops spreading a fixed budget thin across
    many SIMULTANEOUS addresses; it says nothing about the cheaper
    version of the same attack: bulk-backdating a queue of addresses'
    AGE for free (storage.py's AddrIndex indexes an address the instant
    it appears as sender or ANY recipient of ANY tx, so one multi-output
    transaction can backdate thousands at once), then moving ONE pool of
    real capital through that queue sequentially, funding each address
    seconds before it's used. A live balance snapshot cannot tell that
    apart from a genuinely-held balance; seasoning can, because it asks
    not "how much is here now" but "how long has it been here"."""

    def test_a_balance_that_just_arrived_does_not_count(self):
        assert trust.stake_component(AGED, FUNDED, 0) == 0.0

    def test_the_same_balance_counts_once_seasoned(self):
        assert trust.stake_component(AGED, FUNDED, SEASONED) > 0.0

    def test_never_topped_up_is_treated_as_fully_seasoned(self):
        """A balance with no inbound transfer on record at all can only
        have come from mining a block reward (credited directly via
        state.credit(), never through a transaction - see
        chainstate._apply_builder_reward - so it never shows up in
        AddrIndex for blocks_since_last_significant_topup to find).
        Winning a block is real, sequential, unparallelizable work,
        so treating "nothing to find" as seasoned is correct here,
        just for a different reason than for a plain snapshot."""
        assert trust.stake_component(AGED, FUNDED, float("inf")) > 0.0

    def test_sequential_reuse_of_one_pool_scores_like_a_fresh_address(self):
        """The attack this exists to close: bulk-backdate age for free,
        then cycle ONE small real capital pool through a queue of
        already-aged addresses, funding each right before it's used.
        Without seasoning this scores as real stake (a live snapshot
        cannot tell the difference); with it, it scores exactly like an
        address that was never funded at all."""
        cycled_in_seconds_ago = trust.stake_component(
            AGED, trust.STAKE_MIN_TICKS + LAPSE, blocks_since_last_topup=0)
        never_funded = trust.stake_component(AGED, 0, SEASONED)
        assert cycled_in_seconds_ago == never_funded == 0.0

    def test_actually_parking_the_capital_the_full_window_is_rewarded(self):
        """The attacker who stops cycling and genuinely parks the
        capital for the real duration isn't attacking any more at that
        point - they're paying the same price a real trader would, and
        get the same credit for it."""
        paid_the_real_cost = trust.stake_component(
            AGED, trust.STAKE_MIN_TICKS + LAPSE, SEASONED)
        assert paid_the_real_cost > 0.0


class TestSlashing:
    def test_one_abandonment_zeroes_the_score(self):
        now = time.time()
        assert trust.score(per_counterparty=[(10_000 * LAPSE, 1000)],
                           abandoned_count=1, last_completed_at=now,
                           address_age_blocks=AGED, balance_ticks=FUNDED,
                           blocks_since_last_topup=SEASONED,
                           now=now) == 0.0

    def test_slashing_is_not_undone_by_more_trades(self):
        """As long as the delinquent trade's missing leg is still
        missing, not just once, historically, in the past."""
        make_delinquent_trade("bad.peer", session_id="session-x")
        for _ in range(50):
            make_trade("bad.peer", TRADE_COMPLETED, lapse_total=100 * LAPSE)
        node = FakeNode("me", height=AGED)
        assert trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)["score"] == 0.0

    def test_delinquency_records_its_evidence(self):
        make_delinquent_trade("bad.peer", session_id="session-abc")
        node = FakeNode("me", height=AGED)
        detail = trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)
        assert detail["abandoned_count"] == 1
        assert detail["last_abandon_session"] == "session-abc"

    def test_recovery_requires_a_new_address(self):
        """Which is the cost that makes the number mean anything."""
        make_delinquent_trade("bad.peer")
        make_trade("fresh.peer", TRADE_COMPLETED, lapse_total=100 * LAPSE)
        node = FakeNode("me", height=AGED)
        assert trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)["score"] == 0.0
        assert trust.get_detail("fresh.peer", AGED, FUNDED, SEASONED, node=node)["score"] > 0.0

    def test_a_late_settlement_lifts_the_slash(self):
        """The whole point of computing this fresh from Increment rows
        every time rather than an incremented counter: nothing needs
        reversing, the missing leg simply settling is the entire fix
        (see swap_engine.is_delinquent)."""
        trade = make_delinquent_trade("bad.peer", session_id="s-late")
        node = FakeNode("me", height=AGED)
        assert trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)["score"] == 0.0

        # The missing leg lands late, and the ordinary advance loop
        # (tested in test_swap_engine.py) drives the trade to
        # TRADE_COMPLETED from there; simulated directly here since this
        # is a trust-layer test, not an engine one.
        inc = trade_storage.Increment.get(
            trade_storage.Increment.session_id == trade.session_id)
        inc.in_state = trade_storage.LEG_SETTLED
        inc.save()
        trade.status = TRADE_COMPLETED
        trade.save()
        detail = trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)
        assert detail["abandoned_count"] == 0
        assert detail["score"] > 0.0

    def test_not_yet_delinquent_does_not_slash(self):
        """A stalled trade whose margin has not elapsed by the chain's
        own clock must not already count against the peer, unlike an
        actually-delinquent one, which would zero this same history."""
        make_trade("bad.peer", TRADE_COMPLETED, lapse_total=100 * LAPSE)
        trade = make_delinquent_trade("bad.peer")
        node = FakeNode("me", height=1)   # right at deadline_height, no margin yet
        detail = trust.get_detail("bad.peer", AGED, FUNDED, SEASONED, node=node)
        assert detail["abandoned_count"] == 0
        assert detail["score"] > 0.0
        assert trade.status == TRADE_STALLED  # sanity: still stalled, just not delinquent yet


class TestRecords:
    def test_a_completely_fresh_unfunded_peer_scores_zero(self):
        assert trust.get_detail("never.seen", 0, 0, 0)["score"] == 0.0

    def test_an_unknown_but_established_peer_scores_above_zero(self):
        """The actual fix this whole rework exists for: a peer with no
        trade history at all (never "known" in the trade-evidence sense)
        but real, seasoned age and balance must not tie with a bare
        throwaway at 0.0 any more - see TestSybilCost/TestSeasoning for
        why that tie was exploitable."""
        detail = trust.get_detail("never.seen", AGED, FUNDED, SEASONED)
        assert detail["score"] > 0.0
        # Still correctly "unknown" in the trade-evidence sense: stake
        # buys standing, not a fabricated track record.
        assert detail["known"] is False

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
        detail = trust.get_detail("peer", AGED, FUNDED, SEASONED)
        assert detail["history"] > 0
        assert detail["stake"] > 0
        assert detail["score"] == pytest.approx(
            trust.STANDING_WEIGHT * detail["stake"]
            + detail["history"] * detail["stake"], rel=1e-6)

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
        self.height = height
        # "transactions": [] so blocks_since_last_significant_topup can
        # scan it without a real chain: none of these fixtures name a
        # transaction that actually appears here, so every lookup falls
        # through to "nothing found" (float("inf"), fully seasoned),
        # which is the correct default for a fixture that isn't
        # specifically testing seasoning (see TestSeasoning for the
        # ones that are, via trust.stake_component directly).
        self.chain = [{"height": height, "transactions": []}]
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
        make_delinquent_trade("peer")
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
        assert trust.history_component([(-1000, 1)], now, now=now) == 0.0

    def test_negative_balance_scores_zero_stake(self):
        assert trust.stake_component(AGED, -500, SEASONED) == 0.0

    def test_negative_age_scores_zero_stake(self):
        assert trust.stake_component(-10, FUNDED, SEASONED) == 0.0


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
        abandoned, by_peer, _last = trust._network_tally_by_counterparty("b")
        assert (abandoned, by_peer) == (0, {})

    def test_verified_settled_receipt_counts_toward_completed(self):
        make_receipt("r1", "a", "b", "s1", outcome="settled", amount=5 * LAPSE)
        abandoned, by_peer, _last = trust._network_tally_by_counterparty("b")
        assert abandoned == 0
        assert by_peer == {"a": (5 * LAPSE, 1)}

    def test_verified_missed_receipt_counts_as_abandonment(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        abandoned, _by_peer, _last = trust._network_tally_by_counterparty("b")
        assert abandoned == 1

    def test_session_already_known_locally_is_not_double_counted(self):
        trade_storage.Trade.create(
            session_id="s1", order_id="o1", role="taker",
            my_lapse_addr="me", my_xlm_addr="GME", peer_lapse_addr="b",
            peer_xlm_addr="GB", i_send="lapse", lapse_total=LAPSE, xlm_total=10_000_000,
            increment_count=2, confirm_depth=2, status="completed",
            created_at=time.time(), updated_at=time.time())
        make_receipt("r1", "a", "b", "s1", outcome="settled")
        abandoned, by_peer, _last = trust._network_tally_by_counterparty("b")
        assert (abandoned, by_peer) == (0, {})

    def test_two_sessions_with_the_same_counterparty_are_grouped(self):
        """A trade carries several 'settled' receipts (one per step's
        LAPSE leg), all naming the same counterparty and session; two
        DIFFERENT sessions with that same counterparty must still be
        counted as one counterparty, two sessions - not two counterparty
        entries - for diversity-weighting to mean anything."""
        make_receipt("r1", "a", "b", "s1", outcome="settled", amount=3 * LAPSE)
        make_receipt("r2", "a", "b", "s2", outcome="settled", amount=4 * LAPSE)
        abandoned, by_peer, _last = trust._network_tally_by_counterparty("b")
        assert abandoned == 0
        assert by_peer == {"a": (7 * LAPSE, 2)}

    def test_get_detail_without_node_ignores_network_receipts(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        detail = trust.get_detail("b")
        assert detail["abandoned_count"] == 0

    def test_get_detail_with_node_folds_in_network_receipts(self):
        make_receipt("r1", "a", "b", "s1", outcome="missed")
        # Pre-mark it checked at this exact height so get_detail's own
        # lazy verification pass (see trust._verify_addr_receipts) has
        # nothing left to do and this stays a pure DB-reading test of
        # _network_tally_by_counterparty's folding, not chain verification.
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        row.verified_at_height = 100
        row.save()
        detail = trust.get_detail("b", node=FakeNode("me", height=100))
        assert detail["abandoned_count"] == 1
        assert detail["network_abandoned_count"] == 1
        assert detail["score"] == 0.0
        assert detail["known"] is True

    def test_network_and_local_history_add_together(self):
        make_trade("b", TRADE_COMPLETED, lapse_total=6 * LAPSE)
        make_trade("b", TRADE_COMPLETED, lapse_total=4 * LAPSE)
        make_receipt("r1", "a", "b", "s1", outcome="settled", amount=3 * LAPSE)
        detail = trust.get_detail("b", node=FakeNode("me", height=100))
        assert detail["completed_count"] == 3
        assert detail["completed_lapse"] == 13 * LAPSE
        assert detail["network_completed_count"] == 1


class TestVerifyAddrReceipts:
    """trust._verify_addr_receipts: the lazy, per-address chain check
    that replaced a scheduled background sweep over every receipt on
    file. Called with a bare object() standing in for node, exactly as
    the old sweep's tests did: _lightweight_engine only ever wraps the
    reference, and verify_receipt_against_chain is monkeypatched away
    below, so nothing here ever dereferences it."""

    def test_resolves_every_unverified_receipt_for_the_address(self, monkeypatch):
        make_receipt("r1", "a", "b", "s1", verified=None)
        make_receipt("r2", "a", "b", "s2", verified=None)
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: True)
        checked = trust._verify_addr_receipts(object(), "b", current_height=100)
        assert checked == 2
        assert all(r.verified is True for r in trade_storage.StepReceipt.select())

    def test_a_flood_of_receipts_is_capped_per_call(self, monkeypatch):
        """A reporter can only spend its own MAX_RECEIPTS_PER_REPORTER
        slots, but nothing stops an attacker from doing that from many
        cheaply-generated reporter addresses, all naming one victim.
        This cap is what stops a single trust lookup for that victim
        from paying for a chain call per spammed receipt in one request."""
        extra = 5
        for i in range(trust.MAX_CHAIN_CHECKS_PER_LOOKUP + extra):
            make_receipt(f"r{i}", f"attacker{i}", "victim", f"s{i}",
                        verified=None, reporter=f"attacker{i}")
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: True)
        checked = trust._verify_addr_receipts(object(), "victim", current_height=100)
        assert checked == trust.MAX_CHAIN_CHECKS_PER_LOOKUP
        still_pending = sum(1 for r in trade_storage.StepReceipt.select()
                            if r.verified is None)
        assert still_pending == extra

    def test_unreachable_leaves_it_pending(self, monkeypatch):
        make_receipt("r1", "a", "b", "s1", verified=None)

        def boom(engine, r):
            raise swap_engine.Unreachable("down")

        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain", boom)
        checked = trust._verify_addr_receipts(object(), "b", current_height=100)
        assert checked == 0
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        assert row.verified is None

    def test_already_verified_missed_claims_are_rechecked_at_a_new_height(self, monkeypatch):
        """Unlike settled, missed is not a monotonic fact: a late, honest
        payment can falsify it at any time after it was first true."""
        make_receipt("r1", "a", "b", "s1", outcome="missed", verified=True)
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        row.verified_at_height = 50   # checked once already, at an earlier height
        row.save()
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: False)  # the payment showed up
        checked = trust._verify_addr_receipts(object(), "b", current_height=100)
        assert checked == 1
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        assert row.verified is False
        # And the moment it flips, trust stops counting it.
        assert trust._network_tally_by_counterparty("b")[0] == 0

    def test_missed_claim_is_not_rechecked_twice_at_the_same_height(self, monkeypatch):
        """A page rendered twice inside one block must not pay for the
        same chain lookup twice."""
        make_receipt("r1", "a", "b", "s1", outcome="missed", verified=True)
        row = trade_storage.StepReceipt.get(trade_storage.StepReceipt.receipt_id == "r1")
        row.verified_at_height = 100
        row.save()
        calls = []
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: calls.append(r.receipt_id) or True)
        checked = trust._verify_addr_receipts(object(), "b", current_height=100)
        assert checked == 0
        assert calls == []

    def test_already_verified_settled_claims_are_never_rechecked(self, monkeypatch):
        """Settled is monotonic, so it is not worth the extra chain call:
        only unverified and previously-missed claims are candidates."""
        make_receipt("r1", "a", "b", "s1", outcome="settled", verified=True)
        calls = []
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: calls.append(r.receipt_id) or True)
        trust._verify_addr_receipts(object(), "b", current_height=999_999)
        assert calls == []

    def test_a_caught_false_claim_is_never_rechecked_either(self, monkeypatch):
        """False is permanent whichever outcome it was claiming: a false
        settle can never become true, and a missed claim caught false
        means the payment already exists, which cannot un-happen."""
        make_receipt("r1", "a", "b", "s1", outcome="missed", verified=False)
        calls = []
        monkeypatch.setattr(swap_engine, "verify_receipt_against_chain",
                            lambda engine, r: calls.append(r.receipt_id) or True)
        trust._verify_addr_receipts(object(), "b", current_height=999_999)
        assert calls == []
