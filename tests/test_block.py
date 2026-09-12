"""
Unit tests for block.py

Covers: create_genesis, create, block_hash, block_size, validate (all
sub-checks), assemble.

VDF verification is mocked because vdf.evaluate takes ~120s.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
from params import (
    BLOCK_CYCLE_SECONDS, BLOCK_SIZE_LIMIT, GENESIS_MESSAGE,
    GENESIS_TIMESTAMP, TICKS_PER_LAPSE,
)
from tests.fixtures import address, genesis, make_block, make_tx, seed_balance


# ---------------------------------------------------------------------------
# VDF mock: vdf.verify always True unless explicitly overridden
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. create_genesis
# ---------------------------------------------------------------------------

class TestCreateGenesis:
    def test_genesis_height_is_zero(self):
        g = genesis()
        assert g["height"] == 0

    def test_genesis_previous_hash_is_zeros(self):
        g = genesis()
        assert g["previous_hash"] == "0" * 64

    def test_genesis_transactions_empty(self):
        g = genesis()
        assert g["transactions"] == []

    def test_genesis_has_hash_field(self):
        g = genesis()
        assert "hash" in g
        assert len(g["hash"]) == 64

    def test_genesis_hash_is_deterministic(self):
        g1 = genesis()
        g2 = genesis()
        assert g1["hash"] == g2["hash"]

    def test_genesis_contains_message(self):
        g = genesis()
        assert g["message"] == GENESIS_MESSAGE

    def test_genesis_timestamp_matches_params(self):
        g = genesis()
        assert g["timestamp"] == GENESIS_TIMESTAMP

    def test_genesis_builder_is_none(self):
        g = genesis()
        assert g["builder"] is None


# ---------------------------------------------------------------------------
# 2. block_hash
# ---------------------------------------------------------------------------

class TestBlockHash:
    def test_hash_excludes_hash_field(self):
        """block_hash must not include the 'hash' field (no circular dependency)."""
        g = genesis()
        h1 = block_mod.block_hash(g)
        g2 = dict(g)
        g2["hash"] = "different"
        h2 = block_mod.block_hash(g2)
        assert h1 == h2

    def test_hash_changes_with_content(self):
        g = genesis()
        g2 = dict(g)
        g2["height"] = 1
        assert block_mod.block_hash(g) != block_mod.block_hash(g2)

    def test_stored_hash_matches_computed(self):
        g = genesis()
        assert g["hash"] == block_mod.block_hash(g)


# ---------------------------------------------------------------------------
# 3. validate, hash integrity
# ---------------------------------------------------------------------------

class TestValidateHash:
    def test_valid_genesis_passes(self):
        g = genesis()
        ok, err = block_mod.validate(g, fresh_state(), [])
        assert ok is True, err

    def test_tampered_hash_fails(self):
        g = genesis()
        g["hash"] = "00" * 32
        ok, err = block_mod.validate(g, fresh_state(), [])
        assert ok is False
        assert "hash" in err

    def test_tampered_content_fails(self):
        g = dict(genesis())
        g["height"] = 999
        # Recompute hash so it's consistent but height is wrong
        g["hash"] = block_mod.block_hash(g)
        ok, err = block_mod.validate(g, fresh_state(), [])
        # Either height check or hash recompute catches the tamper
        assert ok is False


# ---------------------------------------------------------------------------
# 4. validate, parent linkage
# ---------------------------------------------------------------------------

class TestValidateParent:
    def test_block_with_correct_parent_passes(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is True, err

    def test_wrong_previous_hash_fails(self):
        g = genesis()
        b = make_block(1, "00" * 32, [])
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False
        assert "previous_hash" in err

    def test_height_not_parent_plus_one_fails(self):
        g = genesis()
        # Build a block with height=2 against a height-0 parent
        b = make_block(2, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False


# ---------------------------------------------------------------------------
# 5. validate, timestamp
# ---------------------------------------------------------------------------

class TestValidateTimestamp:
    def test_past_timestamp_passes(self):
        # No minimum-gap rule: any past timestamp is valid
        g = genesis()
        b = make_block(1, g["hash"], [], timestamp_offset=-(BLOCK_CYCLE_SECONDS + 1))
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is True, err

    def test_future_timestamp_fails(self):
        g = genesis()
        far_future = GENESIS_TIMESTAMP + 365 * 24 * 3600 + 3600
        b = block_mod.create(1, g["hash"], [], address(0),
                             "aa" * 100, "bb" * 100, timestamp=far_future)
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False
        assert "future" in err


# ---------------------------------------------------------------------------
# 6. validate, VDF proof (whitepaper: chain is its own clock)
# ---------------------------------------------------------------------------

class TestValidateVDF:
    def test_vdf_verified_for_height_gt_zero(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        # VDF mock returns True by default, should pass
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is True, err

    def test_invalid_vdf_proof_fails(self, monkeypatch):
        monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: False)
        g = genesis()
        b = make_block(1, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False
        assert "VDF" in err

    def test_missing_vdf_output_fails(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        b["vdf_output"] = None
        b["hash"] = block_mod.block_hash(b)
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False

    def test_challenge_binds_previous_hash_and_builder(self):
        """The challenge covers both inputs, so neither can be varied freely."""
        g = genesis()
        base  = block_mod.vdf_challenge(g["hash"], address(0))
        other_builder = block_mod.vdf_challenge(g["hash"], address(1))
        other_parent  = block_mod.vdf_challenge("cc" * 32, address(0))
        assert base != other_builder
        assert base != other_parent
        assert len(base) == 32
        # Deterministic: same inputs always give the same challenge.
        assert base == block_mod.vdf_challenge(g["hash"], address(0))

    def test_validate_uses_builder_bound_challenge(self, monkeypatch):
        """validate() must verify against vdf_challenge, not the bare parent hash."""
        seen = {}
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: seen.setdefault("c", challenge) or True)
        g = genesis()
        b = make_block(1, g["hash"], [], builder_index=0)
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is True, err
        assert seen["c"] == block_mod.vdf_challenge(g["hash"], address(0))
        assert seen["c"] != bytes.fromhex(g["hash"])

    def test_stolen_vdf_proof_rejected_under_new_builder(self, monkeypatch):
        """A VDF output is not a bearer token.

        Replay of the real attack: a node receives a broadcast block, keeps
        vdf_output/vdf_proof, swaps in its own builder address to claim the
        reward, and rebroadcasts. The proof only verifies against the
        original builder's challenge, so the copy is rejected.
        """
        g = genesis()
        victim, thief = address(0), address(1)
        honest_challenge = block_mod.vdf_challenge(g["hash"], victim)
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: challenge == honest_challenge)

        original = make_block(1, g["hash"], [], builder_index=0)
        ok, err = block_mod.validate(original, fresh_state(), [g])
        assert ok is True, err

        stolen = dict(original)
        stolen["builder"] = thief
        stolen["hash"]    = block_mod.block_hash(stolen)
        ok, err = block_mod.validate(stolen, fresh_state(), [g])
        assert ok is False
        assert "VDF" in err

    def test_transactions_not_bound_into_challenge(self, monkeypatch):
        """Content stays swappable on top of a valid proof.

        A block whose transaction list is replaced keeps a verifying VDF
        proof, so a rejected transaction list can be corrected without
        redoing the ~120 s of sequential work.
        """
        g = genesis()
        builder = address(0)
        honest_challenge = block_mod.vdf_challenge(g["hash"], builder)
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: challenge == honest_challenge)

        st = fresh_state()
        seed_balance(st, 2, 100.0)
        t = make_tx(2, 3, TICKS_PER_LAPSE, st)

        empty    = make_block(1, g["hash"], [],  builder_index=0)
        refilled = make_block(1, g["hash"], [t], builder_index=0,
                              vdf_output=empty["vdf_output"],
                              vdf_proof=empty["vdf_proof"])
        assert refilled["vdf_output"] == empty["vdf_output"]
        ok, err = block_mod.validate(refilled, st, [g])
        assert ok is True, err

    def test_genesis_skips_vdf_check(self):
        g = genesis()
        # Genesis has vdf_output=None, should still pass
        ok, err = block_mod.validate(g, fresh_state(), [])
        assert ok is True, err

    def test_invalid_builder_address_fails(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        b["builder"] = "not.a.valid.address"
        b["hash"] = block_mod.block_hash(b)
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False
        assert "builder" in err


# ---------------------------------------------------------------------------
# 7. validate, transaction application (no consensus-level ordering)
# ---------------------------------------------------------------------------

class TestValidateTransactions:
    def test_single_valid_tx_passes(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g = genesis()
        b = make_block(1, g["hash"], [t])
        ok, err = block_mod.validate(b, s.snapshot(), [g])
        assert ok is True, err

    def test_invalid_tx_fails_block(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["signature"] = "00" * 752  # tampered
        g = genesis()
        b = make_block(1, g["hash"], [t])
        ok, err = block_mod.validate(b, s.snapshot(), [g])
        assert ok is False
        assert "invalid tx" in err

    def test_multiple_txs_from_different_senders_applied_in_listed_order(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        seed_balance(s, 1, 100.0)
        t1 = make_tx(0, 2, TICKS_PER_LAPSE, s)
        t2 = make_tx(1, 2, TICKS_PER_LAPSE, s)
        g = genesis()
        b = make_block(1, g["hash"], [t1, t2])
        ok, err = block_mod.validate(b, s.snapshot(), [g])
        assert ok is True, err

    def test_fee_paid_by_sender_goes_to_builder(self):
        """block_fees sums every tx's fee, which chainstate credits entirely
        to the builder alongside the block reward."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, fee=500)
        g = genesis()
        b = make_block(1, g["hash"], [t])
        assert block_mod.block_fees(b) == 500


