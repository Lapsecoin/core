"""The node side of fee requests: offer to help, then help when it is your turn.

Each new block the worker does two things for every request it knows about:

  claim   while the window is open, and the request is one this node will
          serve, post a claim (a 1-tick transaction whose memo names the
          request and carries a signature from this node's Base address, so
          other nodes can see it holds the funds it offers)
  pay     once the window has closed, work out the claimers' order from chain
          data alone; when it is this node's turn, look at the destination
          and send whatever it still lacks, then stand aside for the next

Nothing here is trusted by anyone else. Whether the destination was helped is
read from the destination itself, by whichever node's turn it is, so a node
that lies or crashes costs the requester a few blocks, not the funds.

The chain only ever sees claims and the lock (see gaslock.py). Everything that
touches another chain goes through `io`, so the logic below runs unchanged in
tests with a fake.
"""

import json
import logging
import threading
import time

import base_wallet
import crypto
import evm
import gas
import gaslock
import relay
import settings as settings_mod
from params import TICKS_PER_LAPSE

log = logging.getLogger("ec.gas_worker")

# What a node asks as the lock before it will serve a request. A policy, not
# a rule: it follows the price of LAPSE, so it is a constant that gets
# lowered in a release when LAPSE is worth more, and updated nodes follow.
MIN_SERVED_LOCK = 10 * TICKS_PER_LAPSE

# Blocks of history scanned at startup, to pick up requests already open.
BACKLOG_BLOCKS = gaslock.CLAIM_WINDOW_BLOCKS + gas.MAX_RANKS * gas.RANK_SLOT_BLOCKS + 2

POLL_SECONDS = 15
DONE_KEY = "gas_done"


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
        if not gaslock.escrow_outputs(t) or gaslock.check_lock(t)[0] is False:
            return
        req = gas.parse_request_memo(t.get("memo"))
        if req is None or not gas.verify_request_signature(req, t["from"], t["nonce"]):
            return
        import tx as tx_mod
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


class GasWorker:
    def __init__(self, node, io):
        self.node = node
        self.io = io
        self.tracker = Tracker()
        self.cursor = None            # last height ingested
        self.cursor_hash = None       # its hash, to notice a reorg under us
        self._funded = {}             # Base address -> bool, for one pass
        self.claimed = set()          # request txids this process has claimed
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="gas-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.step()
            except Exception:
                log.exception("[gas] worker pass failed")

    # ------------------------------------------------------------------
    # One pass
    # ------------------------------------------------------------------

    def enabled(self):
        return bool(self.node.settings.get(settings_mod.GAS_ENABLED))

    def step(self):
        view = self.node.view
        tip = view.height
        if self.cursor is not None and (
                self.cursor > tip or view.chain[self.cursor]["hash"] != self.cursor_hash):
            # The chain we read from was replaced: start over from its tail.
            self.tracker = Tracker()
            self.cursor = None
        self._funded = {}
        start = (tip - BACKLOG_BLOCKS) if self.cursor is None else self.cursor
        for h in range(max(start, 0) + (0 if self.cursor is None else 1), tip + 1):
            self.tracker.ingest(view.chain[h])
        self.cursor = tip
        self.cursor_hash = view.chain[tip]["hash"]
        self.tracker.forget_before(tip)
        if not self.enabled() or getattr(self.node, "_kek", None) is None:
            return
        for r in sorted(self.tracker.requests.values(), key=lambda r: r.txid):
            if tip < r.close:
                self._maybe_claim(r, tip)
            else:
                self._maybe_pay(r, tip, view)

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def _serves(self, r):
        """Whether this node will help with r at all, from chain data."""
        if r.lock < MIN_SERVED_LOCK:
            return False
        net = gas.network(r.req["network"])
        if net is None or r.lapse_from == self.node.addr:
            return False
        own = base_wallet.load_address(self.node.base_wallet_path)
        return not (net.slug == "base" and gas.same_address(net, r.req["dest"], own or ""))

    def _can_afford(self):
        price = self.io.price(gas.NETWORKS["base"])
        have_usd = gas.units_to_usd(gas.NETWORKS["base"], self.io.base_balance(), price)
        return have_usd >= gas.NODE_CAP_USD

    def _needs_help(self, net, r):
        price = self.io.price(net)
        return gas.payout_units(net, r.req["target"],
                                self.io.dest_balance(net, r.req["dest"]), price) > 0

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def _maybe_claim(self, r, tip):
        if r.txid in self.claimed or any(c["lapse"] == self.node.addr for c in r.claims):
            return
        if not self._serves(r):
            return
        net = gas.network(r.req["network"])
        if not (self._can_afford() and self._needs_help(net, r)):
            return
        ok, err = self.io.claim(r.txid)
        if ok:
            self.claimed.add(r.txid)
            log.info("[gas] claimed request %s for %s on %s", r.txid[:12], r.req["dest"], net.slug)
        else:
            log.warning("[gas] claim for %s failed: %s", r.txid[:12], err)

    # ------------------------------------------------------------------
    # Pay
    # ------------------------------------------------------------------

    def _done(self):
        raw = self.node.storage.get_meta(DONE_KEY)
        return json.loads(raw) if raw else {}

    def _mark(self, txid, state, **extra):
        done = self._done()
        done[txid] = {"state": state, **extra, "at": int(time.time())}
        # Keep the record short: only requests still tracked matter.
        done = {k: v for k, v in done.items()
                if k in self.tracker.requests or k == txid}
        self.node.storage.set_meta(DONE_KEY, json.dumps(done, sort_keys=True))

    def _maybe_pay(self, r, tip, view):
        if r.txid in self._done() or tip < r.close:
            return
        order = claimers_in_order(r, view.chain[r.close]["hash"], self._claimer_funded)
        if self.node.addr not in order:
            return
        if tip < gas.turn_start(r.close, order.index(self.node.addr)):
            return
        net = gas.network(r.req["network"])
        price = self.io.price(net)
        amount = gas.payout_units(net, r.req["target"],
                                  self.io.dest_balance(net, r.req["dest"]), price)
        if amount == 0:
            self._mark(r.txid, "funded")
            log.info("[gas] %s already funded, standing down", r.req["dest"])
            return
        if not self._can_afford():
            self._mark(r.txid, "unaffordable")
            return
        # Recorded before money moves: if the process dies between the two,
        # the next start sees the mark and does not pay twice. The next
        # ranked node reads the destination and covers what is missing.
        self._mark(r.txid, "paying", amount=amount)
        try:
            tx_hash = self.io.pay(net, r.req["dest"], amount)
        except (relay.RelayError, evm.EVMError, ValueError) as e:
            self._mark(r.txid, "failed", error=str(e))
            log.warning("[gas] paying %s on %s failed: %s", r.req["dest"], net.slug, e)
            return
        self._mark(r.txid, "paid", amount=amount, tx=tx_hash)
        log.info("[gas] sent %s units to %s on %s (%s)", amount, r.req["dest"], net.slug, tx_hash)

    def _claimer_funded(self, base_addr):
        if base_addr not in self._funded:
            try:
                price = self.io.price(gas.NETWORKS["base"])
                bal = self.io.balance_of_base(base_addr)
                self._funded[base_addr] = gas.units_to_usd(
                    gas.NETWORKS["base"], bal, price) >= gas.NODE_CAP_USD
            except (relay.RelayError, evm.EVMError):
                self._funded[base_addr] = False
        return self._funded[base_addr]


