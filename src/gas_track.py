"""What the chain says about fee requests: who asked, who offered, in what
order they pay. Rebuilt from blocks, so the same blocks give the same
answers on every node and in the light client's remote node alike. No I/O."""

import gas
import gaslock
import tx as tx_mod


class Request:
    __slots__ = ("txid", "height", "lapse_from", "lock", "req",
                 "first_claim", "claims")

    def __init__(self, txid, height, lapse_from, lock, req):
        self.txid = txid
        self.height = height
        self.lapse_from = lapse_from
        self.lock = lock
        self.req = req                # gas.parse_request_memo(), signature verified
        self.first_claim = None       # the chain's reading: any well-formed claim counts
        self.claims = []              # {"height", "lapse", "base"} with a verified signature

    @property
    def close(self):
        return gaslock.close_height({"height": self.height, "claim": self.first_claim})


class Tracker:
    """What the chain says about open and recently closed requests, rebuilt
    from blocks. No I/O, no clock: the same blocks give the same answers."""

    def __init__(self):
        self.requests = {}

    def ingest(self, blk):
        height = blk["height"]
        txs = [t for t in blk.get("transactions", []) if isinstance(t, dict)]
        for t in txs:
            self._claim(t, height)
        for t in txs:
            self._request(t, height)

    def _request(self, t, height):
        if not gaslock.escrow_outputs(t) or not gaslock.active(height) \
                or gaslock.check_lock(t, height)[0] is False:
            return
        req = gas.parse_request_memo(t.get("memo"))
        if req is None or not gas.verify_request_signature(req, t["from"], t["nonce"]):
            return
        txid = tx_mod.tx_hash(t)
        self.requests[txid] = Request(txid, height, t["from"],
                                      gaslock.escrow_outputs(t)[0]["amount"], req)

    def _claim(self, t, height):
        ref = gaslock.claim_ref(t)
        if ref is None:
            return
        parsed = gas.parse_claim_memo(t.get("memo"))
        for r in self.requests.values():
            if not r.txid.startswith(ref) or not r.height < height <= r.height + gaslock.CLAIM_WINDOW_BLOCKS:
                continue
            # The chain counts any well-formed claim, so the window follows it.
            if r.first_claim is None:
                r.first_claim = height
            if parsed and gas.verify_claim_signature(parsed, t["from"]):
                r.claims.append({"height": height, "lapse": t["from"],
                                 "base": parsed["base_addr"]})

    def forget_before(self, height):
        for txid in [k for k, r in self.requests.items() if self.finished_at(r) < height]:
            del self.requests[txid]

    @staticmethod
    def finished_at(r):
        return gas.turn_start(r.close, gas.MAX_RANKS) + gas.RANK_SLOT_BLOCKS


def claimers_in_order(r, close_hash, funded):
    """Claimer LAPSE addresses, first to pay first: those whose claim landed
    inside the window and whose Base address holds enough to pay, put in the
    order every node derives identically."""
    eligible = {}
    for c in r.claims:
        if c["height"] <= r.close and c["lapse"] not in eligible and funded(c["base"]):
            eligible[c["lapse"]] = c["base"]
    return gas.order_claimers(r.txid, close_hash, eligible)