# ---------------------------------------------------------------------------
# 8. assemble
# ---------------------------------------------------------------------------

class TestVdfIterationsAdjustment:
    """Regression coverage for the VDF difficulty-adjustment boundary.

    Both the block that carries a genuine adjustment (built via assemble())
    and the validator checking that same block must agree on the required
    iteration count. Historically these used two different code paths
    (compute_next_vdf_iterations vs get_vdf_iterations) that disagreed
    exactly at boundary heights whenever a bump actually triggered.
    """

    def test_boundary_block_validates_when_bump_triggers(self, monkeypatch):
        monkeypatch.setattr("block.VDF_ADJUST_INTERVAL", 3)
        monkeypatch.setattr("block.VDF_ADJUST_MIN_SECONDS", 1_000_000)
        monkeypatch.setattr("block.VDF_ADJUST_NUMERATOR", 2)
        monkeypatch.setattr("block.VDF_ADJUST_DENOMINATOR", 1)

        g = genesis()
        chain = [g]
        for h in range(1, 3):
            blk = make_block(h, chain[-1]["hash"], [], chain=chain)
            chain.append(blk)

        # This block sits exactly at the adjustment boundary (height 3).
        iterations = block_mod.get_vdf_iterations(chain)
        assert iterations == block_mod.VDF_ITERATIONS * 2, \
            "sanity check: the window's real timestamps should trigger a bump"

        blk3 = block_mod.assemble(chain[-1], [], address(0), iterations)
        assert blk3["vdf_iterations"] == iterations
        blk3["vdf_output"] = "aa" * 100
        blk3["vdf_proof"]  = "bb" * 100
        blk3["hash"] = block_mod.block_hash(blk3)

        ok, err = block_mod.validate(blk3, fresh_state(), chain)
        assert ok is True, err

    def test_iterations_never_decrease(self, monkeypatch):
        monkeypatch.setattr("block.VDF_ADJUST_INTERVAL", 3)
        monkeypatch.setattr("block.VDF_ADJUST_MIN_SECONDS", 1)  # never triggers a bump
        monkeypatch.setattr("block.VDF_ADJUST_NUMERATOR", 2)
        monkeypatch.setattr("block.VDF_ADJUST_DENOMINATOR", 1)

        g = genesis()
        chain = [g]
        for h in range(1, 4):
            blk = make_block(h, chain[-1]["hash"], [], chain=chain)
            chain.append(blk)

        assert block_mod.get_vdf_iterations(chain) == block_mod.VDF_ITERATIONS


