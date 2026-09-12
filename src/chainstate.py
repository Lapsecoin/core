"""ChainState: the two values that always move together.

chain and state are always consistent with each other. ChainState groups
them so the node can swap them as a unit and methods that read chain state
get one object instead of two.

ChainState is immutable after construction; mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import block as block_mod
import state as state_mod


def _apply_to_state(state, blk):
    """Everything a block does to the ledger beyond its own transactions,
    applied in place. The single definition of that rule; both apply_block
    and from_chain go through here.
    """
    builder = blk.get("builder")
    if builder:
        _apply_builder_reward(state, builder, blk)


def _apply_builder_reward(state, builder, blk):
    """Credit tx fees and the full newly-minted block reward to the builder.

    No Proof-of-Burn split: the builder receives the entire block reward
    unconditionally, plus every transaction fee in the block. This keeps
    block production profitable regardless of mempool contents and removes
    any incentive structure tied to burning.
    """
    total_fees = block_mod.block_fees(blk)
    if total_fees > 0:
        state.credit(builder, total_fees)

    reward = state.compute_block_reward()
    if reward >= 1:
        state.apply_reward_distribution([(builder, reward)])


class ChainState:
    """Consistent snapshot of chain + ledger state."""

    __slots__ = ("chain", "state", "cumulative_iterations")

    def __init__(self, chain, state, cumulative_iterations=0):
        self.chain = chain       # list of block dicts
        self.state = state       # State (balance ledger)
        # Sum of vdf_iterations actually proven across the chain (excludes
        # genesis, which has no VDF proof). Used for fork choice instead of
        # raw block count, see is_better_than().
        self.cumulative_iterations = cumulative_iterations

    # ------------------------------------------------------------------
    # Convenient accessors
    # ------------------------------------------------------------------

    @property
    def tip(self):
        return self.chain[-1]

    @property
    def height(self):
        return self.chain[-1]["height"]

    @property
    def genesis_hash(self):
        return self.chain[0]["hash"]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def _cumulative_iterations(cls, chain):
        """Sum of vdf_iterations actually proven, excluding genesis."""
        return sum(blk.get("vdf_iterations", 0) for blk in chain if blk["height"] > 0)

    @classmethod
    def from_genesis(cls):
        """Bootstrap a ChainState from the genesis block only."""
        genesis = block_mod.create_genesis()
        return cls([genesis], state_mod.State())

    @classmethod
    def from_chain(cls, chain):
        """Build a ChainState by replaying a fully trusted chain.
        Used at startup and after sync/reorg.

        Drives _apply_to_state, the same single definition of what a block
        does to the ledger that apply_block uses, rather than a second
        hand-written copy of it. Two copies of that rule is one more than
        the number that can be right, and they would not have to drift far
        to fork a chain.

        Not written as a fold over apply_block, tempting as that is:
        apply_block returns a new ChainState and so copies the chain list
        every time, which over a replay of the whole chain is quadratic in
        its length. The state is threaded through directly and the chain
        list is built once, at the end.
        """
        state = state_mod.State()
        for blk in chain:
            if blk["height"] == 0:
                continue
            for t in blk["transactions"]:
                state.apply_tx(t)
            _apply_to_state(state, blk)
        return cls(list(chain), state, cls._cumulative_iterations(chain))

    @classmethod
    def from_storage(cls, chain, stored_state):
        """Build a ChainState from a chain and a pre-loaded State snapshot.
        Avoids replaying txs (balances come from the snapshot).
        """
        return cls(list(chain), stored_state, cls._cumulative_iterations(chain))

    # ------------------------------------------------------------------
    # Produce a new ChainState by appending one block
    # ------------------------------------------------------------------

    def validate_and_apply(self, blk):
        """Validate blk against self, then return (ok, err, new_cs).

        Passes the post-validation probe state directly to _apply_block_state
        so transactions are applied only once (validate() already applied them
        to probe). Failure leaves self unchanged.
        """
        probe = self.state.snapshot()
        ok, err = block_mod.validate(blk, probe, self.chain)
        if not ok:
            return False, err, self
        # probe is now the post-tx state; hand it directly to avoid re-applying.
        return True, None, self._apply_block_with_state(blk, probe)

    def apply_block(self, blk):
        """Return a new ChainState with blk appended. Does not mutate self.
        Used by from_chain replay where no pre-validated probe is available.
        """
        post_tx = self.state.snapshot()
        for t in blk.get("transactions", []):
            post_tx.apply_tx(t)
        return self._apply_block_with_state(blk, post_tx)

    def _apply_block_with_state(self, blk, post_tx_state):
        """Finish applying blk given a state that already has txs applied.

        Shared by apply_block (which builds post_tx via replay) and
        validate_and_apply (which gets post_tx from the validation probe,
        avoiding a second application of all transactions).
        """
        _apply_to_state(post_tx_state, blk)
        new_iterations = self.cumulative_iterations + blk.get("vdf_iterations", 0)
        return ChainState(self.chain + [blk], post_tx_state, new_iterations)

    # ------------------------------------------------------------------
    # Fork choice: most cumulative proven VDF work wins, VDF output breaks ties
    # ------------------------------------------------------------------

    def is_better_than(self, other):
        """Return True if self should replace other.

        Fork choice: the chain with more cumulative proven VDF iterations
        wins. Not raw block count. A block's vdf_iterations is only
        accepted if its VDF proof actually verifies for that many
        iterations, so this sum can't be inflated by claiming more work
        than was cryptographically proven. Raw height is not used: a
        fork's own adjustment history is derived only from its own block
        timestamps, so an attacker who pads their own timestamps could
        otherwise keep their fork's required iteration count artificially
        low and out-build the honest chain in less real time than it took.

        Ties (routine, not rare: every block at a given height needs the
        same protocol-required iteration count regardless of who builds
        it, so any simple same-height fork ties exactly) break on the
        VDF output, not the block hash. block_hash includes the
        transaction list, and the transaction list is deliberately not
        bound into the VDF challenge (block.vdf_challenge), so it can be
        changed after the fact for free, that's the whole point, it's
        what lets a block be corrected and rebroadcast without redoing
        the 120s. But that same freedom means tie-breaking on block_hash
        would let a single builder, with no extra hardware at all, grind
        many transaction-list variants after finishing its VDF and pick
        whichever one hashes lower, biasing ties at nearly zero cost. That
        defeats the property the VDF exists to enforce: that influence
        over the chain costs real sequential time. vdf_output is a
        deterministic function of (previous_hash, builder) alone and
        cannot be varied without redoing the actual VDF under a different
        builder address, so tie-breaking on it keeps that cost real.
        Falls back to hash only for genesis (vdf_output is None there);
        genesis is the unique starting point and never actually ties
        against anything in practice.
        """
        if self.cumulative_iterations != other.cumulative_iterations:
            return self.cumulative_iterations > other.cumulative_iterations
        return block_mod.tie_break_key(self.tip) < block_mod.tie_break_key(other.tip)
