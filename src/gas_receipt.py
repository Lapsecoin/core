"""Checking a payer's receipt against the network it names.

A receipt is a node saying "this transaction paid that request". The chain
stores it and never reads it; what makes it worth anything is that anyone can
look the hash up on the other network. Only receipts from ranked claimers are
looked at (so nobody can make a node ask the RPC about arbitrary hashes), and
at most gas.MAX_RANKS of those exist.

Two grades, because the payer's own transaction is not always the delivery:

  delivered  a payout straight to the destination on Base: the transaction
             exists, succeeded, went to the destination and was sent by the
             payer's Base address
  paid       a payout through Relay: the transaction is the payer's deposit
             on Base. It proves the node paid, not that Relay filled it, so
             delivery is still read from the destination

An unreachable or silent RPC is "unknown", never "no": a node that cannot
tell must not call anything fake or failed.
"""

import evm
import gas
import relay

DELIVERED = "delivered"
PAID = "paid"

_verdicts = {}                # (tx, dest, payer) -> grade or None, once settled


def check(io, net, dest, payer_base, tx_hash):
    """DELIVERED, PAID, False (checked and wrong) or None (cannot tell)."""
    key = (tx_hash, dest.lower(), payer_base.lower())
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
        verdict = PAID
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
        grade = check(io, net, r.req["dest"], bases[rec["lapse"]], rec["tx"])
        if grade == DELIVERED:
            return grade, rec["tx"]
        if grade == PAID and found[0] is None:
            found = (grade, rec["tx"])
    return found