class TestAssemble:
    def test_assemble_returns_block_without_hash(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), block_mod.VDF_ITERATIONS)
        assert "hash" not in b

    def test_assemble_includes_valid_txs(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g = genesis()
        b = block_mod.assemble(g, [t], address(0), block_mod.VDF_ITERATIONS)
        assert len(b["transactions"]) == 1

    def test_assemble_prioritizes_higher_fee_per_byte(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        seed_balance(s, 1, 100.0)
        low  = make_tx(0, 2, TICKS_PER_LAPSE, s, fee=1)
        high = make_tx(1, 2, TICKS_PER_LAPSE, s, fee=10_000)
        g = genesis()
        b = block_mod.assemble(g, [low, high], address(0), block_mod.VDF_ITERATIONS)
        assert b["transactions"][0] is high

    def test_assemble_keeps_same_sender_txs_in_nonce_order_despite_fee(self):
        """A later nonce with a higher fee must not be sorted ahead of an
        earlier, cheaper nonce from the same sender: block._apply_transactions
        requires strict current+1 nonce order with no gaps, so a block that
        put nonce 2 before nonce 1 would fail its own validation, wasting
        the whole VDF cycle that produced it."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        low = make_tx(0, 1, TICKS_PER_LAPSE, s, fee=1)
        s.apply_tx(low)
        high = make_tx(0, 1, TICKS_PER_LAPSE, s, fee=10_000)
        g = genesis()
        b = block_mod.assemble(g, [high, low], address(0), block_mod.VDF_ITERATIONS)
        assert [t["nonce"] for t in b["transactions"]] == [low["nonce"], high["nonce"]]

    def test_assemble_stops_sender_group_at_first_non_fitting_tx(self):
        """If a sender's second transaction doesn't fit, its later ones
        must not be included either, or the block would apply a nonce with
        a gap before it and fail validation."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t2)
        t3 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g = genesis()
        # Reproduce assemble()'s own base_size exactly: measured on the
        # freshly-created skeleton before tx_bytes is added or hash removed.
        skeleton_size = block_mod.block_size(block_mod.create(
            height=g["height"] + 1, previous_hash=g["hash"], transactions=[],
            builder=address(0), vdf_iterations=block_mod.VDF_ITERATIONS))
        t1_size = tx_mod.tx_size_in_block(t1, position=0)
        import unittest.mock as _mock
        with _mock.patch("block.BLOCK_SIZE_LIMIT", skeleton_size + t1_size):
            b = block_mod.assemble(g, [t1, t2, t3], address(0), block_mod.VDF_ITERATIONS)
        assert [t["nonce"] for t in b["transactions"]] == [t1["nonce"]]

    def test_assemble_respects_size_limit(self):
        g = genesis()
        dummy_txs = []
        s = fresh_state()
        seed_balance(s, 0, 10_000.0)
        for i in range(50):
            t = make_tx(0, 1, TICKS_PER_LAPSE, s)
            dummy_txs.append(t)
            try:
                s.apply_tx(t)
            except Exception:
                break
        b = block_mod.assemble(g, dummy_txs, address(0), block_mod.VDF_ITERATIONS)
        blk_size = block_mod.block_size({**b, "hash": "x"})
        assert blk_size <= BLOCK_SIZE_LIMIT

    def test_assemble_records_tx_bytes(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), block_mod.VDF_ITERATIONS)
        assert "tx_bytes" in b

    def test_assemble_height_is_parent_plus_one(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), block_mod.VDF_ITERATIONS)
        assert b["height"] == g["height"] + 1

    def test_assemble_previous_hash_matches_tip(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), block_mod.VDF_ITERATIONS)
        assert b["previous_hash"] == g["hash"]


