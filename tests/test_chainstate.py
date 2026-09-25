"""
Unit tests for chainstate.py

Covers: from_genesis, from_chain, from_storage, validate_and_apply,
apply_block, _apply_block_with_state, is_better_than (fork choice),
accessors (tip, height, genesis_hash).

Fork choice: most cumulative proven VDF work wins; VDF output breaks ties.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
from chainstate import ChainState
from params import TICKS_PER_LAPSE
from tests.fixtures import (
    address, genesis, make_block, make_tx,
)


@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# 1. from_genesis
# ---------------------------------------------------------------------------

class TestFromGenesis:
    def test_creates_chain_with_one_block(self):
        cs = ChainState.from_genesis()
        assert len(cs.chain) == 1

    def test_height_is_zero(self):
        cs = ChainState.from_genesis()
        assert cs.height == 0

    def test_tip_is_genesis(self):
        cs = ChainState.from_genesis()
        g = genesis()
        assert cs.tip["hash"] == g["hash"]

    def test_state_is_empty(self):
        cs = ChainState.from_genesis()
        assert cs.state.total_minted == 0


# ---------------------------------------------------------------------------
# 2. Accessors
# ---------------------------------------------------------------------------

class TestAccessors:
    def test_genesis_hash_property(self):
        cs = ChainState.from_genesis()
        assert cs.genesis_hash == cs.chain[0]["hash"]


# ---------------------------------------------------------------------------
# 3. validate_and_apply
# ---------------------------------------------------------------------------

class TestValidateAndApply:
    def test_valid_block_appends_chain(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.height == 1

    def test_valid_block_does_not_mutate_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        cs.validate_and_apply(b)
        assert cs.height == 0  # original unchanged

    def test_invalid_block_returns_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        b["height"] = 999
        b["hash"] = block_mod.block_hash(b)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is False
        assert cs2 is cs  # same object returned

    def test_block_with_tx_updates_state(self):
        cs = ChainState.from_genesis()
        # Seed balance directly into state for simplicity
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        t = make_tx(0, 1, TICKS_PER_LAPSE, cs.state)
        b = make_block(1, g["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE

    def test_fees_credited_to_builder_after_valid_block(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        reward = cs.state.compute_block_reward()
        t = make_tx(0, 1, TICKS_PER_LAPSE, cs.state)
        b = make_block(1, g["hash"], [t], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        # Builder receives the fee plus the full block reward.
        expected = t["fee"] + reward
        assert cs2.state.get_balance(address(2)) == expected


# ---------------------------------------------------------------------------
# 3b. Builder reward (unconditional, full reward, no split)
# ---------------------------------------------------------------------------

class TestBuilderReward:
    def test_builder_earns_full_reward_with_no_txs(self):
        cs = ChainState.from_genesis()
        reward = cs.state.compute_block_reward()
        b = make_block(1, cs.tip["hash"], [], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(2)) == reward

    def test_reward_is_full_amount_not_a_fraction(self):
        cs = ChainState.from_genesis()
        reward = cs.state.compute_block_reward()
        b1 = make_block(1, cs.tip["hash"], [], builder_index=2)
        _, _, cs1 = cs.validate_and_apply(b1)
        assert cs1.state.get_balance(address(2)) == reward
        assert reward > 0


# ---------------------------------------------------------------------------
# 4. from_chain (replay)
# ---------------------------------------------------------------------------

class TestFromChain:
    def test_from_genesis_only(self):
        g = genesis()
        cs = ChainState.from_chain([g])
        assert cs.height == 0

    def test_from_two_blocks(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        cs = ChainState.from_chain([g, b1])
        assert cs.height == 1

    def test_genesis_hash_preserved_in_chain(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        cs = ChainState.from_chain([g, b1])
        assert cs.genesis_hash == g["hash"]


# ---------------------------------------------------------------------------
# 5. from_storage
# ---------------------------------------------------------------------------

class TestFromStorage:
    def test_from_storage_uses_provided_state(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        s = state_mod.State()
        s.credit(address(99), 999_999)
        cs = ChainState.from_storage([g, b1], s)
        # The state provided is used directly
        assert cs.state.get_balance(address(99)) == 999_999


# ---------------------------------------------------------------------------
# 6. is_better_than (fork choice: cumulative proven work)
# ---------------------------------------------------------------------------

class TestIsBetterThan:
    def test_longer_chain_is_better(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        assert cs1.is_better_than(cs0)
        assert not cs0.is_better_than(cs1)

    def test_equal_height_lower_hash_wins_for_genesis_fallback(self):
        """Genesis has no vdf_output, so the tie-break falls back to hash
        there specifically. Real (non-genesis) ties use vdf_output
        instead, see the tests below."""
        cs = ChainState.from_genesis()
        g = cs.tip
        blk_a = dict(g)
        blk_b = dict(g)
        blk_a["hash"] = "aa" * 32
        blk_b["hash"] = "bb" * 32
        cs_a = ChainState([blk_a], cs.state.snapshot())
        cs_b = ChainState([blk_b], cs.state.snapshot())
        assert cs_a.is_better_than(cs_b)  # "aa..." < "bb..."
        assert not cs_b.is_better_than(cs_a)

    def test_tie_break_uses_vdf_output_not_block_hash(self):
        """A single builder can freely change which transactions a block
        includes after finishing its VDF (the transaction list isn't part
        of the challenge, see block.vdf_challenge), which changes
        block_hash for free. If ties were broken on block_hash, that
        builder could grind transaction-list variants to bias every tie in
        its favor at nearly zero cost, defeating the whole point of the
        VDF: that influence over the chain costs real sequential time.
        Two blocks with the same vdf_output (same real VDF work) but
        different hashes (different transaction lists) must be a genuine,
        unbreakable tie: neither is_better_than the other."""
        cs = ChainState.from_genesis()
        g = cs.tip
        blk_a = dict(g, height=1, vdf_output="same_output", hash="aa" * 32)
        blk_b = dict(g, height=1, vdf_output="same_output", hash="bb" * 32)
        cs_a = ChainState([g, blk_a], cs.state.snapshot())
        cs_b = ChainState([g, blk_b], cs.state.snapshot())
        assert not cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)

    def test_tie_break_cannot_be_overridden_by_a_lower_block_hash(self):
        """The block with the lower vdf_output wins the tie even when it
        has the numerically higher block_hash, proving hash alone can't
        decide it, which is what stops the free transaction-list-grinding
        attack described above."""
        cs = ChainState.from_genesis()
        g = cs.tip
        blk_lower_output = dict(g, height=1, vdf_output="aaa", hash="zz" * 32)
        blk_higher_output = dict(g, height=1, vdf_output="zzz", hash="aa" * 32)
        cs_lower = ChainState([g, blk_lower_output], cs.state.snapshot())
        cs_higher = ChainState([g, blk_higher_output], cs.state.snapshot())
        assert cs_lower.is_better_than(cs_higher)
        assert not cs_higher.is_better_than(cs_lower)

    def test_chain_is_not_better_than_itself(self):
        cs = ChainState.from_genesis()
        assert not cs.is_better_than(cs)

    def test_two_block_chain_beats_one_block_chain(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        _, _, cs2 = cs1.validate_and_apply(b2)
        assert cs2.is_better_than(cs1)
        assert not cs1.is_better_than(cs2)

    def _diverging_chains(self, outputs_a, outputs_b, cumulative_iterations):
        """Two chains sharing genesis, then diverging for len(outputs_a)
        heights, each block's vdf_output taken from the given list.
        outputs_a and outputs_b must be the same length. Both chains get
        the same cumulative_iterations (an exact tie is the only case
        where the per-height tie-break is ever consulted)."""
        assert len(outputs_a) == len(outputs_b)
        g = genesis()
        chain_a = [g]
        chain_b = [g]
        for i, (oa, ob) in enumerate(zip(outputs_a, outputs_b), start=1):
            chain_a.append(dict(g, height=i, vdf_output=oa, hash=f"a{i}" * 16))
            chain_b.append(dict(g, height=i, vdf_output=ob, hash=f"b{i}" * 16))
        state = state_mod.State()
        cs_a = ChainState(chain_a, state, cumulative_iterations=cumulative_iterations)
        cs_b = ChainState(chain_b, state, cumulative_iterations=cumulative_iterations)
        return cs_a, cs_b

    def test_majority_of_diverged_heights_wins_not_the_tip_alone(self):
        """self wins heights 1 and 2 but loses height 3 (the tip): under
        the old tip-only rule this chain would have LOST the tie despite
        winning most of the diverged range. Majority must win instead."""
        # "0" < "1" lexicographically, so "0..." outputs win their height.
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["0" * 64, "0" * 64, "9" * 64],
            outputs_b=["1" * 64, "1" * 64, "1" * 64],
            cumulative_iterations=999,
        )
        assert cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)

    def test_losing_the_tip_alone_no_longer_decides_a_deep_tie(self):
        """The mirror of the above: self wins only the tip and loses every
        earlier diverged height. Under the old rule self would win outright
        (only the tip mattered); under majority it must lose."""
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["9" * 64, "9" * 64, "0" * 64],
            outputs_b=["1" * 64, "1" * 64, "1" * 64],
            cumulative_iterations=999,
        )
        assert not cs_a.is_better_than(cs_b)
        assert cs_b.is_better_than(cs_a)

    def test_single_diverged_height_matches_old_tip_only_behavior(self):
        """At depth 1 majority-of-heights and tip-only are the same rule:
        no regression for the common, shallow case."""
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["0" * 64], outputs_b=["1" * 64],
            cumulative_iterations=1,
        )
        assert cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)

    def test_exact_draw_count_tie_is_still_decided_and_symmetric(self):
        """An even diverged range split exactly half and half falls back to
        a combined-range key, not any single block. Whichever side that
        favors, the two chains must disagree exactly oppositely, and
        neither call may raise or hang."""
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["0" * 64, "1" * 64],
            outputs_b=["1" * 64, "0" * 64],
            cumulative_iterations=2,
        )
        assert cs_a.is_better_than(cs_b) != cs_b.is_better_than(cs_a)

    def test_extra_blocks_past_the_shorter_chains_length_earn_no_votes(self):
        """A longer chain gets no bonus votes for heights its rival never
        had a chance to contest: that would smuggle back "more blocks
        wins", the exact rule cumulative_iterations already rejects.
        Here self has 3 diverged blocks losing all 3 shared heights against
        other's 2, plus one extra block beyond other's length; despite
        having "more blocks", self must still lose on the shared range."""
        g = genesis()
        state = state_mod.State()
        chain_a = [g,
                   dict(g, height=1, vdf_output="9" * 64, hash="a1" * 16),
                   dict(g, height=2, vdf_output="9" * 64, hash="a2" * 16),
                   dict(g, height=3, vdf_output="0" * 64, hash="a3" * 16)]
        chain_b = [g,
                   dict(g, height=1, vdf_output="0" * 64, hash="b1" * 16),
                   dict(g, height=2, vdf_output="0" * 64, hash="b2" * 16)]
        cs_a = ChainState(chain_a, state, cumulative_iterations=999)
        cs_b = ChainState(chain_b, state, cumulative_iterations=999)
        assert not cs_a.is_better_than(cs_b)
        assert cs_b.is_better_than(cs_a)

    def test_cumulative_iterations_still_strictly_primary_over_tie_break(self):
        """A real iteration deficit can never be made up by winning every
        single per-height draw: the majority tie-break must only ever be
        consulted on an exact cumulative_iterations tie."""
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["0" * 64, "0" * 64, "0" * 64],  # wins every height
            outputs_b=["9" * 64, "9" * 64, "9" * 64],
            cumulative_iterations=1,  # but strictly less real work
        )
        cs_b = ChainState(cs_b.chain, cs_b.state, cumulative_iterations=2)
        assert not cs_a.is_better_than(cs_b)
        assert cs_b.is_better_than(cs_a)

    def test_exact_key_collision_at_one_height_favors_neither_side_there(self):
        """A collision in tie_break_key at a diverged height (astronomically
        unlikely with real 256-bit VDF output, but must not corrupt the
        count if it ever happens) must not be silently credited to
        whichever side isn't asking: that made the two chains' own tallies
        of the very same height disagree about who won it. self wins
        height 1 outright; height 2 collides and must count for neither,
        so self must still win overall on the strength of height 1 alone,
        from both directions."""
        same = "c" * 64
        cs_a, cs_b = self._diverging_chains(
            outputs_a=["0" * 64, same],
            outputs_b=["9" * 64, same],
            cumulative_iterations=2,
        )
        assert cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)

    def test_fewer_blocks_with_more_proven_iterations_wins(self):
        """Fork choice weighs cumulative proven VDF work, not raw block
        count: a shorter chain whose blocks each proved more iterations
        can outweigh a longer chain of cheaper blocks."""
        cs = ChainState.from_genesis()
        heavy = ChainState(cs.chain, cs.state,
                            cumulative_iterations=1_000_000)
        light = ChainState(cs.chain + [make_block(1, cs.tip["hash"], [])],
                            cs.state,
                            cumulative_iterations=500_000)
        assert heavy.is_better_than(light)
        assert not light.is_better_than(heavy)


# ---------------------------------------------------------------------------
# 8. apply_block (direct test. Not via validate_and_apply)
# ---------------------------------------------------------------------------

class TestApplyBlock:
    def test_apply_block_increments_height(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        cs2 = cs.apply_block(b1)
        assert cs2.height == 1

    def test_apply_block_does_not_mutate_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        cs.apply_block(b1)
        assert cs.height == 0

    def test_apply_block_applies_transactions(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        t = make_tx(0, 1, TICKS_PER_LAPSE, cs.state)
        b1 = make_block(1, g["hash"], [t])
        cs2 = cs.apply_block(b1)
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE
