"""The fee lock: the only part of fee requests the chain itself enforces.

A request locks LAPSE for a few blocks so asking for gas costs something real
without anyone being paid for it, which is what keeps the service from
becoming a market to fake. The chain cannot see Base or Solana, so it never
learns whether anyone was actually helped. What it does see is whether any
node offered to, by posting a claim:

  no claim in the window  -> the lock goes back to the sender
  at least one claim      -> the lock is burned: it goes to the burn address,
                             which emission counts as unminted again (see
                             state.State.recycled), so builders are paid it out
                             over time rather than it being lost

Everything here is derived from chain data, so every node reaches the same
answer. The pieces:

  * a request is a transaction with exactly one output to the escrow address
    and a `[gas] ` memo; validity is checked in tx.py via check_lock()
  * block processing (process_block) registers new locks, notes the first
    claim, and settles every lock whose window has closed

The window is CLAIM_WINDOW_BLOCKS from the request's block, shortened to one
block after the first claim.
"""

import crypto
import params
import tx as tx_mod
from params import TICKS_PER_LAPSE

REQUEST_TAG = "[gas] "
CLAIM_TAG = "[gas-claim] "
CLAIM_WINDOW_BLOCKS = 5
REF_LEN = 24                       # hex characters of the request txid a claim names
MIN_LOCK = TICKS_PER_LAPSE         # one LAPSE: below this a lock is dust


def escrow_outputs(tx_dict):
    esc = crypto.escrow_address()
    return [o for o in tx_dict.get("outputs", []) if o.get("to") == esc]


def active(height):
    """Whether the lock rules apply to a block at this height."""
    return height >= params.GAS_LOCK_ACTIVATION_HEIGHT


def check_lock(tx_dict, next_height):
    """Consensus validity of a transaction's lock, or (True, None) when it
    has none. `next_height` is the height of the block it would go into.
    Structure only: whether the network, target and addresses in the memo
    make sense is node policy, because the chain has no business deciding
    which networks a node may pay on."""
    if not active(next_height):
        return True, None
    if tx_dict.get("from") == crypto.escrow_address():
        return False, "the escrow address can never be a sender"
    outs = escrow_outputs(tx_dict)
    if not outs:
        return True, None
    if len(outs) != 1:
        return False, "a fee request locks funds in exactly one output"
    if outs[0]["amount"] < MIN_LOCK:
        return False, "the lock is below the minimum"
    memo = tx_dict.get("memo", "")
    if not memo.startswith(REQUEST_TAG) or len(memo[len(REQUEST_TAG):].split(" ")) != 4:
        return False, "funds sent to escrow need a fee request memo"
    return True, None


def close_height(entry):
    """The block at whose end a lock settles."""
    latest = entry["height"] + CLAIM_WINDOW_BLOCKS
    if entry["claim"] is None:
        return latest
    return min(entry["claim"] + 1, latest)


def claim_ref(tx_dict):
    """The request prefix a claim transaction names, or None."""
    memo = tx_dict.get("memo", "")
    if not memo.startswith(CLAIM_TAG):
        return None
    parts = memo[len(CLAIM_TAG):].split(" ")
    ref = parts[0]
    if len(parts) != 3 or len(ref) != REF_LEN or any(c not in "0123456789abcdef" for c in ref):
        return None
    return ref


def process_block(state, blk):
    """What a block does to open locks, applied in place after its
    transactions. Order matters and is fixed: claims for locks from earlier
    blocks first, then this block's new requests, then settlement."""
    height = blk["height"]
    if not active(height):
        return
    txs = [t for t in blk.get("transactions", []) if isinstance(t, dict)]

    for t in txs:
        ref = claim_ref(t)
        if ref is None:
            continue
        for txid, entry in state.escrows.items():
            if (txid.startswith(ref) and entry["claim"] is None
                    and entry["height"] < height <= entry["height"] + CLAIM_WINDOW_BLOCKS):
                entry["claim"] = height

    for t in txs:
        outs = escrow_outputs(t)
        if outs:
            state.escrows[tx_mod.tx_hash(t)] = {
                "sender": t["from"], "amount": outs[0]["amount"],
                "height": height, "claim": None}

    esc = crypto.escrow_address()
    for txid in sorted(state.escrows):
        entry = state.escrows[txid]
        if height < close_height(entry):
            continue
        state.debit(esc, entry["amount"])
        if entry["claim"] is None:
            state.credit(entry["sender"], entry["amount"])
        else:
            state.credit(crypto.burn_address(), entry["amount"])
        del state.escrows[txid]
