"""Checking a payer's receipt against the network it names.

A receipt is a node saying "this transaction paid that request". The chain
stores it and never reads it; what makes it worth anything is that anyone can
look the hash up on the other network. Only receipts from ranked claimers are
looked at (so nobody can make a node ask the RPC about arbitrary hashes), and
at most gas.MAX_RANKS of those exist.

Two grades, because the payer's own transaction is not always the delivery:

  delivered  a payout straight to the destination on Base: the transaction
             exists, succeeded, went to the destination and was sent by the
             payer's Base address. Or a payout through Relay: the payer's
             deposit on Base checks out and Relay itself reports the request
             it belongs to as a success to this network
  paid       a Relay payout whose deposit checks out but whose request Relay
             has not (yet) reported as done, or a receipt without the Relay
             request id. It proves the node paid, not that anything arrived

An unreachable or silent RPC is "unknown", never "no": a node that cannot
tell must not call anything fake or failed.
"""

import evm
import gas
import relay

DELIVERED = "delivered"
PAID = "paid"

_verdicts = {}                # (tx, dest, payer) -> grade or None, once settled


def _relay_verdict(io, net, tx_hash, request_id):
    """What Relay says of the request a deposit started: DELIVERED, PAID
    (not settled yet, or Relay cannot be asked), or False (it failed or was
    refunded, or it is not about this deposit or this network)."""
    try:
        st = io.relay_status(request_id)
    except (evm.EVMError, relay.RelayError):
        return PAID, False
    if not isinstance(st, dict):
        return PAID, False
    state = st.get("status")
    if state == "success":
        ins = [h.lower() for h in st.get("inTxHashes") or [] if isinstance(h, str)]
        if tx_hash.lower() in ins and st.get("originChainId") == evm.BASE_CHAIN_ID \
                and st.get("destinationChainId") == net.chain_id:
            return DELIVERED, True
        return False, True
    if state in ("failure", "refund", "refunded"):
        return False, True
    return PAID, False


def check(io, net, dest, payer_base, tx_hash, request_id=""):
    """DELIVERED, PAID, False (checked and wrong) or None (cannot tell)."""
    key = (tx_hash, dest.lower(), payer_base.lower(), request_id)
    if key in _verdicts:
        return _verdicts[key]
    base = gas.NETWORKS["base"]
    try:
        p = io.payment(base, tx_hash)
        floor = gas.usd_to_units(base, gas.FLOOR_USD, io.price(base)) // 2
    except (evm.EVMError, relay.RelayError):
        return None
    if p is None:
        return None
    if not p["ok"] or p["sender"].lower() != payer_base.lower() or p["value"] < floor:
        verdict = False
    elif net.slug == "base":
        verdict = DELIVERED if p["to"].lower() == dest.lower() else False
    else:
        verdict, final = (_relay_verdict(io, net, tx_hash, request_id) if request_id
                          else (PAID, True))
        if not final:
            return verdict                 # Relay may still be working on it: ask again
    _verdicts[key] = verdict
    return verdict


def best(io, r, order):
    """The strongest checked receipt of a request: (grade, tx_hash) or
    (None, None). Receipts count only from ranked claimers, after the
    window closed."""
    net = gas.network(r.req["network"])
    bases = {c["lapse"]: c["base"] for c in r.claims}
    found = (None, None)
    for rec in r.receipts:
        if rec["lapse"] not in order or rec["height"] <= r.close or rec["lapse"] not in bases:
            continue
        grade = check(io, net, r.req["dest"], bases[rec["lapse"]], rec["tx"], rec.get("req", ""))
        if grade == DELIVERED:
            return grade, rec["tx"]
        if grade == PAID and found[0] is None:
            found = (grade, rec["tx"])
    return found
