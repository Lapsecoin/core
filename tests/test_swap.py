"""Tests for the rules that bound loss in an incremental swap.

These are the numbers that decide how much money is exposed when a
counterparty walks away, so they get tested for the properties that
matter rather than for a few sample values: no step ever exceeds the cap,
the parts always sum to the whole, and a trade that cannot be split safely
is refused instead of quietly oversized.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import swap


XLM = 10_000_000          # stroops per XLM
LAPSE = 100_000_000       # ticks per LAPSE


class TestSessionTag:
    ORDER_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_tag_roundtrips(self):
        sid = swap.new_session_id(self.ORDER_ID, "a.b.c")
        tag = swap.session_tag(self.ORDER_ID, sid, 7)
        assert swap.parse_session_tag(tag) == ("a1b2c3d4", sid[:8], 7)

    def test_session_ids_are_unique_per_fill(self):
        a = swap.new_session_id(self.ORDER_ID, "a.b.c")
        b = swap.new_session_id(self.ORDER_ID, "a.b.c")
        assert a != b

    def test_tag_fits_a_stellar_memo(self):
        """28 bytes is the hard limit; the id length is chosen for it."""
        sid = swap.new_session_id(self.ORDER_ID, "a.b.c")
        for step in (1, 9, 20, 99):
            assert len(swap.session_tag(self.ORDER_ID, sid, step).encode()) <= 28

    def test_order_id_dashes_are_stripped_before_truncating(self):
        """A plain slice would take a dash into the tag, or bury the
        eighth real hex character one position further out, the moment an
        id's dash does not land exactly at index 8 by accident."""
        tag = swap.session_tag("ab-c1d2e3f4gh", "f" * 16, 1)
        assert tag.startswith("abc1d2e3:")

    def test_rejects_non_tags(self):
        for junk in ("", "no-colon", "abc:def:", ":5", "abc:def:notanumber",
                     None, 42, "short:tags:1", "a1b2c3d4:short:1",
                     "a1b2c3d4:g1b2c3d4:1"):
            assert swap.parse_session_tag(junk) is None

    def test_rejects_a_tag_with_the_wrong_field_count(self):
        """A memo that merely looks tag-shaped (colon-joined) must not
        parse just because it happens to have three fields of some kind;
        each field's own shape is checked too (see the hex-length cases
        above)."""
        assert swap.parse_session_tag("a1b2c3d4:e5f6a1b2:1:extra") is None
        assert swap.parse_session_tag("a1b2c3d4:e5f6a1b2") is None


