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
from gas_io import ChainIO
from gas_track import Request, Tracker, claimers_in_order  # noqa: F401  (re-exported)

log = logging.getLogger("ec.gas_worker")

MIN_SERVED_LOCK = gas.MIN_SERVED_LOCK

# Blocks of history scanned at startup, to pick up requests already open.
BACKLOG_BLOCKS = gaslock.CLAIM_WINDOW_BLOCKS + gas.MAX_RANKS * gas.RANK_SLOT_BLOCKS + 2

POLL_SECONDS = 15
DONE_KEY = "gas_done"


class GasWorker:
    def __init__(self, node, io):
        self.node = node
        self.io = io
        self.tracker = Tracker()
        self.cursor = None            # last height ingested
        self.cursor_hash = None       # its hash, to notice a reorg under us
        self._funded = {}             # Base address -> bool, for one pass
        self._afford = None           # (height, bool): can this wallet pay for one request
        self._tip = 0
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

    def step(self):
        view = self.node.view
        tip = view.height
        if self.cursor is not None and (
                self.cursor > tip or view.chain[self.cursor]["hash"] != self.cursor_hash):
            # The chain we read from was replaced: start over from its tail.
            self.tracker = Tracker()
            self.cursor = None
        self._funded = {}
        self._tip = tip
        start = (tip - BACKLOG_BLOCKS) if self.cursor is None else self.cursor
        for h in range(max(start, 0) + (0 if self.cursor is None else 1), tip + 1):
            self.tracker.ingest(view.chain[h])
        self.cursor = tip
        self.cursor_hash = view.chain[tip]["hash"]
        self.tracker.forget_before(tip)
        if getattr(self.node, "_kek", None) is None:
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
        """Whether the gas wallet holds enough to pay for one request. This is
        the whole on/off switch: a node that does not want to serve requests
        never funds the wallet. Asked once per block, not per pass, so an
        unfunded node does not poll a public endpoint for every open request."""
        if self._afford is not None and self._afford[0] == self._tip:
            return self._afford[1]
        try:
            price = self.io.price(gas.NETWORKS["base"])
            ok = gas.units_to_usd(gas.NETWORKS["base"], self.io.base_balance(),
                                  price) >= gas.NODE_CAP_USD
        except (relay.RelayError, evm.EVMError):
            ok = False                # cannot tell: do not offer
        self._afford = (self._tip, ok)
        return ok

    def _needs_help(self, net, r):
        price = self.io.price(net)
        return gas.payout_units(net, r.req["target"],
                                self.io.dest_balance(net, r.req["dest"]), price) > 0

    def _clear(self, net, r):
        """False when the destination is sanctioned, or cannot be checked:
        a node that cannot tell does not help."""
        try:
            return not self.io.sanctioned(net, r.req["dest"])
        except (evm.EVMError, relay.RelayError):
            return False

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def _maybe_claim(self, r, tip):
        if r.txid in self.claimed or any(c["lapse"] == self.node.addr for c in r.claims):
            return
        if not self._serves(r):
            return
        net = gas.network(r.req["network"])
        if not (self._can_afford() and self._needs_help(net, r) and self._clear(net, r)):
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
        try:
            if self.io.sanctioned(net, r.req["dest"]):
                self._mark(r.txid, "refused")
                log.info("[gas] %s is on a sanctions list, not paying", r.req["dest"])
                return
        except (evm.EVMError, relay.RelayError) as e:
            log.warning("[gas] could not screen %s, will retry: %s", r.req["dest"], e)
            return
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


class LiveIO(ChainIO):
    """The real world: public RPCs for balances, Relay for prices and payouts,
    the node for the claim transaction."""

    def __init__(self, node):
        super().__init__()
        self.node = node

    def balance_of_base(self, addr):
        return evm.get_balance_wei(self.base_rpc(), addr)

    def base_balance(self):
        return self.balance_of_base(base_wallet.load_address(self.node.base_wallet_path))

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
                                        self.base_rpc(), dest, amount)
        sender = base_wallet.load_address(node.base_wallet_path)
        q = relay.quote(net, dest, amount, sender, self.relay_key())
        secret = base_wallet.decrypt_secret(node.base_wallet_path, node._kek)
        try:
            return relay.deposit(q, secret, self.base_rpc())
        finally:
            del secret