class TestRaceWindowBuilders:
    """race_window/race_odds must carry the builder through each row, since
    the odds page colors points by builder to show dominance."""

    def _chain(self, builder_indexes):
        chain = [genesis()]
        for h, idx in enumerate(builder_indexes, start=1):
            chain.append(make_block(h, chain[-1]["hash"], [], builder_index=idx))
        return chain

    def test_race_window_includes_builder(self):
        chain = self._chain([0, 1, 0])
        window = block_mod.race_window(chain)
        assert [b for _, _, b in window] == [address(0), address(1), address(0)]

    def test_race_odds_carries_builder_into_rows(self):
        chain = self._chain([0, 1, 0])
        race = block_mod.race_odds(chain, own_seconds=None)
        assert [b for _, _, b in race["window"]] == [address(0), address(1), address(0)]


class TestRaceChartBuilderColors:
    """_race_chart caps identity colors at 3 builders (see api.py's
    _BUILDER_COLOR_SLOTS docstring for why: a validated categorical palette
    only holds a normal-vision-safe distinction for every pair, not just
    adjacent ones, up to 3 slots) and folds the rest into one "other" color
    plus a legend entry, rather than generating unbounded colors."""

    def _chain(self, builder_indexes):
        chain = [genesis()]
        for h, idx in enumerate(builder_indexes, start=1):
            chain.append(make_block(h, chain[-1]["hash"], [], builder_index=idx))
        return chain

    def test_top_3_builders_get_distinct_colors(self):
        import api as api_mod
        # builder 0 wins 3 blocks, 1 wins 2, 2 wins 1, all fit in the top 3.
        chain = self._chain([0, 1, 0, 1, 0, 2])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)

        colors_by_builder = {p["builder"]: p["color"] for p in chart["points"]}
        picked = {address(0), address(1), address(2)}
        # Distinct colors, one each. Which slot a builder gets is by address
        # rather than by rank (see TestRaceChartColorStability), so this
        # asserts that they differ, not which is which.
        assert set(colors_by_builder) == picked
        assert len(set(colors_by_builder.values())) == 3
        assert {e["label"] for e in chart["legend"]} == picked

    def test_fourth_and_later_builders_fold_into_other(self):
        import api as api_mod
        # Four distinct builders, one block each, a 4th generated color
        # would fail the normal-vision floor (see dataviz palette check),
        # so the 4th must share the "other" color instead.
        chain = self._chain([0, 1, 2, 3])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)

        colors_used = {p["color"] for p in chart["points"]}
        assert colors_used == {"var(--series-1)", "var(--series-2)",
                               "var(--series-3)", "var(--series-other)"}
        other_entries = [e for e in chart["legend"] if e["color"] == "var(--series-other)"]
        assert len(other_entries) == 1
        assert other_entries[0]["label"] is None
        assert other_entries[0]["count"] == 1

    def test_ranking_by_frequency_not_first_seen(self):
        """The most-active builder leads the legend even if it appears later
        in the window: the ordering is by count, not by first appearance.

        Read off the legend rather than the colors, which no longer encode
        rank precisely so that they stop moving when rank does."""
        import api as api_mod
        # builder 1 appears once first, then builder 0 dominates with 4 blocks.
        chain = self._chain([1, 0, 0, 0, 0])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)

        assert [e["label"] for e in chart["legend"]] == [address(0), address(1)]
        assert [e["count"] for e in chart["legend"]] == [4, 1]