class TestExposureCap:
    def test_stranger_gets_the_base_cap(self):
        assert swap.exposure_cap_stroops(0.0) == swap.DEFAULT_STRANGER_CAP_STROOPS

    def test_trust_raises_the_cap(self):
        base = swap.exposure_cap_stroops(0.0)
        assert swap.exposure_cap_stroops(4.0) > base

    def test_trust_has_diminishing_returns(self):
        """Ten times the reputation must not buy ten times the exposure."""
        low = swap.exposure_cap_stroops(1.0)
        high = swap.exposure_cap_stroops(100.0)
        assert high > low
        assert high < low * 10

    def test_trust_cannot_remove_the_cap(self):
        enormous = swap.exposure_cap_stroops(10**9)
        ceiling = swap.DEFAULT_STRANGER_CAP_STROOPS * swap.MAX_TRUST_MULTIPLIER
        assert enormous <= ceiling

    def test_peer_stake_hard_caps_exposure(self):
        """A step must be worth less than what the peer forfeits by
        defecting, whatever their reputation says."""
        capped = swap.exposure_cap_stroops(1000.0, peer_stake_stroops=1 * XLM)
        assert capped == 1 * XLM

    def test_zero_stake_falls_back_to_the_dust_floor(self):
        assert swap.exposure_cap_stroops(50.0, peer_stake_stroops=0) == \
               swap.MIN_STEP_STROOPS

    def test_user_may_tighten_below_default(self):
        tight = swap.exposure_cap_stroops(0.0, stranger_cap=XLM // 2)
        assert tight == XLM // 2


class TestIncrementCount:
    def test_small_trade_uses_the_minimum(self):
        assert swap.increment_count(1000, 10 * XLM) == swap.MIN_INCREMENTS

    def test_count_keeps_steps_under_the_cap(self):
        """Checked against the schedule that gets built, not against an
        even share: the probe makes body steps larger than total/count,
        which is exactly the case that used to slip past the cap."""
        cap = 2 * XLM
        for total in (1 * XLM, 5 * XLM, 17 * XLM, 30 * XLM):
            n = swap.increment_count(total, cap)
            schedule = swap.build_schedule(total * 10, total, n)
            assert max(xlm for _lapse, xlm in schedule) <= cap

    def test_too_large_is_refused_not_oversized(self):
        cap = 1 * XLM
        too_big = cap * swap.MAX_INCREMENTS + 1
        with pytest.raises(swap.TradeTooLarge):
            swap.increment_count(too_big, cap)

    def test_refusal_reports_the_actual_limit(self):
        cap = 1 * XLM
        with pytest.raises(swap.TradeTooLarge) as exc:
            swap.increment_count(cap * 100, cap)
        assert exc.value.max_safe_stroops == swap.max_safe_trade_stroops(cap)
        assert exc.value.cap_stroops == cap

    def test_exactly_at_the_limit_is_allowed(self):
        cap = 1 * XLM
        n = swap.increment_count(swap.max_safe_trade_stroops(cap), cap)
        assert n == swap.MAX_INCREMENTS

    def test_reported_limit_is_itself_accepted(self):
        """The limit named in a refusal has to be a trade that actually
        works, or the user is sent to an amount that is refused too."""
        cap = 1 * XLM
        limit = swap.max_safe_trade_stroops(cap)
        schedule, count, _cap = swap.plan(limit * 10, limit, trust_score=0.0,
                                          stranger_cap=cap)
        assert count <= swap.MAX_INCREMENTS
        assert max(xlm for _lapse, xlm in schedule) <= cap

    def test_zero_or_negative_rejected(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                swap.increment_count(bad, XLM)


def math_ceil_div(a, b):
    return -(-a // b)


class TestSchedule:
    def test_parts_sum_to_the_whole(self):
        """A rounding crumb left over is a step that can never settle,
        because the counterparty waits for an amount that never arrives."""
        for count in range(2, swap.MAX_INCREMENTS + 1):
            for lapse, xlm in ((7 * LAPSE, 3 * XLM),
                               (1 * LAPSE, 1 * XLM),
                               (999_999_999, 123_456_789)):
                sched = swap.build_schedule(lapse, xlm, count)
                assert sum(p[0] for p in sched) == lapse
                assert sum(p[1] for p in sched) == xlm

    def test_produces_exactly_count_steps(self):
        for count in (2, 5, 20):
            assert len(swap.build_schedule(10 * LAPSE, 10 * XLM, count)) == count

    def test_first_step_is_a_probe(self):
        sched = swap.build_schedule(10 * LAPSE, 10 * XLM, 5)
        assert sched[0][1] < sched[1][1]

    def test_no_step_is_zero(self):
        for count in range(2, swap.MAX_INCREMENTS + 1):
            sched = swap.build_schedule(5 * LAPSE, 5 * XLM, count)
            assert all(lapse > 0 and xlm > 0 for lapse, xlm in sched)

    def test_too_small_to_split_is_rejected(self):
        with pytest.raises(ValueError):
            swap.build_schedule(3, 3, 10)

    def test_single_step_is_the_whole_trade(self):
        assert swap.build_schedule(5 * LAPSE, 2 * XLM, 1) == [(5 * LAPSE, 2 * XLM)]


class TestPlan:
    def test_plan_respects_the_cap(self):
        sched, count, cap = swap.plan(10 * LAPSE, 10 * XLM, trust_score=0.0)
        assert all(xlm <= cap for _lapse, xlm in sched)

    def test_plan_sums_to_the_totals(self):
        sched, _count, _cap = swap.plan(7 * LAPSE, 3 * XLM, trust_score=2.0)
        assert sum(p[0] for p in sched) == 7 * LAPSE
        assert sum(p[1] for p in sched) == 3 * XLM

    def test_trust_reduces_the_step_count(self):
        _s, stranger_steps, _c = swap.plan(20 * LAPSE, 20 * XLM, trust_score=0.0)
        _s, trusted_steps, _c = swap.plan(20 * LAPSE, 20 * XLM, trust_score=50.0)
        assert trusted_steps < stranger_steps

    def test_trust_never_pushes_below_the_minimum(self):
        _s, steps, _c = swap.plan(20 * LAPSE, 20 * XLM, trust_score=10**9)
        assert steps >= swap.MIN_INCREMENTS

    def test_oversized_trade_refused_with_a_usable_limit(self):
        with pytest.raises(swap.TradeTooLarge) as exc:
            swap.plan(10_000 * LAPSE, 10_000 * XLM, trust_score=0.0)
        assert exc.value.max_safe_stroops > 0
        assert exc.value.max_safe_stroops < 10_000 * XLM


class TestFirstMover:
    def test_less_established_side_opens(self):
        # The peer is well established (score 5) and this node barely is
        # (score 1), so this node is the less established side here, and
        # opening_mover says True: this node opens.
        assert swap.opening_mover(my_trust_of_peer=5.0, peer_trust_of_me=1.0) is True

    def test_more_established_side_does_not_open(self):
        # Reversed: this node is the well-established one now, so it does
        # not open, the peer does.
        assert swap.opening_mover(my_trust_of_peer=1.0, peer_trust_of_me=5.0) is False

    def test_even_match_defers_to_the_caller(self):
        assert swap.opening_mover(0.0, 0.0) is None

    def test_even_match_breaks_by_address_when_given(self):
        """A tie no longer defaults to a fixed role ('taker always
        opens'): that was a free, repeatable lever, since a maker
        posting bait orders from a fresh throwaway address could always
        count on an honest taker being tied at trust 0 and opening
        first, every time (see market.py's notes on this). With trust's
        own score formula reworked so a genuine first contact between
        two established addresses is no longer forced to an exact tie
        (see trust.STANDING_WEIGHT), an exact tie is now the rare case
        of two addresses with literally identical stake - at which
        point neither side has more to lose than the other, so a plain,
        content-derived comparison is enough."""
        assert swap.opening_mover(0.0, 0.0, "a.addr", "b.addr") is True
        assert swap.opening_mover(0.0, 0.0, "b.addr", "a.addr") is False

    def test_address_tiebreak_never_overrides_a_real_score_difference(self):
        # Even if the "wrong" address would win the alphabetical
        # fallback, a genuine score difference always decides first.
        assert swap.opening_mover(5.0, 1.0, "z.addr", "a.addr") is True
        assert swap.opening_mover(1.0, 5.0, "z.addr", "a.addr") is False

    def test_first_move_alternates(self):
        assert swap.i_move_first(1, True) is True
        assert swap.i_move_first(2, True) is False
        assert swap.i_move_first(3, True) is True
        assert swap.i_move_first(4, True) is False

    def test_alternation_is_symmetric(self):
        """Whatever one side does on a step, the other does the opposite."""
        for step in range(1, 21):
            assert swap.i_move_first(step, True) != swap.i_move_first(step, False)

    def test_exposure_is_shared_across_a_trade(self):
        """Neither side carries the first move for the whole trade."""
        mine = [swap.i_move_first(n, True) for n in range(1, 11)]
        assert 0 < sum(mine) < len(mine)

    def test_step_zero_rejected(self):
        with pytest.raises(ValueError):
            swap.i_move_first(0, True)


class TestPricing:
    def test_xlm_for_lapse_rounds_up(self):
        """Rounding must favour the XLM receiver consistently, or a trade
        stalls a stroop short of settling."""
        assert swap.xlm_for_lapse(1, 1) == 1

    def test_whole_lapse_at_unit_price(self):
        assert swap.xlm_for_lapse(LAPSE, 1000) == 1000

    def test_affordable_amount_is_always_payable(self):
        """The invariant that matters: what lapse_for_xlm says you can
        afford never costs more than the stroops you had. The reverse is
        not expected to hold, since xlm_for_lapse rounds a cost up and
        that rounded-up cost does buy slightly more."""
        for price in (1, 1000, 12_345, 99_999_999):
            for stroops in (1, 1000, XLM, 37 * XLM, 123_456_789):
                ticks = swap.lapse_for_xlm(stroops, price)
                if ticks > 0:
                    assert swap.xlm_for_lapse(ticks, price) <= stroops

    def test_cost_is_monotonic(self):
        price = 12_345
        previous = 0
        for ticks in sorted((1, 1000, LAPSE, 7 * LAPSE, 123_456_789)):
            cost = swap.xlm_for_lapse(ticks, price)
            assert cost >= previous
            previous = cost

    def test_zero_price_rejected(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                swap.xlm_for_lapse(LAPSE, bad)
            with pytest.raises(ValueError):
                swap.lapse_for_xlm(XLM, bad)


class TestLossBoundProperty:
    """The one property the whole design exists to provide."""

    def test_max_loss_is_one_step_never_more(self):
        for trust in (0.0, 1.0, 10.0, 1000.0):
            for total in (1 * XLM, 10 * XLM, 50 * XLM):
                cap = swap.exposure_cap_stroops(trust)
                if total > swap.max_safe_trade_stroops(cap):
                    continue
                sched, _count, cap = swap.plan(total * 10, total, trust)
                worst_step = max(xlm for _lapse, xlm in sched)
                assert worst_step <= cap

    def test_high_trust_cannot_collapse_a_trade_to_one_move(self):
        """A reputation must never buy a two-step trade that puts half the
        value at risk in one move."""
        _sched, count, _cap = swap.plan(100 * LAPSE, 100 * XLM, trust_score=10**9)
        assert count >= swap.MIN_INCREMENTS
