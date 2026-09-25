import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from relay_pool import rank_relays_for_pair, RelayCascade  # noqa: E402

RELAYS = [
    "relay://1.1.1.1:22067/?id=A",
    "relay://2.2.2.2:22067/?id=B",
    "relay://3.3.3.3:22067/?id=C",
    "relay://4.4.4.4:22067/?id=D",
]

GENESIS = "deadbeef" * 8


class TestRankRelaysForPair:
    def test_order_independent_of_argument_order(self):
        a = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.1:9000", "10.0.0.2:9001")
        b = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.2:9001", "10.0.0.1:9000")
        assert a == b

    def test_different_pairs_can_rank_differently(self):
        a = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.1:9000", "10.0.0.2:9001")
        b = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.1:9000", "10.0.0.3:9002")
        # Not a hard requirement (a collision is possible), just showing
        # the pair genuinely factors into the ranking, not just the pool.
        assert a != b or len(RELAYS) < 2

    def test_different_genesis_can_rank_differently(self):
        a = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.1:9000", "10.0.0.2:9001")
        b = rank_relays_for_pair(RELAYS, "other-genesis", "10.0.0.1:9000", "10.0.0.2:9001")
        assert a != b or len(RELAYS) < 2

    def test_is_a_permutation_of_input(self):
        ranked = rank_relays_for_pair(RELAYS, GENESIS, "10.0.0.1:9000", "10.0.0.2:9001")
        assert sorted(ranked) == sorted(RELAYS)

    def test_empty_pool(self):
        assert rank_relays_for_pair([], GENESIS, "a:1", "b:2") == []


class TestRelayCascadeConvergence:
    """The property that actually matters: two sides that never talk to
    each other still walk the identical sequence of candidates."""

    def test_both_sides_pick_the_same_first_candidate(self):
        cascade_a = RelayCascade()
        cascade_b = RelayCascade()
        addr_a, addr_b = "10.0.0.1:9000", "10.0.0.2:9001"

        first_from_a = cascade_a.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
        first_from_b = cascade_b.next_candidate(GENESIS, addr_b, addr_a, relays=RELAYS)
        assert first_from_a == first_from_b

    def test_both_sides_advance_in_lockstep_after_a_failure(self):
        cascade_a = RelayCascade()
        cascade_b = RelayCascade()
        addr_a, addr_b = "10.0.0.1:9000", "10.0.0.2:9001"

        # Round 1: both try the same relay (assume it was down)
        a1 = cascade_a.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
        b1 = cascade_b.next_candidate(GENESIS, addr_b, addr_a, relays=RELAYS)
        assert a1 == b1

        # Round 2: both should move to the same next relay, not diverge
        a2 = cascade_a.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
        b2 = cascade_b.next_candidate(GENESIS, addr_b, addr_a, relays=RELAYS)
        assert a2 == b2
        assert a2 != a1

    def test_wraps_instead_of_raising_once_exhausted(self):
        cascade = RelayCascade()
        addr_a, addr_b = "10.0.0.1:9000", "10.0.0.2:9001"
        seen = [cascade.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
                for _ in range(len(RELAYS) * 2 + 1)]
        assert all(c in RELAYS for c in seen)
        # after a full wrap it should repeat the same sequence
        assert seen[:len(RELAYS)] == seen[len(RELAYS):2 * len(RELAYS)]

    def test_empty_pool_returns_none_not_crash(self):
        cascade = RelayCascade()
        assert cascade.next_candidate(GENESIS, "a:1", "b:2", relays=[]) is None

    def test_reset_restarts_at_top_candidate(self):
        cascade = RelayCascade()
        addr_a, addr_b = "10.0.0.1:9000", "10.0.0.2:9001"
        first = cascade.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
        cascade.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)  # advance past first
        cascade.reset(GENESIS, addr_a, addr_b)
        after_reset = cascade.next_candidate(GENESIS, addr_a, addr_b, relays=RELAYS)
        assert after_reset == first

    def test_unrelated_pairs_dont_share_a_cursor(self):
        cascade = RelayCascade()
        a1 = cascade.next_candidate(GENESIS, "10.0.0.1:9000", "10.0.0.2:9001", relays=RELAYS)
        b1 = cascade.next_candidate(GENESIS, "10.0.0.3:9002", "10.0.0.4:9003", relays=RELAYS)
        # both start at index 0 of their own (possibly different) ranking
        a1_again = cascade.next_candidate(GENESIS, "10.0.0.1:9000", "10.0.0.2:9001", relays=RELAYS)
        assert a1_again != a1  # pair A advanced independently of pair B's call