class TestRaceOddsNoOutlierBand:
    """race_odds no longer flags or excludes outliers: every interval in
    the window counts toward both the median and odds_pct."""

    def test_no_excluded_key(self):
        chain = [genesis()]
        for h in range(1, 4):
            chain.append(make_block(h, chain[-1]["hash"], []))
        race = block_mod.race_odds(chain, own_seconds=None)
        assert "excluded" not in race

    def test_odds_pct_counts_every_interval_including_outliers(self):
        """A block built long after its parent (e.g. a real network stall)
        must still count in odds_pct's denominator, not get silently
        dropped as an outlier."""
        chain = [genesis()]
        for h in range(1, 4):
            chain.append(make_block(h, chain[-1]["hash"], [], timestamp_offset=150))
        # One wildly long interval, 10x the others.
        chain.append(make_block(4, chain[-1]["hash"], [], timestamp_offset=1500))
        race = block_mod.race_odds(chain, own_seconds=100.0)
        assert len(race["window"]) == 4
        # own_seconds=100 beats all 4 intervals (150,150,150,1500).
        assert race["odds_pct"] == pytest.approx(100.0)


class TestRaceChartAxisZoom:
    """The chart's y-axis is a display decision, separate from the stats:
    it zooms to the typical cluster of values instead of stretching to fit
    a rare outlier, which would otherwise flatten every normal point into
    a sliver at the bottom of the plot. An outlier still renders, clipped
    to the plot edge and marked, with its real value in the tooltip."""

    def _chain(self, intervals):
        """Build a chain whose actual block-to-block intervals match
        `intervals` exactly. make_block's timestamp_offset is added on top
        of a fixed height*120 base, so it isn't itself the interval,
        convert desired intervals to the offsets that produce them."""
        chain = [genesis()]
        cum_offset = 0
        for h, interval in enumerate(intervals, start=1):
            base_gap = 240 if h == 1 else 120
            cum_offset += interval - base_gap
            chain.append(make_block(h, chain[-1]["hash"], [], timestamp_offset=cum_offset))
        return chain

    def test_outlier_does_not_stretch_axis_to_fit(self):
        import api as api_mod
        # Five normal ~150s intervals, one 1500s stall.
        chain = self._chain([150, 150, 150, 150, 150, 1500])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)
        # If the axis had stretched to include 1500, axis_hi would be
        # far above the typical band; zoomed, it should hug ~150-165s.
        assert chart["axis_hi"] < 300

    def test_outlier_point_flagged_clipped(self):
        import api as api_mod
        chain = self._chain([150, 150, 150, 150, 150, 1500])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)
        clipped = [p for p in chart["points"] if p["clipped"]]
        assert len(clipped) == 1
        assert clipped[0]["seconds"] == 1500

    def test_axis_bound_is_marked_when_something_clips_to_it(self):
        """A line resting on the plot edge is reading against a bound that
        means "this or more", so the axis has to say so rather than show a
        plain number there."""
        import api as api_mod
        chain = self._chain([150, 150, 150, 150, 150, 1500])
        race = block_mod.race_odds(chain, own_seconds=None)
        ticks = api_mod._race_chart(race)["ticks"]

        assert ticks[-1]["suffix"] == "+"
        assert all(t["suffix"] == "" for t in ticks[:-1])

    def test_axis_bound_is_not_marked_when_nothing_clips(self):
        """With everything on scale the bound is just a bound, and a + there
        would claim an off-scale value that does not exist."""
        import api as api_mod
        chain = self._chain([150, 152, 148, 151, 149, 150])
        race = block_mod.race_odds(chain, own_seconds=None)
        ticks = api_mod._race_chart(race)["ticks"]

        assert all(t["suffix"] == "" for t in ticks)

    def test_normal_points_not_clipped(self):
        import api as api_mod
        chain = self._chain([150, 150, 150, 150, 150, 1500])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)
        normal = [p for p in chart["points"] if p["seconds"] == 150]
        assert all(not p["clipped"] for p in normal)

    def test_no_outliers_axis_unaffected(self):
        """Sanity check: with no outliers, the typical-range filter is a
        no-op and every point stays unclipped."""
        import api as api_mod
        chain = self._chain([150, 152, 148, 151, 149])
        race = block_mod.race_odds(chain, own_seconds=None)
        chart = api_mod._race_chart(race)
        assert all(not p["clipped"] for p in chart["points"])


