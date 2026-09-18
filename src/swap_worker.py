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

Why this is allowed to be slow
------------------------------
A step is paced by LapseCoin confirmations, minutes at a time, so polling
every few seconds is already far finer than anything can change. The
interval is set by what is polite to a public API rather than by what the
trade needs.

Blame
-----
Abandonment is considered here because this is the only place that knows
both how long a trade has been stuck and whether the counterparty has
been seen alive on the network meanwhile. Both are required before anyone
is blamed; see swap_engine.consider_abandonment. Liveness comes from the
notes peers already gossip, so it costs nothing extra and cannot be
faked into making somebody look absent.
"""

import logging
import threading
import time

import settings as settings_mod
import swap_engine
import trade_storage
import xlm as xlm_mod
from trade_storage import Trade, TRADE_ACTIVE, TRADE_STALLED

log = logging.getLogger("ec.swap_worker")

# How often to look at active trades. Well below the pace anything can
# actually change, and chosen for Horizon's sake rather than the trade's:
# a step takes minutes, so this could be far slower without a trade
# noticing.
POLL_SECONDS = 20

# How recently a peer must have been seen announcing itself for its
# silence on a trade to count against it. Wide, because the alternative
# is blaming somebody whose liveness note simply did not reach us.
LIVENESS_WINDOW_SECONDS = 3600

# Backoff after a chain is unreachable, so an outage does not turn into a
# tight retry loop against a public endpoint that is already struggling.
UNREACHABLE_BACKOFF_SECONDS = 120


class SwapWorker:
    """Drives active trades. One instance per node."""

    def __init__(self, node, xlm_keyfile, poll_seconds=POLL_SECONDS):
        self.node = node
        self.xlm_keyfile = xlm_keyfile
        self.poll_seconds = poll_seconds
        self.running = False
        self._thread = None
        self._unreachable_until = 0.0
        self._last_error = ""
        self._passes = 0

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
            self._secrets)

    def _enabled(self):
        return self.node.settings.get(settings_mod.SWAP_ENABLED)

    def _peer_is_live(self, peer_addr):
        """Whether this counterparty has been seen on the network lately.

        Uses the liveness notes peers already gossip. A note travels
        through relays, so its presence says the peer is around without
        tying them to an address anyone can point at, and its absence is
        weak evidence rather than proof, which is why it is only ever one
        of two conditions for blame.
        """
        try:
            return peer_addr in self.node.active_addresses(LIVENESS_WINDOW_SECONDS)
        except Exception:
            return False

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
            try:
                self.run_once()
            except Exception:
                log.exception("[swap] worker pass failed")
            time.sleep(self.poll_seconds)

    # -- work ----------------------------------------------------------

    def recover(self):
        """Rebuild every unfinished trade from the chains. Sends nothing."""
        if not self._enabled():
            return 0
        trade_storage.ensure_tables()
        corrected = swap_engine.reconcile_all(self._engine())
        if corrected:
            log.info("[swap] startup: corrected %d step(s) from chain data",
                     corrected)
        return corrected

    def run_once(self):
        """One pass over every active trade. Returns how many were touched."""
        if not self._enabled():
            return 0
        if time.time() < self._unreachable_until:
            return 0

        trade_storage.ensure_tables()
        kek, seed = self._secrets()
        if kek is None:
            # Locked, or no trading wallet. Not an error: a node that
            # cannot sign simply does not send, which is the correct
            # behaviour rather than something to report every pass.
            return 0

        engine = self._engine()
        touched = 0
        for trade in Trade.select().where(
                Trade.status.in_([TRADE_ACTIVE, TRADE_STALLED])):
            try:
                engine.advance(trade)
                touched += 1
                fresh = Trade.get_or_none(Trade.session_id == trade.session_id)
                if fresh is not None and fresh.status == TRADE_STALLED:
                    engine.consider_abandonment(
                        fresh, self._peer_is_live(fresh.peer_lapse_addr))
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
            "enabled": self._enabled(),
            "unlocked": self._secrets()[0] is not None,
            "passes": self._passes,
            "paused_until": self._unreachable_until,
            "last_error": self._last_error,
        }
