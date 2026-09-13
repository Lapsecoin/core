"""Block creation, validation, serialization. Pure functions on dicts."""

import random
import statistics
import time as _time

import crypto
from crypto import canonical_json
import settings as settings_mod
import tx as tx_mod
import vdf as vdf_mod
from params import (
    BLOCK_SIZE_LIMIT,
    GENESIS_MESSAGE,
    GENESIS_TIMESTAMP,
    VDF_ITERATIONS,
    VDF_ADJUST_INTERVAL,
    VDF_ADJUST_MIN_SECONDS,
    VDF_ADJUST_NUMERATOR,
    VDF_ADJUST_DENOMINATOR,
    TIMESTAMP_SKEW_SECONDS,
    MIN_BLOCK_SPACING_SECONDS,
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
        # Integer ratio, not a float multiply. See VDF_ADJUST_NUMERATOR.
        return prior_iterations * VDF_ADJUST_NUMERATOR // VDF_ADJUST_DENOMINATOR
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


# Draws simulated when estimating odds. Enough that the answer is steady
# to a tenth of a point (the standard error of a mean over [0,1] is about
# 0.5 points here), few enough to be a couple of milliseconds.
ODDS_TRIALS = 10_000


def _simulate_draws(own_samples, field_samples, window_s, seed,
                    trials=ODDS_TRIALS):
    """Replay the draw `trials` times over the observed intervals.

    Returns (odds_pct, in_draw_pct, mean_entrants_when_in).

    Each trial draws one interval per builder, with replacement, from what
    that builder actually did, then applies the chain's own rule: the
    height opens on the first candidate, everything within window_s of it
    is settled on vdf_output and so splits the height evenly, everything
    later has lost (Node.open_draw, ChainState.is_better_than).

    Resampled rather than reduced to one pace apiece, because a pace is an
    estimate and the rule has a hard edge. Comparing medians makes a rival
    at 9.9s and one at 10.1s the difference between a half share and the
    whole height, when the data cannot tell those two apart. Here a rival
    near the edge is inside the window on some heights and outside on
    others, at the rate its own blocks say, which is the honest answer and
    the one a point estimate cannot give.

    Non-parametric on purpose: block intervals are a build time plus
    whatever the machine was doing, not a named distribution, so this
    draws from the record instead of fitting a shape to it.

    What it assumes, and cannot check: that every builder seen in the
    window contends for every height. A peer that is merely offline half
    the time looks like one that is present and slow. win_share_pct is the
    check on that, being a measurement of the same quantity, and a gap
    between the two means this assumption is not holding here.
    """
    rng    = random.Random(seed)
    others = list(field_samples.values())
    share_total = in_draw = entrant_total = 0.0
    for _ in range(trials):
        ours  = rng.choice(own_samples)
        drawn = [rng.choice(s) for s in others]
        cutoff = min([ours] + drawn) + window_s
        if ours > cutoff:
            continue        # finished after the draw for this height closed
        entrants = 1 + sum(1 for d in drawn if d <= cutoff)
        in_draw += 1
        entrant_total += entrants
        share_total   += 1.0 / entrants
    return (100.0 * share_total / trials,
            100.0 * in_draw / trials,
            entrant_total / in_draw if in_draw else None)


def race_odds(chain, own_seconds, own_addr=None, draw_window=None):
    """Race-odds page data: recent block intervals (a proxy for builder
    build time) and how this node's own pace compares to the field.

    own_seconds is this node's median real VDF build time, or None if it
    has neither finished a build nor calibrated. own_addr is this node's
    builder address, which is what separates our own blocks from everyone
    else's; None (unknown) falls back to comparing against the whole
    window.

    Returns None if there's no window yet. Otherwise:
      {"window": [(height, interval_seconds, builder), ...],
       "median": float, "own_seconds": float or None,
       "odds_pct": float or None, "own_pace": float or None,
       "own_pace_measured": bool, "entrants": int or None,
       "draw_window": float, "field_builders": int,
       "field_blocks": int, "own_blocks": int,
       "win_share_pct": float or None}

    odds_pct is this node's share of the next height: 100/entrants when it
    is fast enough to be in the draw at all, and 0 when it is not.

    Every builder in the window gets a pace, the median interval of its
    own blocks; ours is own_pace. A height is anchored to its first
    candidate and stays open for draw_window seconds (Node.open_draw).
    Everything landing inside is settled on vdf_output, which
    vdf_challenge derives from (previous_hash, builder) alone, so it is a
    draw among equals and not a race. Everything landing outside lost the
    height. Entrants is therefore the builders whose pace is within
    draw_window of the fastest, this node included, and the draw between
    them is uniform.

    That is the rule this chain actually runs, so the question it answers
    is countable rather than estimated. Earlier versions of this counted
    how many of the field's blocks our pace beat, which had to invent its
    own threshold and got it wrong in both directions: losing by half a
    second and losing by a minute counted the same, though the first is a
    coin flip and the second is never winning again. The setting's own
    help text is the giveaway, it is "how much of a speed advantage it
    takes to win outright".

    A near-tie still lands on one side or the other of draw_window, which
    is not a rounding artifact: that boundary is the rule, and a builder
    sitting on it is genuinely marginal. Both halves of the pace estimate
    lean the same, conservative, way. A builder that rarely wins is only
    seen on the heights it did win, which are its faster rounds, so it
    looks more competitive than it is and is more likely to be counted as
    an entrant; and our own consecutive blocks carry no propagation hop
    where the field's blocks after ours do, worth well under a second
    against a window in the seconds.

    own_pace falls back to own_seconds for a node with no blocks in the
    window at all, where a rough figure in the wrong unit beats no figure;
    own_pace_measured says which it is.

    win_share_pct is what actually happened: the share of the window this
    node built. Not a prediction and not derived from any of the above,
    which is the point of showing it alongside: the two are computed from
    different things and a gap between them means one of the assumptions
    here is wrong on this network.

    A real network stall shows up as one unusually long interval that
    own_seconds trivially beats, display-only, so that's an acceptable
    accuracy tradeoff for not special-casing outliers.
    """
    window = race_window(chain)
    if not window:
        return None
    median = statistics.median(i for _, i, _ in window)

    field = [row for row in window if row[2] != own_addr] if own_addr else window
    mine  = [row for row in window if row[2] == own_addr] if own_addr else []

    # Our pace in the field's own unit: the median interval of the blocks
    # we built, taken from this same window.
    #
    # own_seconds cannot do this job. It is a VDF wall clock over our last
    # 30 completed builds; an interval is a timestamp delta over 720
    # blocks, and carries whatever elapses between a parent being stamped
    # and the next evaluation starting. Two different quantities over two
    # different periods, so the difference between them is a constant
    # nobody measured. Against a much slower field that constant is lost
    # in the gap and the comparison survives it. Against hardware like
    # ours it *is* the answer, and the number it produces is arbitrary:
    # intervals sit in a band a few seconds wide, so a couple of seconds
    # of mismatch swings it by tens of points. Two identical machines
    # splitting a chain 50/50 would read anything at all.
    #
    # Interval against interval, both stamped the same way, both from the
    # same window, and the mismatch cancels instead of being estimated.
    # Two identical machines read 50, which is the correct answer and one
    # the old form could only reach by luck.
    own_pace = (statistics.median(i for _, i, _ in mine) if mine
                else own_seconds)

    # Each builder's observed intervals, kept whole rather than reduced to
    # one number: the spread is what decides the marginal cases below.
    field_samples = {}
    for _h, interval, builder in field:
        field_samples.setdefault(builder, []).append(interval)
    field_paces = {b: statistics.median(v) for b, v in field_samples.items()}

    window_s = (settings_mod.DRAW_WINDOW_SECONDS.default if draw_window is None
                else draw_window)
    own_samples = [i for _h, i, _b in mine] or ([own_pace] if own_pace is not None
                                                else [])

    odds_pct = in_draw_pct = entrants = None
    if own_samples:
        # Seeded from the tip, so the figure is stable while the chain is
        # and moves when it does. An unseeded draw would have the page
        # showing a different number on every refresh with nothing having
        # happened.
        seed = int(chain[-1].get("hash", "0")[:8] or "0", 16)
        odds_pct, in_draw_pct, entrants = _simulate_draws(
            own_samples, field_samples, window_s, seed)

    return {"window": window, "median": median,
            "own_seconds": own_seconds, "odds_pct": odds_pct,
            "own_pace": own_pace, "own_pace_measured": bool(mine),
            "entrants": entrants, "in_draw_pct": in_draw_pct,
            "draw_window": window_s,
            "field_builders": len(field_paces),
            "field_blocks": len(field), "own_blocks": len(mine),
            "win_share_pct": (100.0 * len(mine) / len(window)
                              if own_addr else None)}


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
        if ts < parent_ts + MIN_BLOCK_SPACING_SECONDS:
            return False, (f"block timestamp {ts} must be at least "
                           f"{MIN_BLOCK_SPACING_SECONDS}s after parent timestamp {parent_ts}")
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

    # Never earlier than the floor validation enforces. The clock is the
    # right answer almost always, and is silently the wrong one whenever
    # less than MIN_BLOCK_SPACING_SECONDS has passed since the parent was
    # stamped: _check_timestamp rejects such a block, so a builder that
    # stamped the honest time threw away the evaluation it had just spent
    # two minutes on, logged "self-produced block failed validation", and
    # did it again on the next height.
    #
    # That happens to whichever node is fastest, which is the one this
    # chain is supposed to pay: a machine finishing inside the spacing
    # floor of the parent could never build at all. A node whose clock
    # has drifted
    # behind the chain hits it too, for as long as the drift lasts.
    # Clamping is what the rule already asks for, spelled out.
    timestamp = max(_time.time(),
                    tip["timestamp"] + MIN_BLOCK_SPACING_SECONDS)

    skeleton = create(
        height=next_height,
        previous_hash=tip["hash"],
        transactions=[],
        builder=builder_addr,
        vdf_iterations=iterations,
        timestamp=timestamp,
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
