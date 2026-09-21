"""The thread that actually moves trades forward.

Without this the engine is a library nobody calls. It exists as a
separate thread rather than inside the node's block cycle because a step
waits on Horizon over the network, and a cycle that blocks on a public
API stops building blocks.

Order of operations at startup
------------------------------
Reconcile every unfinished trade against both chains before sending
anything, ever. A node coming back up does not know whether the payment
it was making got out, and the only account that survives a crash is the
one written on the two ledgers. Sending first and reconciling after is
precisely how a restart pays twice.

Only then does the loop begin, and each pass re-derives what it needs, so
a pass is safe to interrupt and safe to repeat.

Woken, not polled
-----------------
There used to be one number here, POLL_SECONDS, and this thread ran a
full pass against it whether or not anything had happened: every 20
seconds, forever, for as long as swaps were enabled, doing real work
(chain reads, a possible Horizon call) even while nothing about any
trade had changed since the last pass.

Nothing here needs to be discovered by asking again on a clock. Every
fact a pass could learn arrives at this node as a specific event first:
a matching transaction lands in the mempool or a block (node's own
gossip handlers), a new block changes what height means for every
pending deadline (node._commit, node.apply_better_chain), a fill
request or response naming one of this node's own trades is gossiped
in. node calls wake() at each of those points (see
node._wake_swap_worker and its call sites) and this thread simply runs
one pass in response, on its own thread, off whatever hot path noticed
the event. A pass that finds nothing to do costs one idle query per
open trade, which is cheap enough that reacting eagerly, rather than
batching reactions until some interval elapses, costs nothing extra.

BACKSTOP_SECONDS below is not the mechanism, it is insurance against the
mechanism: gossip is UDP, delivery is not guaranteed, and this is what
notices if a relevant event was simply never seen. It fires rarely
enough that, on a quiet node, this thread spends nearly all its time
blocked on wake() rather than doing anything.

Reacting to every event has one cost the old fixed poll never had:
nothing here rate-limits how often wake() itself can fire. node calls it
on every admitted transaction, among other things, and admission is
cheap and already something a peer decides how much of to send this
node (a flood of small, individually-valid transactions costs the
sender little and would otherwise wake this thread once per transaction,
turning gossip volume directly into swap-worker chain I/O). MIN_PASS_
INTERVAL_SECONDS is the floor under that: passes still run back to back
under sustained pressure, just never faster than this, so the worst case
is bounded regardless of how fast something outside this node's control
can make wake() fire. An idle node never notices it, since nothing is
re-firing wake() for it to throttle.

Blame
-----
Whether a stall currently counts against a peer is computed fresh from
that trade's own rows every pass (see swap_engine.is_delinquent), not
decided once and stored; see trust.py's module docstring for why.

What this no longer consults is whether the peer looked "alive". That
signal came from the liveness notes the uptime rewarder gossiped, and it
was wrong twice over: those notes said a node was powered on, not that
its swap worker had seen the trade and declined to pay, and broadcasting
them published a payable address network-wide, which is exactly the link
a trading identity must not have. They are gone.

What replaces it is stronger and comes from the chain: a peer is only
blamed once they have themselves reciprocated an earlier step. Their own
signed transaction is the acceptance, so an unsolicited payment nobody
answered can never be dressed up as abandonment. See
swap_engine.is_delinquent.
"""

import logging
import threading
import time

import market as market_mod
import settings as settings_mod
import swap_engine
import trade_storage
import xlm as xlm_mod
from trade_storage import Trade, TRADE_ACTIVE, TRADE_STALLED

log = logging.getLogger("ec.swap_worker")

# The defensive backstop only: how long this thread will wait for wake()
# before running a pass anyway, in case a relevant event was gossiped and
# simply never arrived (see the module docstring). Not the detection
# mechanism, and deliberately far above the pace anything actually
# changes at: a step is paced by LapseCoin confirmations, minutes at a
# time.
BACKSTOP_SECONDS = 300