class TestRaceChartColorStability:
    """A builder's color must not move because someone else won a block.

    The chart re-renders every few seconds. Assigning colors by rank meant
    two builders one block apart swapped colors the moment they swapped
    places, so the chart appeared to recolor itself while nothing about who
    built what had changed.
    """

    def _colors(self, counts):
        import api as api_mod
        rows, h = [], 0
        for builder, n in counts.items():
            for _ in range(n):
                h += 1
                rows.append((h, 120.0, builder))
        chart = api_mod._race_chart(
            {"window": rows, "median": 120.0, "own_seconds": None, "odds_pct": None})
        return {e["label"]: e["color"] for e in chart["legend"] if e["label"]}

    def test_colors_hold_when_the_lead_changes_hands(self):
        leading_alice = self._colors({"alice": 5, "bob": 4, "carol": 3})
        leading_bob   = self._colors({"alice": 4, "bob": 5, "carol": 3})
        leading_carol = self._colors({"alice": 3, "bob": 4, "carol": 5})

        assert leading_alice == leading_bob == leading_carol

    def test_colors_hold_when_counts_tie(self):
        """Ties used to break on whatever order the chain put them in, so
        equal builders could swap on any refresh with nothing changing."""
        assert (self._colors({"alice": 4, "bob": 4, "carol": 4})
                == self._colors({"carol": 4, "alice": 4, "bob": 4}))

    def test_a_newcomer_does_not_disturb_those_it_sorts_after(self):
        before = self._colors({"alice": 5, "bob": 4, "carol": 3})
        after  = self._colors({"alice": 5, "bob": 4, "dave": 3})

        assert before["alice"] == after["alice"]
        assert before["bob"] == after["bob"]

    def test_the_legend_still_leads_with_the_most_dominant(self):
        """Color is by address, order is by block count: they are separate
        questions and the legend answers the more useful one."""
        import api as api_mod
        rows, h = [], 0
        for builder, n in {"alice": 2, "bob": 7, "carol": 4}.items():
            for _ in range(n):
                h += 1
                rows.append((h, 120.0, builder))
        chart = api_mod._race_chart(
            {"window": rows, "median": 120.0, "own_seconds": None, "odds_pct": None})

        assert [e["label"] for e in chart["legend"]] == ["bob", "carol", "alice"]