class LiveIO:
    """The real world: public RPCs for balances, Relay for prices and payouts,
    the node for the claim transaction."""

    def __init__(self, node):
        self.node = node

    def _base_rpc(self):
        return (self.node.settings.get(settings_mod.BASE_RPC_URL).strip() or evm.DEFAULT_BASE_RPC)

    def _relay_key(self):
        return self.node.settings.get(settings_mod.RELAY_API_KEY).strip()

    def price(self, net):
        return relay.price_usd(net, self._relay_key())

    def balance_of_base(self, addr):
        return evm.get_balance_wei(self._base_rpc(), addr)

    def base_balance(self):
        return self.balance_of_base(base_wallet.load_address(self.node.base_wallet_path))

    def dest_balance(self, net, addr):
        if net.vm == "evm":
            url = self._base_rpc() if net.slug == "base" else net.rpc
            return evm.get_balance_wei(url, addr)
        resp = evm.rpc(net.rpc, "getBalance", [addr])
        return int(resp["value"]) if isinstance(resp, dict) else int(resp)

    def claim(self, request_txid):
        import wallet_ui
        from local_reader import local_reader_for
        node = self.node
        secret = base_wallet.decrypt_secret(node.base_wallet_path, node._kek)
        try:
            base_addr = evm.address_from_secret(secret)
            sig = evm.sign_message(gas.claim_message(gas.request_ref(request_txid), node.addr), secret)
        finally:
            del secret
        memo = gas.build_claim_memo(request_txid, base_addr, sig)
        outputs = [{"to": crypto.burn_address(), "amount": 1}]
        reader = local_reader_for(node)
        acct = reader.account(node.addr, fees=True, fresh=True)
        fee = wallet_ui.auto_fee(acct, node.addr, node.pk_hex, outputs, memo=memo)
        t, _ = node._build_and_sign_tx_with_kek(outputs, fee, node._kek, memo)
        return reader.submit(t)

    def pay(self, net, dest, amount):
        node = self.node
        if net.slug == "base":
            return base_wallet.send_eth(node.base_wallet_path, node._kek,
                                        self._base_rpc(), dest, amount)
        sender = base_wallet.load_address(node.base_wallet_path)
        q = relay.quote(net, dest, amount, sender, self._relay_key())
        secret = base_wallet.decrypt_secret(node.base_wallet_path, node._kek)
        try:
            return relay.deposit(q, secret, self._base_rpc())
        finally:
            del secret
