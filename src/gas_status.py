"""Where a fee request stands, for the page that watches one.

Everything is read from the chain (claims, the window, the order of
claimers) except the one fact the chain cannot know, whether the destination
has the gas yet, which the caller supplies. No node is trusted to say it
paid: the page shows the destination's own balance.
"""

import gas
import gas_track

# What the page tells people when it estimates waiting time. A rough average
# (blocks are at least 90 seconds apart), never used for any decision.
BLOCK_SECONDS = 120


def short(addr):
    """A claimer shown without the whole address."""
    return ".".join(addr.split(".")[:3])


def request_status(chain, tip, txid, found_height, dest_funded, claimer_funded=lambda base: True):
    """A dict describing the request, or None when the transaction is not a
    well-formed, signed fee request.

    dest_funded(): whether the destination already holds what it asked for.
    claimer_funded(base_addr): whether a claimer's Base address can pay; a
    node that cannot is not in the order, so it is not shown as one.
    """
    tracker = gas_track.Tracker()
    for h in range(found_height, tip + 1):
        tracker.ingest(chain[h])
    r = tracker.requests.get(txid)
    if r is None:
        return None
    net = gas.NETWORKS[r.req["network"]]
    claimers = sorted({c["lapse"] for c in r.claims})
    out = {
        "txid": txid, "network": net.slug, "network_name": net.name, "symbol": net.symbol,
        "dest": r.req["dest"], "target": r.req["target"], "lock": r.lock,
        "height": r.height, "close": r.close, "tip": tip,
        "claimers": len(claimers), "offers": 1 if r.first_claim is not None else 0,
        "explorer": net.explorer,
    }
    if tip < r.close:
        out.update(stage="collecting", blocks_left=r.close - tip,
                   eta_seconds=(r.close - tip) * BLOCK_SECONDS,
                   closes_early=r.first_claim is not None)
        return out
    if r.first_claim is None:
        out.update(stage="unclaimed", lock_outcome="refunded")
        return out
    out["lock_outcome"] = "burned"
    order = gas_track.claimers_in_order(r, chain[r.close]["hash"], claimer_funded)
    out["order"] = [{"rank": i + 1, "id": short(a)} for i, a in enumerate(order)]
    if not order:
        out.update(stage="no_eligible")
        return out
    if dest_funded():
        out.update(stage="funded")
        return out
    started = tip - (r.close + 1)
    last_end = gas.turn_start(r.close, len(order))
    if tip >= last_end:
        out.update(stage="gave_up")
        return out
    idx = max(0, started // gas.RANK_SLOT_BLOCKS)
    slot_end = gas.turn_start(r.close, idx + 1)
    out.update(stage="paying", current={"rank": idx + 1, "id": short(order[idx])},
               slot_end=slot_end, blocks_left=max(slot_end - tip, 0),
               eta_seconds=max(slot_end - tip, 0) * BLOCK_SECONDS,
               ranks_left=len(order) - idx - 1)
    return out
