"""ChainState: the two values that always move together.

chain and state are always consistent with each other. ChainState groups
them so the node can swap them as a unit and methods that read chain state
get one object instead of two.

ChainState is immutable after construction; mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import block as block_mod
import crypto
import state as state_mod


def _fork_point(chain_a, chain_b):
    """Index of the first block where chain_a and chain_b diverge.

    A block's hash commits (transitively, through previous_hash) to every
    block before it, so equality of hash at some height implies equality of
    the whole chain up to that height, and once two chains differ at a
    height they cannot agree again at any later one. That makes "do these
    chains agree here" monotone in height, so the first disagreement can be
    found with a binary search instead of a linear scan from genesis.

    Returns min(len(chain_a), len(chain_b)) if one is a plain prefix of the
    other, i.e. no divergence within the shorter chain's length.
    """
    lo, hi = 0, min(len(chain_a), len(chain_b))
    while lo < hi:
        mid = (lo + hi) // 2
        if chain_a[mid]["hash"] == chain_b[mid]["hash"]:
            lo = mid + 1
        else:
            hi = mid
    return lo


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
        it, so any simple same-height fork ties exactly) break block by
        block over the whole diverged range, not on a single block. See
        _wins_tie_break for why: deciding an arbitrarily deep tie from one
        block let its cost stay flat no matter how much real, equally-
        proven work sat behind it.

        Two chains with the same tip are never a tie to resolve (including
        self compared with itself): there is nothing to replace.
        """
        if self.cumulative_iterations != other.cumulative_iterations:
            return self.cumulative_iterations > other.cumulative_iterations
        if self.tip["hash"] == other.tip["hash"]:
            return False
        return self._wins_tie_break(other)

    def _wins_tie_break(self, other):
        """Break an exact cumulative_iterations tie: majority of per-height
        draws wins, not the single tip's VDF output.

        Each diverged height had its own draw already, the same kind
        _reorg_to_sibling settles for the immediate tip: same protocol-
        required iteration count on both sides, so the lower VDF output at
        that height is a value fixed by (previous_hash, builder) that
        cannot be produced without actually redoing that height's VDF, see
        block.vdf_challenge. Tallying every one of those draws instead of
        just the last is what makes overturning N blocks of tied work cost
        sustained advantage across all N of them, the same assumption
        proof-of-work chains already rest on (matching or beating the
        network's power, sustained, not just for an instant): a single
        grinding burst at the final block used to be enough regardless of
        how deep the tie ran, which priced a thousand-block reorg the same
        as a one-block one.

        Only the range both chains actually share counts:
        [fork point, min(len(self.chain), len(other.chain))). A chain with
        extra blocks past its rival's length gets no extra votes for them:
        rewarding blocks the other side never had a chance to contest
        would smuggle back exactly the "more blocks wins" rule
        cumulative_iterations already exists to reject, just moved into
        the tie-break instead of the primary comparison. (An equal
        cumulative_iterations total with differing chain length is only
        possible when this shared range is non-empty: since every VDF
        proof carries strictly positive iterations, a longer chain whose
        extra blocks fell entirely outside this range would have to have
        more total iterations, not equal ones. So there is always
        something in range to compare here.)

        A margin-based rule (lowest summed output, say) was considered and
        rejected: since output values have a bounded range, simulation
        showed grinding one height down hard enough to offset several
        honest ones is a real, if bounded, lever, and a per-height count
        has no such lever, each height is worth exactly one vote regardless
        of by how much it's won. So is any position-weighted vote (heavier
        near the tip or near the fork point): that just relocates the
        cheap single point to grind instead of removing it. Every diverged
        height counts exactly once.
        """
        fork_idx = _fork_point(self.chain, other.chain)
        end = min(len(self.chain), len(other.chain))
        self_wins = 0
        other_wins = 0
        for i in range(fork_idx, end):
            a_key = block_mod.tie_break_key(self.chain[i])
            b_key = block_mod.tie_break_key(other.chain[i])
            if a_key < b_key:
                self_wins += 1
            elif a_key > b_key:
                other_wins += 1
            # else: an exact key collision at this height, credited to
            # neither. Astronomically unlikely with real VDF output (a
            # 256-bit value), but a bare `else` here used to award it to
            # "other" no matter which side was asking, so the two chains'
            # own tallies of the very same height could disagree about who
            # won it. Both sides computing the same function on the same
            # data still meant every node agreed with every other node, so
            # this never split the network, but it could silently
            # manufacture a false tie (or miscount a margin) rather than
            # correctly leaving a real collision undecided by this height.
        if self_wins != other_wins:
            return self_wins > other_wins
        # A draw count tie: only possible with an even number of diverged
        # heights, and only reachable by an attacker able to steer the
        # outcome of every single one of them, since honest randomness
        # landing exactly even gets rarer as the range grows. Falling back
        # to any one block here (including the tip) would hand back
        # exactly the cheap single-block grind this whole scheme exists to
        # remove, and the attacker would get to choose when to trigger it.
        # Combine the entire shared range into one value instead: still
        # requires having actually produced every one of those blocks, no
        # single height to target.
        return (self._fallback_key(fork_idx, end)
                < other._fallback_key(fork_idx, end))

    def _fallback_key(self, fork_idx, end):
        """Combine every diverged height's tie-break key in [fork_idx, end)
        into one value, for the rare exact draw-count tie. See
        _wins_tie_break."""
        combined = "".join(
            block_mod.tie_break_key(self.chain[i]) for i in range(fork_idx, end)
        )
        return crypto.sha256_hex(combined)