# The floor between the start of one pass and the start of the next,
# however fast wake() keeps re-firing. Short enough that a real event
# still gets a prompt reaction (a person is not going to notice a
# two-second difference on a trade paced by two-minute confirmations),
# long enough that a burst of cheap gossip cannot turn this thread into
# a tight loop of real chain work. See the module docstring.
MIN_PASS_INTERVAL_SECONDS = 2.0

# Backoff after a chain is unreachable, so an outage does not turn into a
# tight retry loop against a public endpoint that is already struggling.
UNREACHABLE_BACKOFF_SECONDS = 120

# How often to retry a market backfill (Node.backfill_market_from) while
# this node's own book and accepted-fill store are still both completely
# empty. Only fires in that narrow condition, so this is a bootstrap aid,
# not a replacement for gossip: once anything at all has arrived, by
# gossip or by one successful backfill, this stops trying.
BACKFILL_RETRY_SECONDS = 300


class SwapWorker:
    """Drives active trades. One instance per node."""

    def __init__(self, node, xlm_keyfile, backstop_seconds=BACKSTOP_SECONDS,
                min_pass_interval=MIN_PASS_INTERVAL_SECONDS):
        self.node = node
        self.xlm_keyfile = xlm_keyfile
        self.backstop_seconds = backstop_seconds
        self.min_pass_interval = min_pass_interval
        self.running = False
        self._thread = None
        # Set by wake() whenever node sees something that could move a
        # trade forward; cleared right before each pass runs, not right
        # after wake() sets it, so an event arriving mid-pass is not lost
        # (see _run).
        self._wake = threading.Event()
        self._unreachable_until = 0.0
        self._last_error = ""
        self._passes = 0
        self._next_backfill_attempt = 0.0

    # -- wiring --------------------------------------------------------

    def _secrets(self):
        """(kek, xlm_seed), or (None, None) when the node is locked.

        Read fresh each time and never cached: holding a decrypted seed on
        this object for the life of the process would put it in every core
        dump for the sake of saving a file read between events that are
        minutes apart.
        """
        kek = getattr(self.node, "_kek", None)
        if kek is None:
            return None, None
        try:
            seed = xlm_mod.decrypt_seed(self.xlm_keyfile, kek=kek)
        except (OSError, ValueError):
            return None, None
        return kek, seed

    def _engine(self):
        return swap_engine.Engine(
            swap_engine.LapseAdapter(self.node),
            swap_engine.XLMAdapter(self.xlm_keyfile),
            self._secrets,
            min_confirm_depth=max(
                self.node.settings.get(settings_mod.SWAP_CONFIRM_DEPTH),
                swap_engine.MIN_CONFIRM_DEPTH))

    # -- lifecycle -----------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="swap-worker")
        self._thread.start()

    def stop(self):
        self.running = False
        self._wake.set()

    def wake(self):
        """Ask for a pass soon: something node saw might move a trade
        forward. Cheap and safe from any thread (an Event set), and safe
        to call when swaps are disabled or this worker was never started;
        the next pass (or none, if nothing is running) is what actually
        decides whether there is anything to do.
        """
        self._wake.set()

    def _run(self):
        try:
            self.recover()
        except Exception:
            log.exception("[swap] startup reconciliation failed; not sending "
                          "anything until it succeeds")
            # Deliberately not proceeding to the loop on this path would
            # strand the node forever, so the loop runs but reconcile is
            # retried per trade inside advance(), which re-reads the chain
            # before any send regardless.
        while self.running:
            self._run_one_iteration()

    def _run_one_iteration(self):
        """One trip round the loop: a pass, the debounce floor, then the
        wake/backstop wait. Split out from _run so the debounce and wait
        logic can be exercised directly, one call at a time, rather than
        only by spinning up the real background thread.
        """
        # Cleared before the pass runs, not after: node can call
        # wake() from another thread at any moment, including while
        # this pass is already in flight, and clearing afterwards
        # would silently discard an event that arrived during the
        # pass that noticed it too late to act on it. Clearing first
        # means such a wake() re-arms the flag during the pass, and
        # the wait() below then returns immediately instead of
        # sleeping through it.
        self._wake.clear()
        started = time.monotonic()
        try:
            self.run_once()
        except Exception:
            log.exception("[swap] worker pass failed")
        # Debounce floor: never start the next pass sooner than this
        # after starting this one, no matter how fast wake() keeps
        # re-firing (see MIN_PASS_INTERVAL_SECONDS). An idle node
        # never reaches this sleep at all, since wake() below simply
        # blocks for the rest of backstop_seconds instead.
        remaining = self.min_pass_interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        self._wake.wait(timeout=self.backstop_seconds)

    # -- work ----------------------------------------------------------

    def recover(self):
        """Rebuild every unfinished trade from the chains. Sends nothing."""
        trade_storage.ensure_tables()
        corrected = swap_engine.reconcile_all(self._engine())
        if corrected:
            log.info("[swap] startup: corrected %d step(s) from chain data",
                     corrected)
        return corrected

    def _maybe_backfill_market(self):
        """Try once, every BACKFILL_RETRY_SECONDS, to pull the order book
        and known accepted fills from a peer, but only while this node's
        own book still looks empty: a book with even one order still
        worth showing, gossiped or backfilled, is left to gossip from
        there. Needs no wallet and runs even while locked, since it only
        ever stores and relays what a peer sends, exactly like any other
        inbound gossip.

        "Still worth showing" means live: not cancelled, and not expired
        against this node's own current height. The previous version
        counted every Order row at all, cancelled or expired included -
        so a single stale, long-cancelled or long-expired order arriving
        by ordinary gossip (nothing unusual on a network with any real
        history at all) permanently convinced this node its book was
        "no longer empty" and disabled backfill for good, even though
        every order a taker could actually act on was still missing.
        That is a much worse failure than the cooldown below: it does
        not resolve itself no matter how long the node is left running,
        because nothing here ever re-opens the gate once anything at
        all, live or not, has been seen.

        The very first pass runs immediately at startup (see
        _next_backfill_attempt's own initial value), which races peer
        discovery on a freshly started node: this node's own pool can
        easily still be empty at that exact moment, well before it is
        actually empty of peers. The full BACKFILL_RETRY_SECONDS cooldown
        is only armed once a peer was actually found and asked, whether
        or not that request came back with anything - finding no peer
        yet is not the same "tried and got nothing" case that cooldown
        exists to avoid hammering, and arming it anyway used to strand a
        brand new node for a full five minutes for having lost a race
        that resolves itself within seconds.
        """
        trade_storage.ensure_tables()
        now = time.time()
        if now < self._next_backfill_attempt:
            return
        # getattr-guarded the same way pool is looked up below: every
        # real Node always has a view, but this runs unconditionally
        # from run_once's very first line, ahead of the wallet check, so
        # it must not assume anything about the caller beyond what it
        # actually needs.
        height = getattr(getattr(self.node, "view", None), "height", 0)
        if (trade_storage.Order.select()
                .where(trade_storage.Order.cancelled == False,   # noqa: E712
                       trade_storage.Order.expiry_block > height)
                .count()
                or trade_storage.FillResponse.select()
                       .where(trade_storage.FillResponse.accepted == True)  # noqa: E712
                       .count()):
            return
        pool = getattr(self.node, "pool", None)
        peer = pool.random() if pool is not None else None
        if not peer:
            log.debug("[swap] market backfill: book still empty but no "
                     "peer to ask yet")
            return
        self._next_backfill_attempt = now + BACKFILL_RETRY_SECONDS
        log.debug("[swap] market backfill: asking %s", peer)
        try:
            self.node.backfill_market_from(peer)
        except Exception:
            log.exception("[swap] market backfill from %s failed", peer)

    def run_once(self):
        """One pass over every active trade. Returns how many were touched."""
        if time.time() < self._unreachable_until:
            return 0
        self._maybe_backfill_market()

        trade_storage.ensure_tables()
        kek, seed = self._secrets()
        if kek is None:
            # Locked, or no trading wallet. Not an error: a node that
            # cannot sign simply does not send, which is the correct
            # behaviour rather than something to report every pass.
            return 0

        engine = self._engine()

        # Before advancing anything already known: decide every live fill
        # request against this node's own orders, and check this node's
        # own outstanding requests for an answer. Run first so a session
        # opened this pass is picked up by the very same pass's advance
        # loop below, rather than waiting a full poll interval to be
        # noticed twice.
        my_xlm_addr = xlm_mod.load_public_key(self.xlm_keyfile)
        if my_xlm_addr is not None:
            try:
                swap_engine.answer_fill_requests(
                    engine, self.node, my_xlm_addr,
                    max(self.node.settings.get(settings_mod.SWAP_CONFIRM_DEPTH),
                        swap_engine.MIN_CONFIRM_DEPTH),
                    min_trust=self.node.settings.get(
                        settings_mod.SWAP_AUTO_ACCEPT_MIN_TRUST))
            except Exception:
                # A bug answering requests must not stop trades already
                # running from being advanced.
                log.exception("[swap] answering fill requests failed")
            try:
                swap_engine.check_fill_responses(
                    self.node,
                    max(self.node.settings.get(settings_mod.SWAP_CONFIRM_DEPTH),
                        swap_engine.MIN_CONFIRM_DEPTH))
            except Exception:
                log.exception("[swap] checking fill responses failed")
            try:
                swap_engine.auto_match_orders(engine, self.node, my_xlm_addr)
            except Exception:
                log.exception("[swap] auto-matching this node's own orders failed")

        # Housekeeping, run after the above has had at least one chance
        # at whatever is currently pending: a node that was offline for
        # longer than a request's or an order's lifetime must not delete
        # either before this very pass has looked at them (see
        # market.FILL_REQUEST_MAX_AGE_SECONDS and market.prune_expired).
        try:
            market_mod.prune_expired(self.node.view.height)
            market_mod.prune_fill_requests()
            market_mod.prune_fill_responses()
        except Exception:
            log.exception("[swap] pruning the order book failed")

        # Nothing runs here to verify network-sourced trades: that chain
        # I/O now happens lazily, on whichever thread actually asks for a
        # specific address's trust (see trust._network_tally_by_counterparty),
        # bounded to that address's own accepted fills rather than a batch
        # of whatever happens to be oldest. A page render or a fill
        # decision pays for it directly, once, instead of a background
        # sweep paying for it on a schedule regardless of whether anyone
        # is looking.
        touched = 0
        # A trade stays in this set for as long as it is missing a leg,
        # however long that is: there is no separate status or pass for
        # one that has been stalled a long time (see trust.py's module
        # docstring for why not). Nothing needs to be emitted for a
        # bystander to eventually notice a stall either: the accepted
        # (FillRequest, FillResponse) pair that opened this trade was
        # already gossiped at accept time, which is all
        # swap_engine.verify_trade_against_chain needs to independently
        # recompute is_delinquent's verdict for itself, on demand.
        for trade in Trade.select().where(
                Trade.status.in_([TRADE_ACTIVE, TRADE_STALLED])):
            try:
                engine.advance(trade)
                touched += 1
            except swap_engine.Unreachable as e:
                # An outage says nothing about any trade, so nothing is
                # concluded and nothing is blamed. Back off rather than
                # hammering an endpoint that is already failing.
                self._last_error = str(e)
                self._unreachable_until = time.time() + UNREACHABLE_BACKOFF_SECONDS
                log.info("[swap] a chain is unreachable (%s); pausing %ds",
                         e, UNREACHABLE_BACKOFF_SECONDS)
                break
            except swap_engine.SwapError as e:
                self._last_error = str(e)
                log.debug("[swap] %s: %s", trade.session_id, e)
            except Exception:
                # One bad trade must not stop the others.
                log.exception("[swap] %s could not be advanced",
                              trade.session_id)

        self._passes += 1
        return touched

    # -- introspection -------------------------------------------------

    def status(self):
        return {
            "running": self.running,
            "unlocked": self._secrets()[0] is not None,
            "passes": self._passes,
            "paused_until": self._unreachable_until,
            "last_error": self._last_error,
        }
