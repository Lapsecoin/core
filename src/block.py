"""Block creation, validation, serialization. Pure functions on dicts."""

import statistics
import time as _time

import crypto
from crypto import canonical_json
import tx as tx_mod
import vdf as vdf_mod
from params import (
    BLOCK_SIZE_LIMIT,
    GENESIS_MESSAGE,
    GENESIS_TIMESTAMP,
    VDF_ITERATIONS,
    VDF_ADJUST_INTERVAL,
    VDF_ADJUST_MIN_SECONDS,
    VDF_ADJUST_FACTOR,
    TIMESTAMP_SKEW_SECONDS,
)


def get_vdf_iterations(chain) -> int:
    """Return the VDF iteration count required for the next block
    (height = len(chain)) built on top of `chain`.

    Deterministically derived from real block timestamps, no
    self-reported or otherwise-unverifiable field is trusted. Assemblers
    and validators call this on the same chain prefix and always agree,
    including exactly at adjustment boundaries.

    Between boundaries, this is an O(1) lookup of the value fixed at the
    last boundary (itself independently verified when that block was
    accepted, so trusting it here is safe). At a boundary, the window
    that just completed is replayed once to decide whether to bump.
    """
    next_height = len(chain)
    if next_height < VDF_ADJUST_INTERVAL:
        return VDF_ITERATIONS

    last_boundary = (next_height // VDF_ADJUST_INTERVAL) * VDF_ADJUST_INTERVAL
    if next_height > last_boundary:
        return chain[last_boundary].get("vdf_iterations", VDF_ITERATIONS)

    # next_height == last_boundary: assembling/validating the boundary
    # block itself. Fold in the window that just completed, using real
    # timestamp deltas between consecutive blocks (not a self-reported
    # figure), the same signal Bitcoin's own retarget relies on.
    window_start      = last_boundary - VDF_ADJUST_INTERVAL
    prior_iterations  = chain[window_start].get("vdf_iterations", VDF_ITERATIONS)
    deltas = [
        chain[h]["timestamp"] - chain[h - 1]["timestamp"]
        for h in range(max(window_start, 1), last_boundary)
    ]
    if deltas and statistics.median(deltas) < VDF_ADJUST_MIN_SECONDS:
        return int(prior_iterations * VDF_ADJUST_FACTOR)
    return prior_iterations


# Blocks looked at on either side of a given block when computing its
# "vs median" block time for display, distinct from (and much smaller
# than) the consensus retarget window in get_vdf_iterations.
BLOCK_TIME_MEDIAN_WINDOW = 30

# Blocks looked at for the race-odds page, about 1 day at the ~120s
# target. Display-only, like BLOCK_TIME_MEDIAN_WINDOW above.
ODDS_WINDOW_BLOCKS = 720


def block_time_stats(chain, height):
    """This block's own time-since-parent vs. the local median, and their
    difference. None for genesis, which has no parent to measure from.
    Display-only. Not a consensus value."""
    if height <= 0:
        return None
    own = chain[height]["timestamp"] - chain[height - 1]["timestamp"]
    lo = max(1, height - BLOCK_TIME_MEDIAN_WINDOW + 1)
    deltas = [chain[h]["timestamp"] - chain[h - 1]["timestamp"]
              for h in range(lo, height + 1)]
    median = statistics.median(deltas)
    return {"own": own, "median": median, "diff": own - median}


def race_window(chain):
    """Last ODDS_WINDOW_BLOCKS block-to-block intervals, restricted to the
    single most recent vdf_iterations regime so seconds stay comparable
    (iterations only ratchet upward, see VDF_ADJUST_INTERVAL).

    Returns a list of (height, interval_seconds, builder) tuples, oldest first.
    """
    n = len(chain)
    if n < 2:
        return []
    start = max(1, n - ODDS_WINDOW_BLOCKS)
    current_iterations = chain[-1].get("vdf_iterations")
    rows = []
    for h in range(n - 1, start - 1, -1):
        if chain[h].get("vdf_iterations") != current_iterations:
            break
        rows.append((h, chain[h]["timestamp"] - chain[h - 1]["timestamp"],
                    chain[h].get("builder") or ""))
    rows.reverse()
    return rows


def race_odds(chain, own_seconds):
    """Race-odds page data: recent block intervals (a proxy for builder
    build time) and this node's odds of beating the field. own_seconds is
    this node's own median real VDF build time, or None if it hasn't
    finished a build yet.

    Returns None if there's no window yet. Otherwise:
      {"window": [(height, interval_seconds, builder), ...],
       "median": float, "own_seconds": float or None,
       "odds_pct": float or None}
    odds_pct is the percentage of this window's intervals own_seconds beats.
    A real network stall shows up as one unusually long interval that
    own_seconds trivially beats, display-only, so that's an acceptable
    accuracy tradeoff for not special-casing outliers.
    """
    window = race_window(chain)
    if not window:
        return None
    median = statistics.median(i for _, i, _ in window)

    odds_pct = None
    if own_seconds is not None:
        beaten = sum(1 for _, i, _ in window if i > own_seconds)
        odds_pct = 100.0 * beaten / len(window)

    return {"window": window, "median": median,
            "own_seconds": own_seconds, "odds_pct": odds_pct}


def vdf_challenge(previous_hash: str, builder: str) -> bytes:
    """Challenge a block's VDF must be evaluated over.

    Binds the sequential work to the address that will be paid for it.
    Without the builder in the challenge, a VDF output is a bearer token:
    any node that receives a broadcast block can keep vdf_output and
    vdf_proof, swap in its own builder address and its own transaction
    list, and rebroadcast a block that verifies just as well as the
    original. Whoever's copy arrives first wins, so the node that actually
    spent the ~120 s loses the reward to a node that spent nothing.

    Folding the builder in makes every builder evaluate a different VDF,
    so a stolen output verifies against nobody else's challenge. The
    transaction list is deliberately not folded in: content stays
    swappable on top of a valid proof, which is what lets a block whose
    transactions are rejected be corrected without redoing the ~120 s.
    """
    return crypto.sha256(bytes.fromhex(previous_hash) + builder.encode())


def tie_break_key(blk):
    """Sort key for choosing among equally-valid, same-height blocks: the
    lowest key wins. Must be vdf_output, not block_hash or arrival order,
    see ChainState.is_better_than for why. Falls back to hash only for
    genesis, which never actually ties against anything."""
    return blk.get("vdf_output") or blk["hash"]


def create_genesis():
    """
    Create the genesis block (block 0). Hardcoded and deterministic.
    GENESIS_TIMESTAMP anchors block 0. Every subsequent block carries its own
    timestamp, validated as not more than 30 s in the future.
    """
    blk = {
        "height":           0,
        "previous_hash":    "0" * 64,
        "transactions":     [],
        "builder":          None,
        "timestamp":        GENESIS_TIMESTAMP,
        "message":          GENESIS_MESSAGE,
        "vdf_output":       None,
        "vdf_proof":        None,
        "vdf_iterations":   VDF_ITERATIONS,
    }
    blk["hash"] = block_hash(blk)
    return blk


def create(height, previous_hash, transactions, builder,
           vdf_output=None, vdf_proof=None, timestamp=None,
           vdf_iterations=None):
    """Create a new block dict.

    builder: address of the node that produced the accepted VDF proof
             and assembled this block. Receives the full block reward
             plus every transaction fee in the block. None only for the
             genesis block.
    vdf_output: hex string of the VDF output element (from vdf.evaluate).
    vdf_proof:  hex string of the full VDF proof blob (from vdf.evaluate).
    vdf_iterations: iteration count used for this block's VDF. Set at
                    adjustment boundaries, carried forward otherwise.
    Both vdf_output and vdf_proof are None only for the genesis block.
    """
    blk = {
        "height":         height,
        "previous_hash":  previous_hash,
        "timestamp":      timestamp if timestamp is not None else _time.time(),
        "transactions":   transactions,
        "builder":        builder,
        "vdf_output":     vdf_output,
        "vdf_proof":      vdf_proof,
        "vdf_iterations": vdf_iterations if vdf_iterations is not None else VDF_ITERATIONS,
    }
    blk["hash"] = block_hash(blk)
    return blk


def block_hash(blk):
    """Deterministic hash of block (excludes 'hash' field itself)."""
    fields = {k: v for k, v in blk.items() if k != "hash"}
    return crypto.sha256_hex(canonical_json(fields))


def block_size(blk):
    """Size in bytes of serialized block."""
    return len(canonical_json(blk))


def block_fees(blk):
    """Sum of all transaction fees in blk, paid entirely to the builder."""
    return sum(t.get("fee", 0) for t in blk.get("transactions", []))


def _check_hash(blk):
    if blk.get("hash") != block_hash(blk):
        return False, "block hash mismatch"
    return True, None


def _check_parent(blk, chain):
    height = blk["height"]
    if height == 0:
        return True, None
    parent = chain[-1] if chain else None
    if parent is None:
        return False, "no parent block"
    if blk["previous_hash"] != parent["hash"]:
        return False, "previous_hash does not match parent"
    if height != parent["height"] + 1:
        return False, "height does not follow parent"
    return True, None


def _check_timestamp(blk, chain):
    ts = blk.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False, "block missing timestamp"
    if ts > _time.time() + TIMESTAMP_SKEW_SECONDS:
        return False, f"block timestamp {ts} is too far in the future"
    if chain:
        parent_ts = chain[-1]["timestamp"]
        if ts < parent_ts + TIMESTAMP_SKEW_SECONDS:
            return False, (f"block timestamp {ts} must be at least "
                           f"{TIMESTAMP_SKEW_SECONDS}s after parent timestamp {parent_ts}")
    return True, None


def _check_builder_and_vdf(blk, chain):
    if blk["height"] == 0:
        return True, None
    builder = blk.get("builder")
    if not isinstance(builder, str) or not crypto.is_valid_address(builder):
        return False, f"invalid builder address: {builder!r}"
    vdf_output = blk.get("vdf_output")
    vdf_proof  = blk.get("vdf_proof")
    if not isinstance(vdf_output, str) or not isinstance(vdf_proof, str):
        return False, "missing vdf_output or vdf_proof"

    # Validate vdf_iterations matches what the chain requires at this height
    expected_iterations = get_vdf_iterations(chain)
    block_iterations    = blk.get("vdf_iterations", VDF_ITERATIONS)
    if block_iterations != expected_iterations:
        return False, (f"vdf_iterations mismatch: block has {block_iterations}, "
                       f"chain expects {expected_iterations}")

    challenge = vdf_challenge(chain[-1]["hash"], builder)
    if not vdf_mod.verify(challenge, vdf_output, vdf_proof, block_iterations):
        return False, "invalid VDF proof"
    return True, None


def _apply_transactions(blk, state):
    """Apply blk's transactions to state in the block's listed order.

    Each transaction is validated against the running state incrementally
    (standard practice for a plaintext mempool, e.g. Bitcoin): an included
    transaction's nonce must be exactly current+1 given prior transactions
    already applied within this same block. There is no consensus-level
    canonical ordering requirement, a block can list its transactions in
    whatever order the builder chose, as long as each one is valid against
    the state as of applying the ones before it.
    """
    for t in blk["transactions"]:
        if not isinstance(t, dict):
            return False, "transaction entry is not a dict"
        ok, err = tx_mod.validate(t, state)
        if not ok:
            return False, f"invalid tx: {err}"
        state.apply_tx(t)
    return True, None


def validate(blk, state, chain):
    """Full block validation. Returns (True, None) or (False, error_string).

    Applies transactions to `state` in place. Callers must pass
    state.snapshot() (not the live state) so a failure leaves it clean.
    Rewards are NOT applied here; the caller applies them after success.
    """
    size = block_size(blk)
    if size > BLOCK_SIZE_LIMIT:
        return False, f"block exceeds size limit: {size} > {BLOCK_SIZE_LIMIT}"

    for check, args in (
        (_check_hash,            (blk,)),
        (_check_parent,          (blk, chain)),
        (_check_timestamp,       (blk, chain)),
        (_check_builder_and_vdf, (blk, chain)),
        (_apply_transactions,    (blk, state)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None


def assemble(tip, txs, builder_addr, iterations):
    """Assemble a candidate block from a mempool snapshot.

    Pure function: does not touch node state. Groups candidate transactions
    by sender and sorts each sender's own group by nonce ascending, since a
    block is only valid if a sender's transactions apply in strict nonce
    order (block.py's _apply_transactions requires current+1 with no gaps).
    Groups are then prioritized against each other by their lead (lowest
    pending nonce) transaction's fee-per-byte, the standard block-building
    priority. Adds whole groups in that priority order; within a group,
    stops at the first transaction that doesn't fit, since including a
    later nonce without its predecessor would create a gap and invalidate
    the whole block, unlike across different senders, where skipping one
    that doesn't fit to let a later, smaller one from someone else in is
    fine. Returns a block dict without a VDF proof attached; the caller
    adds vdf_output, vdf_proof, and recomputes the hash.

    txs:        candidate txs from the mempool, in any order.
    iterations: this block's required vdf_iterations, from
                get_vdf_iterations(chain). Taken directly rather than
                a chain argument so callers that already computed it
                (to run the VDF itself) don't pay for it twice.
    """
    next_height = tip["height"] + 1

    skeleton = create(
        height=next_height,
        previous_hash=tip["hash"],
        transactions=[],
        builder=builder_addr,
        vdf_iterations=iterations,
    )
    # "[" + "]" = 2 bytes for empty list; we'll add ", ".join(tx_jsons) inside
    base_size   = block_size(skeleton)
    running     = base_size
    valid_txs   = []

    by_sender = {}
    for t in txs:
        if isinstance(t, dict):
            by_sender.setdefault(t.get("from"), []).append(t)
    for group in by_sender.values():
        group.sort(key=lambda t: t["nonce"])

    groups = sorted(
        by_sender.values(),
        key=lambda g: g[0].get("fee", 0) / max(tx_mod.tx_size(g[0]), 1),
        reverse=True,
    )

    for group in groups:
        for t in group:
            # Size of this tx as it would appear serialized inside the
            # block. We add 1 for the "," separator between txs (except
            # the first).
            t_size = tx_mod.tx_size_in_block(t, position=len(valid_txs))
            if running + t_size > BLOCK_SIZE_LIMIT:
                break  # this sender's remaining txs would leave a nonce gap
            valid_txs.append(t)
            running += t_size

    skeleton["transactions"] = valid_txs
    skeleton["tx_bytes"]     = running - base_size
    # Hash is NOT set here: the caller must add vdf_output + vdf_proof before
    # hashing. block_hash is called once in _run_cycle after all fields are final.
    del skeleton["hash"]
    return skeleton