class TestRaceChartHeightAxis:
    """The height axis, and the one property that makes it safe to hand to
    the page as a set of draggable handles.

    Dragging redistributes the width the window is drawn across. The page
    recomputes each point's x from the handle positions, so the map it uses
    has to agree with _race_chart's own linear placement while the handles
    are untouched; if it doesn't, simply loading the page would nudge every
    point off where the server drew it, and the no-JS render and the live
    one would disagree.
    """

    def _race(self, n):
        return {"window": [(9000 + i, 120.0 + (i % 7), "a%d" % (i % 5))
                           for i in range(n)],
                "median": 122.0, "own_seconds": 121.0, "odds_pct": 50.0}

    def _x_from_ticks(self, chart, idx):
        """The page's piecewise map, at rest. Mirrors xOf() in odds.html."""
        ticks = chart["x_ticks"]
        i = len(ticks) - 2
        for k in range(len(ticks) - 1):
            if idx <= ticks[k + 1]["idx"]:
                i = k
                break
        span = ticks[i + 1]["idx"] - ticks[i]["idx"]
        t = (idx - ticks[i]["idx"]) / span if span else 0.0
        frac = ticks[i]["frac"] + t * (ticks[i + 1]["frac"] - ticks[i]["frac"])
        return chart["pad_l"] + frac * chart["plot_w"]

    @pytest.mark.parametrize("n", [2, 5, 14, 15, 16, 100, 719, 720])
    def test_at_rest_the_piecewise_map_is_the_linear_one(self, n):
        import api as api_mod
        chart = api_mod._race_chart(self._race(n))
        for p in chart["points"]:
            assert abs(self._x_from_ticks(chart, p["idx"]) - p["x"]) < 0.2

    @pytest.mark.parametrize("n", [2, 5, 14, 15, 16, 100, 719, 720])
    def test_ticks_pin_the_ends_and_strictly_ascend(self, n):
        """Handles are clamped between their neighbours, so a repeated or
        out-of-order index would produce one that cannot move, and a
        zero-width segment to divide by."""
        import api as api_mod
        ticks = api_mod._race_chart(self._race(n))["x_ticks"]
        idxs = [t["idx"] for t in ticks]

        assert idxs[0] == 0 and idxs[-1] == n - 1
        assert idxs == sorted(set(idxs))
        assert ticks[0]["frac"] == 0.0
        assert ticks[-1]["frac"] == pytest.approx(1.0)

    @pytest.mark.parametrize("n", [15, 16, 100, 719, 720])
    def test_rest_gaps_clear_the_pages_minimum(self, n):
        """MIN_GAP in odds.html keeps two handles from overlapping. A rest
        gap below it would put a handle outside its own clamp before anyone
        had dragged anything, so _X_TICK_COUNT and MIN_GAP have to stay
        compatible: this fails if either is changed without the other."""
        import api as api_mod
        ticks = api_mod._race_chart(self._race(n))["x_ticks"]
        gaps = [ticks[i + 1]["frac"] - ticks[i]["frac"]
                for i in range(len(ticks) - 1)]

        assert min(gaps) > 0.05

    def test_short_window_gets_labels_but_no_handles(self):
        """Below the threshold the interior ticks cannot land on distinct
        blocks, so the axis degrades to its two pinned ends. The page reads
        that as nothing to drag rather than special-casing a count."""
        import api as api_mod
        ticks = api_mod._race_chart(self._race(8))["x_ticks"]

        assert len(ticks) == 2

    def test_labels_name_real_blocks_in_the_window(self):
        import api as api_mod
        race = self._race(720)
        heights = [h for h, _, _ in race["window"]]
        ticks = api_mod._race_chart(race)["x_ticks"]

        assert [t["height"] for t in ticks] == [heights[t["idx"]] for t in ticks]


class TestRetargetUsesAnExactRatio:
    """The retarget multiplies a consensus value, so it does it with an
    integer ratio rather than a float. See params.VDF_ADJUST_NUMERATOR."""

    def test_the_ratio_matches_the_float_it_replaced(self):
        # Not a behaviour change today: the two agree everywhere the chain
        # can currently reach. This pins that they agree, so the swap is
        # demonstrably a no-op for any live chain rather than asserted to be.
        for v in (1, 2, 12_200_000, 999_999, 3_000_000):
            assert v * 51 // 50 == int(v * 1.02)

    def test_the_ratio_stays_exact_where_the_float_does_not(self):
        # Past 2**53 a double stops representing integers exactly and the
        # float form drifts. This is the case the integer ratio exists for.
        v = 469_580_907_456_899
        assert v * 51 // 50 == 478_972_525_606_036
        assert int(v * 1.02) == 478_972_525_606_037

    def test_iterations_only_ever_increase(self):
        from params import VDF_ADJUST_NUMERATOR, VDF_ADJUST_DENOMINATOR
        assert VDF_ADJUST_NUMERATOR > VDF_ADJUST_DENOMINATOR


class TestTimestampRulesAreSeparate:
    """The future-tolerance rule and the parent-gap rule are two different
    questions and now have two different constants. Split at identical
    values, so this is a no-op for validity today; what it buys is that
    either can be tuned without silently moving the other."""

    def test_the_split_is_behaviour_preserving_at_the_shipped_values(self):
        from params import TIMESTAMP_SKEW_SECONDS, MIN_BLOCK_SPACING_SECONDS
        assert TIMESTAMP_SKEW_SECONDS == MIN_BLOCK_SPACING_SECONDS == 30

    def test_parent_gap_uses_its_own_constant(self, monkeypatch):
        monkeypatch.setattr("block.MIN_BLOCK_SPACING_SECONDS", 5)
        g = genesis()
        b = make_block(1, g["hash"], [], timestamp_offset=-(120 - 10))
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is True, err

    def test_future_tolerance_uses_its_own_constant(self, monkeypatch):
        # Widening the parent gap must not widen how far into the future a
        # block may claim to be, which is what one shared constant did.
        monkeypatch.setattr("block.MIN_BLOCK_SPACING_SECONDS", 5)
        g = genesis()
        far_future = GENESIS_TIMESTAMP + 365 * 24 * 3600 + 3600
        b = block_mod.create(1, g["hash"], [], address(0),
                             "aa" * 100, "bb" * 100, timestamp=far_future)
        ok, err = block_mod.validate(b, fresh_state(), [g])
        assert ok is False
        assert "future" in err
