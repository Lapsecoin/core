"""Block cycle orchestrator.

One cycle:
  1. drain queue          inbound txs and blocks from net_in_q
  2. sync, if triggered   only on evidence we're behind, or on silence
  3. vdf.evaluate()       ~120s, draining throughout, abandoned early once it
                          provably can't land in time (_should_abandon)
  4. assemble + spread    own candidate enters propagation (gossip.py)
  5. pick winner          lowest vdf_output among the candidates in hand
  6. commit               swap ChainState, persist, publish view

Nothing in the cycle waits, but a height still gets a real draw: after
adopting a block we start on the next height immediately and keep the
previous one open for a short window, during which a candidate with a
lower vdf_output still takes it (_reorg_to_sibling). The draw is what
decides a height by the evaluation each builder paid for rather than by
who reached us first; building on the next height throughout is what makes
giving it a window cost nothing.

The window runs from the first valid candidate anyone produced for that
height (open_draw), not from our own completion, so it is the same
interval on every node. Anchored to our own completion it would stretch
by however long we took, handing a slower node a longer window and
cancelling out the speed the chain is supposed to pay for. Finishing
outside it is losing the height, which is also the whole basis of
_should_abandon: work that cannot land inside the window cannot land at
all, so it is dropped in favour of the next height.

Flask threads read node.view (a NodeView snapshot). The node loop is the
sole writer; every mutation publishes a new snapshot atomically.
"""

import collections
import json
import os
import logging
import queue
import statistics
import secrets as _secrets
import state as state_mod
import threading
import time

import block as block_mod
import crypto
import gossip as gossip_mod
import mempool as mempool_mod
import settings as settings_mod
import tx as tx_mod
import vdf as vdf_mod
from cachetools import LRUCache
from chainstate import ChainState
from params import DB_PATH
from storage import Storage

log = logging.getLogger("ec.node")
_rng = _secrets.SystemRandom()

# Sync is event-driven, not scheduled. Two things can tell a node it is
# behind, and only two:
#
#   1. An inbound block at a height above our tip. This already reaches
#      every node for free, carried by propagation, and it names a peer
#      who has the chain. See _handle_inbound_block.
#   2. Silence. If no block arrives for meaningfully longer than the pace
#      the chain itself is running at, either the network stalled or we
#      are isolated, and no inbound block is ever going to tell us that.
#
# Polling on a fixed interval was a way of compensating for throwing (1)
# away. It scaled badly for what it bought: picking a peer at random, a
# node with k of N peers ahead needs ~N/k polls to stumble onto one, so
# detection got *slower* as the network grew, while the cost of each poll
# grew with chain length.
#
# The silence threshold is this multiple of the chain's own recent median
# block interval (block_mod.block_time_stats, measured, not assumed), so
# it tracks whatever pace the network is actually running at rather than
# a hardcoded seconds value. Above 1 so ordinary block-to-block jitter
# doesn't read as silence.
# A reorg that replaced this many blocks or more is worth remembering. One
# block below it is the draw settling a tie, which happens constantly and
# is not what anyone is looking for when they ask how deep reorgs go.
REORG_NOTABLE_DEPTH = 2

SILENCE_MULTIPLE = 3.0

# Fallback for the silence threshold before the chain is long enough to
# have a median interval of its own (fresh node, first blocks).
SILENCE_FALLBACK_SECONDS = 600.0

# A background probe of one *random* peer, once per cycle.
#
# Blocks only tell us what our own peers choose to forward. An eclipsed
# node, or one partitioned onto a consistent but inferior fork, sees a
# healthy stream and never goes silent, so no block-driven trigger ever
# fires. Random, because best-known is exactly who an eclipsing attacker
# would arrange for us to keep asking.
#
# Once per cycle because a probe is now one GETINFO datagram that ends on
# the work comparison. Frequency was never what made the old polling
# expensive. Every poll unconditionally ran an O(log chain) fork search
# and a fetch, and that is what had to go. At a datagram a cycle the cost
# is a rounding error against a ~120s evaluation, so there is no reason to
# detect an eclipse in twenty block times when one will do.

# How much of a fetch to do before handing the loop back. A sync of many
# blocks used to run to completion inline, which meant minutes during which
# the node drained nothing and forwarded nothing, and under a stem that is
# worse than slow for us, it is destructive for everybody: a hop handed to a
# node in that state dies there, and the sender only finds out by rework. So
# a pass takes a bounded bite and returns; we are still behind, the trigger
# fires again next time round, and propagation keeps flowing meanwhile.
SYNC_PAGES_PER_PASS = 2

# GETINFO probe timeout for a sync attempt. The probe is one round trip
# and the real one takes milliseconds; this only bounds the case where a
# peer has gone quiet but hasn't been struck yet.
SYNC_INFO_TIMEOUT_SECONDS = 2.0

# Echo deadline before a node has measured any of its own round trips (see
# Node._echo_deadline_seconds). Only ever used on a node that has just
# started and originated something before seeing anything come back, and
# only decides how long to wait before re-sending, so erring long costs a
# delayed retry and erring short costs one redundant flood.
ECHO_BOOTSTRAP_SECONDS = 10.0
ECHO_MIN_SECONDS       = 2.0

# How often to log that the VDF is still running. Without this, the default
# INFO log goes quiet for the entire ~2-3 minute wait between "[vdf] starting"
# and "[vdf] proof ready", which reads as hung rather than working.
VDF_HEARTBEAT_INTERVAL_SECONDS = 30

# Recent-state cache: lets a shallow reorg resume from an already-computed
# state instead of replaying the whole chain from genesis (see
# _resume_point). Routine reorgs here are shallow, a lost race resolves
# within a block or two, ties are the common case, not sustained multi-
# height divergence (see the whitepaper's section on the VDF lottery).
# Anything deeper than this window falls back to a full replay, which is
# also the right conservative behavior for what would be a genuinely
# abnormal, out-of-scope event (a sustained partition or majority
# attacker). Not something worth building fast-path machinery for.
RECENT_STATE_CACHE_SIZE = 20

# Verdicts remembered per tip (see Node._judged). Only needs to cover the
# distinct blocks that can plausibly show up for one height, which is a
# handful of real candidates plus whatever noise arrives alongside them.
JUDGED_CACHE_SIZE = 10_000


# ---------------------------------------------------------------------------
# Tail validation (pure, no node state touched)
# ---------------------------------------------------------------------------

def _validate_tail(tail, prefix):
    """Validate new blocks against a trusted prefix, building the resulting
    ChainState along the way. Pure: nothing is mutated.

    Returns (True, None, cs) or (False, error_string, None). The caller
    reuses the returned cs directly rather than deriving it a second time
    (e.g. via a separate ChainState.from_chain(prefix + tail) call), which
    would silently redo this same replay.
    """
    cs = ChainState.from_chain(prefix) if prefix else ChainState.from_genesis()
    for blk in tail:
        ok, err, cs = cs.validate_and_apply(blk)
        if not ok:
            return False, f"invalid block at {blk.get('height')}: {err}", None
    return True, None, cs


# ---------------------------------------------------------------------------
# NodeView: read-only snapshot for Flask threads
# ---------------------------------------------------------------------------

class NodeView:
    """Immutable snapshot of node state. Published after every block commit.
    Flask reads node.view; one reference swap, GIL-atomic, no lock needed.
    """
    __slots__ = ("chain", "height", "tip", "genesis_hash", "state")

    def __init__(self, cs):
        self.chain        = cs.chain
        self.tip          = cs.tip
        self.height       = cs.height
        self.genesis_hash = cs.genesis_hash
        self.state        = cs.state.snapshot()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node:

    def __init__(self, keyfile, public_key, gossip, syncer, pool, net_in_q,
                 db_path=None):
        self.keyfile      = keyfile
        self.pk           = public_key
        self.pk_hex       = public_key.hex()
        self.addr         = crypto.public_key_to_address(public_key)
        self.gossip       = gossip
        self.syncer       = syncer
        self.pool         = pool
        self.net_in_q     = net_in_q
        self.mempool      = mempool_mod.Mempool()
        self.storage      = Storage(db_path or DB_PATH)
        self.settings     = settings_mod.Settings(self.storage)
        self._privacy_key_lock = threading.Lock()

        # The height whose draw is still open, and when it closes. A draw
        # is the whole point of the vdf_output tie-break: among candidates
        # for one height, the lowest output wins, and that has to be given
        # a moment to resolve. See _reorg_to_sibling.
        self._draw_height = None
        self._draw_closes = 0.0
        # When the open draw was anchored. Nothing in the cycle reads this;
        # it is what says how long a running window actually is, which is
        # otherwise unobservable from outside (only its end is stored).
        self._draw_anchor = 0.0
        self.running      = False
        self._kek         = None
        self._loop_thread = None
        self._cycle_count = 0
        # Set by _handle_inbound_block when a peer shows evidence of a
        # higher chain: the address to sync from next. Evidence, not proof
        #, see that method. Consumed and cleared by _run_cycle.
        self._sync_hint = None
        self._sync_hint_height = 0

        # Hashes already judged against the current tip, so a block replayed
        # under a fresh transport msg_id doesn't buy a repeat of the VDF
        # verification behind block_mod.validate. Cleared whenever the tip
        # moves, since that is exactly when a previous verdict stops
        # applying. Only ever an optimisation: a miss costs a re-check,
        # never a wrong answer.
        #
        # An LRU rather than a capped dict. A dict that simply stopped
        # accepting entries once full could be switched off by anyone
        # willing to send ten thousand junk hashes, which is precisely the
        # traffic it exists to absorb; here that junk just ages out again.
        self._judged_at_tip = LRUCache(maxsize=JUDGED_CACHE_SIZE)
        self._judged_tip = None

        # Last background probe. Spaced by the chain's own pace rather than
        # left to fire on every loop tick, the cost of a probe is low, not
        # zero, and once per block is already as fine-grained as the thing
        # it is watching for.
        self._last_probe = time.monotonic()

        # item_hash -> (item, kind, spread_at) for things we originated and
        # have not yet seen come back from anyone. A stem hands an item to a
        # single peer and forgets it; if that peer is a dead end (its only
        # link is back to us) or the datagram is simply lost, the item stops
        # there and nobody else ever hears about it. We can't detect either
        # case from the send side, but we can notice that it never came
        # back, see _retry_unconfirmed_spreads. This is what makes the
        # stem safe on a graph we don't get to see.
        self._unconfirmed_spreads = {}

        # Measured seconds between spreading something and seeing it return
        # from a peer. The retry deadline comes from these rather than a
        # guessed number; ECHO_BOOTSTRAP_SECONDS covers the window before
        # this node has measured any of its own.
        self._echo_seconds = collections.deque(maxlen=50)

        # When we last saw any block at all. Silence beyond the chain's own
        # measured pace is the only other thing worth polling on: it means
        # either the network stalled or we're isolated, and both are cases
        # no inbound block will ever tell us about.
        self._last_block_seen = time.monotonic()

        # When we last polled *because* of silence, kept separate from
        # _last_block_seen so a poll that finds nothing doesn't read as a
        # block having arrived. Bounds silence to one poll per threshold
        # rather than one per loop tick.
        self._last_silence_poll = 0.0

        # height -> (State snapshot, cumulative_iterations) for the last
        # RECENT_STATE_CACHE_SIZE heights this node has actually committed.
        # Refreshed on every commit (_commit and apply_better_chain), stale
        # entries above a reorg's fork point purged there too. Not
        # persisted across restarts. It exists to avoid replaying a
        # shallow reorg from genesis, and self-refills within a few
        # cycles of normal running regardless; an empty cache right after
        # startup just means the fallback (full replay) applies until it
        # does. See RECENT_STATE_CACHE_SIZE above for why this stays small.
        self._recent_states = collections.OrderedDict()

        # Real wall-clock seconds this node itself spent on its last 30 VDF
        # evaluations, recorded the moment each one finishes, regardless of
        # whether the resulting candidate goes on to win its fork race. The
        # chain only ever holds whichever block won each height, so deriving
        # "this machine's build time" from chain timestamps would silently
        # drop every attempt that lost a race and skew toward other builders'
        # numbers entirely. This is local, in-memory, per-node knowledge,
        # nothing else has it, so it can't be reconstructed from chain data.
        self._own_build_seconds = collections.deque(maxlen=30)
        self._load_own_build_seconds()

        self.cs   = self._load_cs()
        self.view = NodeView(self.cs)

        # Short, human-readable line for the GUI status window / anything
        # else that wants "what is this node doing right now" without
        # scraping the log file.
        self.status_line = "starting"

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    _OWN_BUILD_SECONDS_META_KEY = "own_build_seconds"

    def _load_own_build_seconds(self):
        """Restore _own_build_seconds across restarts, so the odds page and
        own_block_time_ratio() don't sit empty for up to 30 cycles (an hour)
        after every restart. Best-effort: any parse failure just starts
        fresh, same as a node that's never built before."""
        raw = self.storage.get_meta(self._OWN_BUILD_SECONDS_META_KEY)
        if not raw:
            return
        try:
            self._own_build_seconds.extend(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("[startup] discarding unreadable own_build_seconds meta")

    def _save_own_build_seconds(self):
        self.storage.set_meta(self._OWN_BUILD_SECONDS_META_KEY,
                              json.dumps(list(self._own_build_seconds)))

    def _load_cs(self):
        """Load or create ChainState from storage."""
        stored = self.storage.load_all_blocks()
        if not stored:
            cs = ChainState.from_genesis()
            self.storage.save_block(cs.chain[0])
            log.info("[startup] genesis created")
            return cs

        # Backfill tx_bytes for blocks from older databases.
        for blk in stored:
            if "tx_bytes" not in blk:
                blk["tx_bytes"] = sum(tx_mod.tx_size(t)
                                       for t in blk.get("transactions", []))

        if self.storage.state_exists():
            s = state_mod.State.from_snapshot(*self.storage.load_state())
            cs = ChainState.from_storage(stored, s)
        else:
            cs = ChainState.from_chain(stored)

        log.info("[startup] loaded our chain up to block %d, tip %s",
                 cs.height, cs.tip["hash"][:12])
        return cs

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def advertised_addr(self):
        """The address this node tells peers about, which is not
        necessarily the one it builds with.

        The builder address inside a block is public by construction, it
        has to be, or the block can't be paid, so nothing here hides a
        wallet, and peers have other ways to infer the link anyway. What it
        does is stop handing that link to every peer that asks for our tip.

        Whatever is advertised must be an address this operator can
        actually receive at: peers pay advertised addresses (see
        uptime_rewarder), so advertising something unspendable would burn
        other people's coins, not protect ours. So privacy mode uses a real
        second keypair, generated once and encrypted to disk beside the
        main one, never a throwaway. Until that key exists we advertise
        nothing rather than something nobody can pay.
        """
        if not self.settings.get(settings_mod.PRIVATE_ADDRESS):
            return self.addr
        configured = self.settings.get(settings_mod.ADVERTISED_ADDRESS)
        if configured:
            return configured
        return self.storage.get_meta(self._PRIVACY_ADDR_META) or ""

    _PRIVACY_ADDR_META = "privacy_address"

    @property
    def privacy_addr(self):
        """The generated privacy address, if it exists yet, regardless of
        whether the privacy switch is currently on. ensure_privacy_key runs
        unconditionally at startup, so this is normally always set; the
        settings page uses it to show what flipping the switch will start
        using, without the operator having to turn it on first to see it."""
        return self.storage.get_meta(self._PRIVACY_ADDR_META) or ""

    @property
    def privacy_keyfile(self):
        return self.keyfile + ".privacy"

    def ensure_privacy_key(self, passphrase):
        """Create the privacy keypair if it doesn't exist yet.

        Created at startup, where the passphrase is still in hand, and
        written by the ordinary save_key with its own salt, so it is a
        standalone key file, openable with the passphrase alone and not
        chained to the main one. An earlier version encrypted it under the
        main key file's KEK purely because that was what a *running* node
        had resident; that coupled the two files for no reason, and the
        answer was to create it at the moment the passphrase exists rather
        than to invent a way around not having it.

        Made unconditionally, not only when privacy is on, so the switch
        works the instant it is flipped instead of waiting for a restart to
        have an address to advertise. One small file, written once.
        """
        with self._privacy_key_lock:
            existing = self.storage.get_meta(self._PRIVACY_ADDR_META)
            if existing and os.path.exists(self.privacy_keyfile):
                return existing
            sk, pk = crypto.generate_keypair()
            crypto.save_key(self.privacy_keyfile, sk, pk, passphrase)
            del sk
            addr = crypto.public_key_to_address(pk)
            self.storage.set_meta(self._PRIVACY_ADDR_META, addr)
            log.info("[privacy] created a separate address to advertise, "
                 "saved in %s -> %s",
                     self.privacy_keyfile, addr[:24])
            return addr

    def is_signing_active(self):
        return self._kek is not None

    def mark_tx_seen(self, tx_hash):
        return self.gossip.mark_seen(tx_hash)

    def own_vdf_median(self):
        """This node's own median real VDF build time, over its last 30
        actual attempts (own_build_seconds, see that field's docstring).
        None until this node has completed at least one build."""
        if not self._own_build_seconds:
            return None
        return statistics.median(self._own_build_seconds)

    def own_block_time_ratio(self):
        """This node's own median VDF build time as a ratio of the chain's
        own recent median block-to-block time (block_mod's
        BLOCK_TIME_MEDIAN_WINDOW, whoever actually built each of those
        blocks). Above 1 means this node is running slower than the
        network's recent real pace. None until this node has completed a
        build, or the chain has too few blocks for a chain-side median."""
        own_median = self.own_vdf_median()
        if own_median is None:
            return None
        chain = self.view.chain
        stats = block_mod.block_time_stats(chain, len(chain) - 1)
        if stats is None or not stats["median"]:
            return None
        return own_median / stats["median"]

    def get_info(self):
        v = self.view
        return {
            "height":       v.height,
            "tip_hash":     v.tip["hash"],
            "genesis_hash": v.genesis_hash,
            "mempool_size": self.mempool.size(),
            "address":      self.addr,
            "peer_count":   self.pool.count(),
            "total_minted": v.state.total_minted,
            "can_mint":     v.state.compute_can_mint(),
            "block_reward": v.state.compute_block_reward(),
            "block_time_ratio": self.own_block_time_ratio(),
            "status":       self.status_line,
        }

    def start(self, kek):
        self._kek         = kek
        self.running      = True
        self._loop_thread = threading.current_thread()
        log.info("[startup] node ready, our address is %s", self.addr)
        try:
            while self.running:
                try:
                    self._run_cycle()
                except Exception:
                    log.exception("[cycle] unhandled error, sleeping 1s")
                    time.sleep(1)
        finally:
            self._kek = None

    def stop(self):
        self.running = False

    def _validate_for_mempool(self, tx_dict):
        """Validate tx_dict against confirmed state plus this sender's
        already-pending mempool txs. Shared by submit_tx and
        _handle_inbound_tx so both acceptance paths agree."""
        probe = self.mempool.probe_state_for(tx_dict.get("from"), self.cs.state)
        return tx_mod.validate(tx_dict, probe)

    def submit_tx(self, tx_dict):
        """Validate and add a tx. Node loop thread only."""
        assert threading.current_thread() is self._loop_thread
        ok, err = self._validate_for_mempool(tx_dict)
        if not ok:
            log.debug("[tx] rejected  reason=%s  from=%s",
                      err, tx_dict.get("from", "?")[:24])
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            log.debug("[tx] mempool add failed  reason=%s  from=%s", h,
                      tx_dict.get("from", "?")[:24])
            return False, h
        self._spread(tx_dict, gossip_mod.KIND_TX, h)
        log.info("[tx] accepted %s from %s", h[:12],
                 tx_dict.get("from", "?")[:24])
        return True, h

    def submit_tx_from_api(self, tx_dict, timeout=5):
        """Thread-safe bridge: enqueue tx, block until the loop replies."""
        reply = queue.Queue(maxsize=1)
        try:
            # Bounded put: net_in_q has a cap now (see main.py), so an
            # unbounded put could hang this request thread outright instead
            # of failing it.
            self.net_in_q.put({"type": "submit_tx", "tx": tx_dict, "reply": reply},
                              timeout=timeout)
        except queue.Full:
            return False, "node busy (inbound queue full)"
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return False, "node busy (timeout)"

    def build_and_sign_tx(self, to_outputs, fee=0, passphrase=None, memo=""):
        """Build, sign, and return a plaintext transaction from this node's
        own address. fee is sender-chosen (default 0; callers building a
        wallet UI should let the user pick a competitive fee). memo is an
        optional short plaintext note, see tx.MAX_MEMO_BYTES; it is public
        and permanent like the rest of the transaction, not encrypted."""
        if not passphrase:
            raise ValueError("passphrase is required to sign a transaction")
        kek = crypto.derive_kek(self.keyfile, passphrase)
        try:
            return self._build_and_sign_tx_with_kek(to_outputs, fee, kek, memo)
        finally:
            del kek

    def build_and_sign_tx_internal(self, to_outputs, fee=0, memo=""):
        """Same as build_and_sign_tx, but for background node-internal
        callers (e.g. the uptime rewarder) that run inside this same
        process while the node is up, reuses the kek already resident in
        memory from start() instead of asking for the passphrase again,
        since re-deriving it would need the plaintext passphrase this node
        never retains past startup. Raises if the node isn't running
        (is_signing_active() false), same failure mode as a missing
        passphrase would give the passphrase-based path."""
        if self._kek is None:
            raise ValueError("node is not running (no signing key resident)")
        return self._build_and_sign_tx_with_kek(to_outputs, fee, self._kek, memo)

    def _build_and_sign_tx_with_kek(self, to_outputs, fee, kek, memo=""):
        v         = self.view
        committed = v.state.get_nonce(self.addr)
        pending   = self.mempool.pending_nonce(self.addr)
        nonce     = max(committed, pending) + 1
        sk = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        t  = tx_mod.create(self.addr, self.pk_hex, to_outputs, nonce, fee, sk, memo=memo)
        del sk
        return t, fee

    # ------------------------------------------------------------------
    # Block cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        self._cycle_count += 1
        # Captured, not discarded: a peer's candidate for the height we're
        # about to build can legitimately arrive in this brief window too
        # (right at cycle start, before the wait loop below even begins),
        # and dropping it here would silently skip it, see
        # _consider_inbound_block, called on these once `cs` is known.
        pre_cycle_blocks = self._drain_queue()
        self._retry_unconfirmed_spreads()

        self._sync_if_triggered()
        cs = self.cs   # local alias; can change under sync
        pruned = self.mempool.prune_stale(cs.state)
        peers = self.pool.count()
        log.info("[block %d] building on %s  (%d peer%s, %d transaction%s waiting%s)",
                 cs.height + 1, cs.tip["hash"][:12],
                 peers, "" if peers == 1 else "s",
                 self.mempool.size(), "" if self.mempool.size() == 1 else "s",
                 f", {len(pruned)} dropped as stale" if pruned else "")
        if peers == 0:
            # Distinct from peers=0 in the line above, which reads as one
            # number among several. A node with no peers is building a
            # chain nobody will ever see, and it is worth saying that
            # outright every cycle it stays true rather than leaving an
            # operator to infer it.
            log.warning("[vdf] no peers: building alone, this chain reaches "
                        "nobody until one is found")
        self.status_line = f"computing VDF for block {cs.height + 1}"

        # Run VDF in a background thread so the node loop stays responsive
        # to tx submissions and peer messages during the ~120s evaluation.
        # `handle` lets us abort it early, see _should_abandon and
        # vdf.EvaluationHandle's docstring.
        import concurrent.futures as _cf
        accumulated_blocks = []
        iterations = block_mod.get_vdf_iterations(cs.chain)
        vdf_start = time.monotonic()
        handle = vdf_mod.EvaluationHandle()

        # No waiting anywhere in here. Two things can end this cycle:
        # our own VDF finishing, or the tip moving on without us.
        #
        # The version before this idled: once a competing candidate for our
        # height showed up, it sat out a settle window collecting stragglers
        # before picking. That handed the block's builder a free head start
        # on the next height. They were computing it while we waited. And
        # it wasn't buying anything, because a late same-height candidate is
        # still handled perfectly well after the fact: it is simply a chain
        # of equal cumulative work with a lower vdf_output, which is exactly
        # what is_better_than already resolves. Settling a height and
        # working on the next one are independent, so they shouldn't block
        # each other. See _reorg_to_sibling.
        # Process anything that arrived in the drain at the very top of
        # this cycle (before `cs` was even known) through the same path
        # the wait loop below uses, see pre_cycle_blocks's comment.
        for blk in pre_cycle_blocks:
            self._consider_inbound_block(blk, cs, accumulated_blocks)

        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(
                vdf_mod.evaluate,
                block_mod.vdf_challenge(cs.tip["hash"], self.addr), iterations, handle)
            last_heartbeat = vdf_start
            while True:
                if _fut.done():
                    break

                self._retry_unconfirmed_spreads()
                new_blocks = self._drain_queue(timeout=1)
                for blk in new_blocks:
                    self._consider_inbound_block(blk, cs, accumulated_blocks)

                now = time.monotonic()
                if now - last_heartbeat >= VDF_HEARTBEAT_INTERVAL_SECONDS:
                    elapsed = now - vdf_start
                    log.info("[block %d] still building, %.0fs so far",
                             cs.height + 1, elapsed)
                    self.status_line = (f"computing VDF for block {cs.height + 1} "
                                        f"({elapsed:.0f}s elapsed)")
                    last_heartbeat = now

                # Mid-wait sync happens on evidence, not on a timer: a
                # block from a height above ours landed in the drain above
                # and set the hint. Nothing arriving means nothing to do.
                if self._sync_if_triggered():
                    handle.cancel()

                if self.cs is not cs:
                    # The tip moved on (a sync, or a sibling that beat our
                    # own tip). What we're computing is for a parent that
                    # is no longer ours, so it can never be committed,
                    # stop paying for it and start the next height now
                    # rather than finding out later.
                    handle.cancel()
                    break

                if self._should_abandon(cs, accumulated_blocks, vdf_start):
                    handle.cancel()
                    break

            own_cancelled = handle._cancelled
            if not own_cancelled:
                try:
                    vdf_out, vdf_proof, vdf_seconds = _fut.result()
                except vdf_mod.Cancelled:
                    own_cancelled = True
            if own_cancelled and not _fut.done():
                # cancel() was called but the underlying prove() hasn't
                # unwound yet. Block until it does; chiavdf has no async
                # cancel, only "stop at the next poll of the shutdown
                # file", so this wait is normally short but not zero.
                try:
                    _fut.result()
                except vdf_mod.Cancelled:
                    pass

        if own_cancelled:
            log.info("[block %d] gave up on our own: it can no longer arrive "
                     "while the draw is open", cs.height + 1)
        else:
            log.info("[block %d] our proof is ready, took %.0fs",
                     cs.height + 1, vdf_seconds)
            self._own_build_seconds.append(vdf_seconds)
            self._save_own_build_seconds()

        if self.cs is not cs:
            # A mid-wait sync check adopted a better chain out from under us.
            # The VDF we just computed (or abandoned) was for cs.tip, which
            # is no longer our tip, apply_block() trusts previous_hash
            # without re-checking it, so committing this candidate would
            # silently splice a block onto the wrong parent. Discard it; the
            # next cycle starts fresh against the new tip.
            log.info("[block %d] dropped: a better chain arrived while we "
                     "were building, so this was built on the wrong parent",
                     cs.height + 1)
            return

        if own_cancelled:
            # We never got a candidate of our own this cycle. Resolve the
            # height from whatever valid peer candidates arrived instead.
            winner, relay = self._pick_winner(cs, None, accumulated_blocks)
            if winner is None:
                return
            self._commit(winner, relay=relay)
            return

        candidate = block_mod.assemble(cs.tip, self.mempool.all_txs(), self.addr, iterations)
        candidate["vdf_output"]    = vdf_out
        candidate["vdf_proof"]     = vdf_proof
        candidate["vdf_iterations"] = iterations
        candidate["hash"]          = block_mod.block_hash(candidate)
        ok, err = block_mod.validate(candidate, cs.state.snapshot(), cs.chain)
        if not ok:
            log.error("[vdf] self-produced block failed validation: %s", err)
            return
        self._spread(candidate, gossip_mod.KIND_BLOCK, candidate["hash"])

        # Flush whatever landed while the VDF was finishing, without
        # blocking, then decide. Anything that arrives after this point is
        # not lost: a same-height sibling with a lower vdf_output is just a
        # better chain, and _reorg_to_sibling takes it locally, for free.
        peer_blocks = accumulated_blocks + self._drain_queue()
        # Pass cs explicitly; self.cs may have advanced during drain if syncer fired.
        winner, relay = self._pick_winner(cs, candidate, peer_blocks)
        if winner is None:
            return
        self._commit(winner, relay=relay)

    def _consider_inbound_block(self, blk, cs, accumulated_blocks):
        """Record an inbound block as a candidate for this cycle's draw,
        but only once it has actually validated.

        Nothing unvalidated may go in this list. _should_abandon reads it to
        decide whether the height is contested, and a contested height can
        cancel an evaluation already most of the way through, so an
        unchecked entry here means one crafted datagram throws away ~120s of
        somebody's real work. It also keeps the list bounded by what the
        network can actually produce rather than by what anyone can send.
        """
        if self._validate_candidate(blk, cs):
            # First valid candidate for this height starts its draw, even
            # though we have not adopted anything yet. That is the anchor
            # every node shares; see open_draw.
            self.open_draw(blk["height"])
            accumulated_blocks.append(blk)

    def _should_abandon(self, cs, accumulated_blocks, vdf_start):
        """Whether to stop computing this height because we can no longer
        plausibly land in time to matter.

        Only asked once the height is contested: with no competitor in hand
        there is nothing to lose by finishing, and everything to lose by
        stopping.

        Once contested, the only thing our candidate can still win is the
        draw, and the draw closes at a fixed moment (open_draw). So the
        question is exactly whether we can finish before it does. Measured
        on both sides, not assumed: how long our own evaluations actually
        take (_own_build_seconds) against how much of the window is left.

        Measuring against a whole block interval instead, as this did
        before, is far too generous: a node running a minute behind the
        leader would never abandon, would finish a block that missed every
        draw it could have entered, and would start the next height a
        minute late, every height, forever. Nothing it computed could ever
        be used. Abandoning immediately costs it nothing it could have kept
        and puts it back on the clock for the next height.

        Erring either way is survivable, which is why a rough estimate is
        enough: abandoning slightly early forfeits a long-odds draw,
        abandoning slightly late costs part of a head start on the next
        height. Both are bounded; idling was not.
        """
        if not accumulated_blocks:
            return False
        own_median = self.own_vdf_median()
        if own_median is None:
            return False   # never finished one; no basis to predict this one
        if self._draw_height != cs.height + 1:
            return False   # no draw anchored for the height we're building
        now = time.monotonic()
        remaining   = own_median - (now - vdf_start)
        window_left = self._draw_closes - now
        if window_left <= 0:
            # Already too late for anything, which a plain
            # remaining > window_left misses: an evaluation running past
            # its own estimate has a negative `remaining` too, and if it
            # is further past its estimate than the window is past its
            # deadline the comparison says keep going, on a height that
            # closed. That is the exact waste this check exists to stop.
            return True
        return remaining > window_left

    def open_draw(self, height):
        """Open the draw for `height`: until it closes, a better candidate
        for that height can still take the tip from the one we adopted.

        Anchored to the first valid candidate we see for that height, ours
        or a peer's, and never restarted while it runs. The anchor has to
        be an event every node observes at roughly the same moment, or the
        window is a different length for each of them: anchored to our own
        completion instead, a node that took 30s longer would keep
        collecting for 30s longer, so the slower a node is the more time it
        gets to be beaten, and the faster a node is the less its speed buys
        it. Speed is the thing this chain pays for, so the window must not
        stretch to accommodate whoever is behind. Finishing outside it is
        simply losing the height.

        Only a window that is still running is left alone. Matching on the
        height alone would be wrong: nothing resets _draw_height when a
        window expires, so after a reorg back below a height we already
        held, rebuilding to that height would find its own number still
        sitting there with a deadline long past, and that genuinely new
        contest would get no window at all.
        """
        now = time.monotonic()
        if self._draw_height == height and now < self._draw_closes:
            return
        self._draw_height = height
        self._draw_anchor = now
        self._draw_closes = now + self._draw_window_seconds()

    def _draw_window_seconds(self):
        """How long the window runs. The configured value, and nothing else.

        This used to widen itself from measurement, and the measurement was
        the wrong quantity. What it recorded was the gap between the first
        candidate for a height and each later one, which on a real network
        is not propagation at all: it is how far apart the builders are in
        speed. Feeding that back made the window grow to cover the very
        speed differences the draw is supposed to let decide the height, so
        a node with a genuine ten-second lead saw the window stretch to
        thirty and turn its lead into a coin flip. It pushed hardest in the
        wrong direction exactly where it mattered most.

        The quantity that does belong here is propagation, since a window
        shorter than that excludes a builder for being far away rather than
        slow. But propagation measures around half a second for an ordinary
        block, far under any sane setting, so there is nothing for an
        automatic widening to do that the operator's own number does not
        already cover. It is one number now, which is also one fewer thing
        to be wrong about.
        """
        return self.settings.get(settings_mod.DRAW_WINDOW_SECONDS)

    def _draw_is_open(self, height):
        return (self._draw_height == height
                and time.monotonic() < self._draw_closes)

    def _reorg_to_sibling(self, blk, cs):
        """Take a same-height alternative to our tip when it wins the draw.

        The draw is what the vdf_output tie-break is for: among candidates
        for one height the lowest output wins, so that who wins is decided
        by the evaluation each builder actually paid for and not by who
        happened to reach us first. It needs a window to resolve in, and
        fork choice alone cannot provide one: is_better_than only compares
        output between chains of *equal* work, so the moment anybody builds
        on top of the first block to arrive, that chain has strictly more
        work and every sibling loses regardless of its output. Left to fork
        choice, first arrival beats the draw, which hands the height to
        whoever is best connected, exactly what the tie-break exists to
        stop.

        So the window is explicit, and it is not a wait: we start building
        on the adopted tip immediately and keep doing so throughout, which
        is what makes it affordable. See open_draw.
        """
        if not self._draw_is_open(blk.get("height")):
            return False
        if blk.get("height") != cs.height or cs.height == 0:
            return False
        if blk.get("previous_hash") != cs.chain[-2]["hash"]:
            return False
        if blk.get("hash") == cs.tip["hash"]:
            return False
        # Settle the draw with a string compare before doing anything
        # expensive. A sibling whose output isn't lower cannot win, so
        # there is nothing to replay or verify, and without this, anyone
        # could make us re-derive chain state (and verify a VDF proof) once
        # per datagram just by sending same-height blocks.
        if block_mod.tie_break_key(blk) >= block_mod.tie_break_key(cs.tip):
            return False
        ok, _err = self.apply_better_chain(cs.chain[:-1] + [blk])
        if ok:
            log.info("[block %d] changed hands to %s, which proved a better "
                     "result for the same height",
                     blk["height"], blk["hash"][:12])
        return ok

    def _judged_cache(self, cs):
        """The verdict cache belonging to this tip, emptied if the tip has
        moved since it was filled. Both the read and the write go through
        here, so a write can never land in the previous tip's generation."""
        if self._judged_tip != cs.tip["hash"]:
            self._judged_at_tip = LRUCache(maxsize=JUDGED_CACHE_SIZE)
            self._judged_tip = cs.tip["hash"]
        return self._judged_at_tip

    def _judged(self, blk, cs):
        """Cached verdict for this block against this tip, or None."""
        return self._judged_cache(cs).get(blk.get("hash"))

    def _remember_judgement(self, blk, cs, verdict):
        h = blk.get("hash")
        if h:
            self._judged_cache(cs)[h] = verdict
        return verdict

    def _validate_candidate(self, blk, cs):
        """True if blk is a fully validated candidate for cs.height+1
        extending cs.tip.

        The single place a block is judged against a tip, and the only one.
        Three separate paths used to run block_mod.validate themselves
        (arrival, the cycle's candidate list, and picking the winner), and
        two of the three went straight past this cache, so one block
        arriving was verified three times over: three VDF proof checks and
        three passes over every signature it carries. At a full block that
        is most of a second of pure re-verification on the node loop, for
        an answer already sitting in a dict. The same hole made a replayed
        block free to send and expensive to receive, since nothing
        consulted a verdict before paying for it again.

        This is also the one gate standing between an attacker-crafted,
        zero-cost "block" message and aborting this node's own in-flight
        VDF (via _should_abandon, which this return value feeds). Crafting
        a fake block message costs nothing; the work it would cancel costs
        ~120s. A block that hasn't cleared real block_mod.validate() must
        never be allowed to influence that.

        Deliberately does not relay: _handle_inbound_block is the single
        place that decides propagation, and it has already made that call
        by the time a block reaches this list.
        """
        if blk.get("height") != cs.height + 1:
            return False
        if blk.get("previous_hash") != cs.tip["hash"]:
            return False
        cached = self._judged(blk, cs)
        if cached is not None:
            return cached
        ok, err = block_mod.validate(blk, cs.state.snapshot(), cs.chain)
        if not ok:
            log.debug("[vdf] rejected inbound candidate: %s", err)
        return self._remember_judgement(blk, cs, ok)

    def _pick_winner(self, cs, candidate, peer_blocks):
        """Return (best_block, relay). relay=True means it came from a peer.
        Returns (None, False) if there's no viable winner (candidate stale,
        or candidate is None and no peer block validated either, this
        cycle contributes nothing, which is fine, the next one starts fresh).

        cs: the ChainState candidate was built against; passed explicitly so
        this method is immune to self.cs advancing during the drain window.
        candidate: this node's own finished block, or None if it abandoned
        its own attempt this cycle (see _should_abandon).
        """
        tip = cs.tip

        valid_peers = []
        for blk in peer_blocks:
            # Through _validate_candidate, not a direct block_mod.validate:
            # everything here has almost certainly been judged already, on
            # arrival or when it entered the draw, and this is where that
            # verdict gets reused instead of re-derived. See that method.
            if not self._validate_candidate(blk, cs):
                continue
            log.debug("[vdf] peer block accepted  height=%d  hash=%s  builder=%s  tx=%d",
                      blk["height"], blk["hash"][:12],
                      (blk.get("builder") or "")[:24], len(blk.get("transactions", [])))
            valid_peers.append(blk)

        if candidate is not None and candidate.get("previous_hash") != tip["hash"]:
            log.warning("[vdf] candidate stale (tip advanced during drain), skipping cycle")
            candidate = None

        entrants = ([candidate] if candidate is not None else []) + valid_peers
        if not entrants:
            log.info("[block %d] nobody produced a usable one this round, "
                     "starting over", tip["height"] + 1)
            return None, False

        # Among all equally-valid same-height candidates (all proving the
        # same required iterations), the lowest vdf_output wins, the same
        # rule ChainState.is_better_than uses, so a node's own immediate
        # pick can't diverge from what syncer would settle on anyway.
        winner   = min(entrants, key=block_mod.tie_break_key)
        is_peer  = winner is not candidate
        log.info("[block %d] %s wins with %s, out of %d in the running",
                 tip["height"] + 1,
                 "a peer's block" if is_peer else "our block",
                 winner["hash"][:12], len(entrants))
        return winner, is_peer

    def _commit(self, blk, relay=False):
        """Append a validated block: update ChainState, persist, publish view."""
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        self.cs = self.cs.apply_block(blk)
        self._remember_state(self.cs)
        self.storage.save_block_and_state(blk, self.cs.state)
        self.mempool.remove_many(confirmed)
        self.view = NodeView(self.cs)

        # Nothing is propagated from here. A peer block was already passed
        # on when it arrived (_handle_inbound_block), and our own candidate
        # when we built it, re-sending at commit time would just be a
        # second copy of something the network already has.

        # The height we just took is now open for its draw: a better
        # candidate for it can still win until the window closes, while we
        # get on with the next height in the meantime. Our own finished
        # candidate closes it immediately (below), at that point we have
        # compared everything we were ever going to.
        self.open_draw(blk["height"])

        # Whether this node won the height is the one thing an operator
        # actually watches for, and reading it off `builder` meant
        # recognising your own address in a truncated string.
        won = blk.get("builder") == self.addr
        log.info("[block %d] %s  %s, %d transaction%s, built by %s",
                 blk["height"], "WE WON IT" if won else "accepted",
                 blk["hash"][:12], len(blk["transactions"]),
                 "" if len(blk["transactions"]) == 1 else "s",
                 (blk.get("builder") or "")[:24])
        self.status_line = f"block {blk['height']} {'won' if won else 'received'}"

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _drain_queue(self, timeout=0):
        """Drain net_in_q. Returns list of inbound block dicts."""
        assert threading.current_thread() is self._loop_thread
        blocks = []
        try:
            msg = self.net_in_q.get(block=timeout > 0, timeout=timeout or None)
            self._handle(msg, blocks)
        except queue.Empty:
            return blocks
        while True:
            try:
                self._handle(self.net_in_q.get_nowait(), blocks)
            except queue.Empty:
                break
        return blocks

    def _handle(self, msg, block_out):
        """Dispatch one queue message."""
        t = msg.get("type")
        if t == "block":
            self._handle_inbound_block(msg, block_out)
        elif t == "submit_tx":
            msg["reply"].put(self.submit_tx(msg["tx"]))
        elif t == "tx":
            self._handle_inbound_tx(msg)

    def _spread(self, item, kind, item_hash):
        """Originate an item and remember it until we see it come back from
        somebody other than whoever we handed it to."""
        stem_target = self.gossip.spread(item, kind, item_hash)
        self._unconfirmed_spreads[item_hash] = (
            item, kind, time.monotonic(), stem_target)

    def _note_echo(self, item_hash, sender=None):
        """An item we originated came back from someone else, which is
        evidence it actually got out. Also the only measurement of how long
        that takes that this node can make for itself.

        An echo from the peer we stemmed to is not evidence of anything.
        That peer already has the item by construction, so it can drop it on
        the floor and hand it straight back, and we would call the walk a
        success and never re-send. One datagram from the single node we
        chose to trust, and the block is gone. Requiring the echo to come
        from anyone else means swallowing an item quietly now takes a second
        peer of ours in on it.
        """
        entry = self._unconfirmed_spreads.get(item_hash)
        if entry is None:
            return
        if sender is not None and sender == entry[3]:
            log.debug("[gossip] ignoring echo from the peer we stemmed to")
            return
        del self._unconfirmed_spreads[item_hash]
        self._echo_seconds.append(time.monotonic() - entry[2])

    def _echo_deadline_seconds(self):
        """How long to wait for an echo before assuming the walk died.

        p95 of this node's own measured echo times once it has enough of
        them, so it tracks the network it is actually on. Doubling that
        leaves room for the ordinary tail without re-sending on every
        slightly-slow round."""
        if len(self._echo_seconds) < 5:
            return ECHO_BOOTSTRAP_SECONDS
        ordered = sorted(self._echo_seconds)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
        return max(ECHO_MIN_SECONDS, p95 * 2)

    def _retry_unconfirmed_spreads(self):
        """Re-send anything we originated that never came back.

        The retry floods directly instead of stemming again. The first
        attempt already spent what privacy a stem can buy, and by this point
        the walk has demonstrably failed once: another private hand-off
        would risk the same silent death, and for a block that means losing
        the whole evaluation. Delivery wins on the retry.
        """
        deadline = self._echo_deadline_seconds()
        now = time.monotonic()
        for h in list(self._unconfirmed_spreads):
            item, kind, spread_at, _stem_target = self._unconfirmed_spreads[h]
            if now - spread_at < deadline:
                continue
            log.info("[gossip] our %s %s was not echoed back after %.0fs, "
                     "sending it to everyone", kind, h[:12], now - spread_at)
            del self._unconfirmed_spreads[h]
            self.gossip.force_fluff(item, kind, h)

    def _silence_threshold(self):
        """How long without any block counts as silence, in seconds: a
        multiple of the pace the chain is actually running at, measured
        from its own timestamps rather than assumed."""
        chain = self.cs.chain
        stats = block_mod.block_time_stats(chain, len(chain) - 1)
        if stats is None or not stats["median"]:
            return SILENCE_FALLBACK_SECONDS
        return stats["median"] * SILENCE_MULTIPLE

    def _probe_spacing(self):
        """One background probe per block interval, measured from the chain
        rather than assumed."""
        chain = self.cs.chain
        stats = block_mod.block_time_stats(chain, len(chain) - 1)
        if stats is None or not stats["median"]:
            return SILENCE_FALLBACK_SECONDS
        return stats["median"]

    def _sync_if_triggered(self):
        """Sync only when something says to. Returns True if a better chain
        was adopted.

        Two triggers, in priority order:

        - A hint from an inbound block above our tip, which also names the
          peer that has it, so we ask that peer rather than guessing.
        - Silence past _silence_threshold(), the only case no inbound block
          can ever report. Here there is nobody to ask in particular, so we
          fall back to the highest peer we know of.
        - Otherwise a background probe of a *random* peer. Blocks arriving
          proves the network is alive, not that we are on its best chain:
          an eclipsed or partitioned node sees a healthy stream and never
          goes silent. This is the only trigger that looks outside the set
          currently feeding us, which is why it picks at random, and it is
          affordable every cycle because a probe that finds nothing is a
          single datagram.

        Either way the peer's answer is validated before it is believed, so
        a lying hint costs one failed attempt against the liar, nothing
        more. This is also why a claimed height is never a trigger on its
        own: claims are cheap, blocks are not.
        """
        assert threading.current_thread() is self._loop_thread
        peer = self._sync_hint
        self._sync_hint = None
        self._sync_hint_height = 0
        hinted = peer is not None

        if peer is None:
            now = time.monotonic()
            threshold = self._silence_threshold()
            silent = (now - self._last_block_seen >= threshold
                      and now - self._last_silence_poll >= threshold)
            if silent:
                # Nobody to ask in particular, so ask whoever claims most.
                peer = self._best_known_peer()
                reason = f"no block for {now - self._last_block_seen:.0f}s"
                self._last_silence_poll = now
            elif now - self._last_probe >= self._probe_spacing():
                # Deliberately random, not best-known: this exists to hear
                # from outside whatever set is currently feeding us.
                peer = self.pool.random()
                reason = "background probe"
                self._last_probe = now
            else:
                return False
            if peer is None:
                return False
            log.debug("[sync] %s, polling %s", reason, peer)

        # What the node is actually doing, while it is doing it. A sync can
        # run for many seconds and nothing used to say so: status_line kept
        # reading "computing VDF", which is what the VDF thread is doing but
        # not what the node is waiting on, so a node catching up and a node
        # stuck looked exactly the same from outside.
        resume_status = self.status_line
        self.status_line = f"checking {peer} for a better chain"
        adopted = self.syncer.check_and_sync(
            self.cs.chain,
            lambda chain: self.apply_better_chain(chain)[0],
            peer=peer,
            progress=self._note_sync_progress,
            info_timeout=SYNC_INFO_TIMEOUT_SECONDS,
            local_work=self.cs.cumulative_iterations,
            max_pages=SYNC_PAGES_PER_PASS,
            # Don't stay blocked longer than the network's own patience
            # with us: past our measured echo deadline, anyone who stemmed
            # an item to us has already given up and re-sent it, so time
            # spent beyond that is time spent being a hole in propagation.
            budget=self._echo_deadline_seconds(),
        )
        if not adopted:
            # Nothing came of it, so put back whatever the node was saying
            # before rather than leaving a finished check on screen.
            self.status_line = resume_status

        if hinted and not adopted:
            # The hint was a block we could not validate. We don't have
            # its parents, so acting on it is a bet, and this peer just
            # lost it. Without a cost here, one crafted datagram buys an
            # attacker a sync attempt, repeatable for as long as they care
            # to send them. A strike is the existing price for a peer that
            # wastes our time, and enough of them evict it (PeerPool).
            log.debug("[sync] hint from %s led nowhere", peer)
            self.pool.strike(peer)
        return adopted

    def _note_sync_progress(self, height, target):
        """Show how far a sync has got, as it gets there.

        Called once per applied page rather than per block, so it costs
        nothing and still moves: a page is up to FETCH_CHUNK blocks, and a
        node far behind fetches many of them.
        """
        behind = max(target - height, 0)
        self.status_line = (f"syncing: block {height:,} of {target:,} "
                            f"({behind:,} to go)")

    def _best_known_peer(self):
        """The peer we last saw claiming the highest chain, else any peer.

        The pool already records every peer's claimed height from each
        INFO exchange; picking with it instead of uniformly at random is
        free. It stays a claim, never a conclusion. It only decides who
        is worth one round trip.
        """
        best, best_height = None, -1
        for row in self.pool.snapshot():
            addr, _last_seen, active, height = row[0], row[1], row[2], row[3]
            if not active or height is None:
                continue
            if height > best_height:
                best, best_height = addr, height
        return best if best is not None else self.pool.random()

    def _handle_inbound_block(self, msg, block_out):
        """Single choke point for every inbound block: decide what it proves,
        propagate it if we can vouch for it, and note who to sync from if it
        shows we're behind.

        Three cases, by what we can actually establish:

        - Extends our tip. Fully validatable right here, so validate and
          propagate (gossip.relay_block), and hand it to the cycle, which
          re-checks it against the tip it actually raced for.

        - Further ahead than our tip+1. We cannot validate it. We don't
          have its parents, so it is evidence, not proof, and we must not
          relay it: passing on something we can't vouch for would let anyone
          spend the whole network's bandwidth for one crafted datagram.
          What it does do is tell us who to ask. Being lied to costs exactly
          one sync attempt against that peer, which then fails validation on
          its own merits, so believing the hint is never what decides truth.

        - A sibling of our tip. Fully validatable too, its parent is the
          block below our own tip, so it is news rather than old news: it
          may be the one that wins the height's draw. Taken if it does,
          and passed on in that case only.

        - Anything else at or below our tip. Nothing to learn, nothing to
          pass on.
        """
        blk      = msg["block"]
        sender   = msg.get("sender")
        stemming = msg.get("stemming", False)
        block_out.append(blk)

        cs     = self.cs
        height = blk.get("height")
        if not isinstance(height, int):
            return

        if height == cs.height + 1 and blk.get("previous_hash") == cs.tip["hash"]:
            # Through the cache, so a block replayed under a fresh transport
            # msg_id costs a dict lookup rather than another VDF proof check
            # and another pass over every signature it carries. Nothing at
            # this layer deduplicates by item hash before here (the
            # transport's msg_id dedup does not, since re-sending mints a
            # new one), so without it the cost of replaying a block at this
            # node was bounded only by the rate limiter.
            if not self._validate_candidate(blk, cs):
                log.debug("[block] inbound rejected at height %d", height)
                return
            # Only now: a block that hasn't validated proves nothing about
            # the network still producing blocks, and treating it as proof
            # would let one datagram hold the silence trigger open forever
            #, which is exactly what an eclipsing peer would want.
            self._last_block_seen = time.monotonic()
            self._note_echo(blk["hash"], sender)
            self.gossip.relay(blk, gossip_mod.KIND_BLOCK, blk["hash"],
                              sender, stemming=stemming)
        elif height == cs.height:
            if blk.get("hash") == cs.tip["hash"]:
                # Our own tip, come back to us. Relayed, and this is not the
                # no-op it looks like: the branch above only fires while a
                # block is still ahead of us, so once we commit a height we
                # stop passing that block on. For a block we built ourselves
                # that is fatal to its spread. The builder stems its
                # candidate to a single peer and commits it moments later,
                # so by the time the walk fluffs and comes back, the one
                # node that produced the block is the one node that will
                # never put it on the public wire, and every peer reachable
                # only through the builder never hears of it. That is the
                # same defect the transaction path had at its originator,
                # and blocks run on this identical mechanism (see
                # gossip.py): fixing it there and not here fixed half of it.
                # gossip._fluff floods an item once per hash, so passing
                # everything through cannot loop.
                self.gossip.relay(blk, gossip_mod.KIND_BLOCK, blk["hash"],
                                  sender, stemming=stemming)
            # Otherwise a sibling of our own tip. Not late, not lost: if it
            # is the better chain we take it right now, locally, no round
            # trip. Passing on the ones that win, since a node that already
            # committed this height does not relay through the branch
            # above, so without this a winning sibling only ever reaches
            # whoever the builder reached directly. Bounded by the same
            # check that let it win, only a strictly lower output gets this
            # far, so at most a handful per height however many arrive.
            elif self._reorg_to_sibling(blk, cs):
                self.gossip.relay(blk, gossip_mod.KIND_BLOCK, blk["hash"],
                                  sender, stemming=stemming)
        elif height > cs.height and sender:
            log.info("[sync] %s is on block %d and we are on %d, catching up",
                     sender, height, cs.height)
            # Keep the strongest claim rather than the latest one: a single
            # slot on last-writer-wins would let anyone displace a real hint
            # just by sending a stream of weaker ones.
            if height > self._sync_hint_height:
                self._sync_hint = sender
                self._sync_hint_height = height

    def _handle_inbound_tx(self, msg):
        """Route an inbound tx: validate, admit to the mempool, propagate.

        A tx still in the private phase is forwarded without being admitted
        here, we're a relay for it, not its destination, but it is still
        validated first. Relaying something unvalidated would let anyone
        spend our bandwidth (and every downstream peer's) for the price of
        one crafted datagram.
        """
        tx_dict  = msg["tx"]
        origin   = tx_dict.get("from", "?")[:24]
        sender   = msg.get("sender")
        stemming = msg.get("stemming", False)

        tx_hash = tx_mod.tx_hash(tx_dict)

        ok, err = self._validate_for_mempool(tx_dict)
        if not ok:
            log.debug("[tx] inbound rejected  reason=%s  from=%s", err, origin)
            return
        self._note_echo(tx_hash, sender)

        if stemming:
            log.debug("[tx] stem relay  from=%s", origin)
            went_public = self.gossip.relay(tx_dict, gossip_mod.KIND_TX,
                                            tx_hash, sender, stemming=True)
            if not went_public:
                # Still private, and still not ours: we are a relay for it,
                # not its destination. Keeping it out of the mempool is what
                # stops /api/mempool answering "which nodes are on the
                # private path", which is the thing the stem exists to hide.
                return
            # The walk ended here and we broadcast it to everyone, so it is
            # public now and there is nothing left to hide by not keeping
            # it. Not keeping it meant the one node that put a transaction
            # in front of the whole network was the one node that could not
            # then mine it, which cost that transaction a builder and cost
            # us the fee, silently, on every walk that ended at us.
            added, h_or_err = self.mempool.add(tx_dict)
            if added:
                log.debug("[tx] fluffed here, keeping it  hash=%s  from=%s",
                          h_or_err[:12], origin)
            return

        added, h_or_err = self.mempool.add(tx_dict)
        if added:
            log.debug("[tx] inbound accepted  hash=%s  from=%s", h_or_err[:12], origin)
        else:
            log.debug("[tx] inbound duplicate  from=%s", origin)
        # Relayed either way. Whether our mempool already held this and
        # whether we have already flooded it are different questions, and
        # gossip._fluff answers the second itself, once per item hash, so
        # passing everything through here cannot loop. Gating on the first
        # is what broke the flood at its most important node: the
        # originator has the transaction in its mempool from the moment it
        # signed it and has flooded nothing, so a public copy arriving back
        # found "already have it" and stopped there, stranding every peer
        # reachable only through it.
        self.gossip.relay(tx_dict, gossip_mod.KIND_TX, tx_hash,
                          sender, stemming=False)

    # ------------------------------------------------------------------
    # Chain sync / reorg
    # ------------------------------------------------------------------

    def _remember_state(self, cs):
        """Cache (state, cumulative_iterations) at cs.height, so a later
        shallow reorg back to this point can resume without a full replay.
        Bounded to RECENT_STATE_CACHE_SIZE; see that constant for why."""
        self._recent_states[cs.height] = (cs.state.snapshot(), cs.cumulative_iterations)
        self._recent_states.move_to_end(cs.height)
        while len(self._recent_states) > RECENT_STATE_CACHE_SIZE:
            self._recent_states.popitem(last=False)

    def _forget_states_from(self, height):
        """Purge cached entries at or above `height`: a reorg just replaced
        whatever blocks used to be there, so a *later* reorg landing on one
        of those heights must not reuse state that belonged to the now-
        abandoned branch."""
        for h in [h for h in self._recent_states if h >= height]:
            del self._recent_states[h]

    def _resume_point(self, fork_point, remote_chain):
        """A ChainState to resume validation from at fork_point, avoiding a
        full genesis replay, when one is cheaply available:
          - a pure extension (fork_point == len(self.cs.chain)): self.cs
            itself, already fully built and sitting in memory.
          - a shallow reorg: a cached recent state (see _remember_state).
        Returns None if neither applies (a reorg deeper than the cache, or
        shortly after a restart before the cache has refilled), callers
        fall back to a full replay in that case, which is also the right
        conservative behavior for what would be a genuinely abnormal, deep
        divergence (see RECENT_STATE_CACHE_SIZE).
        """
        if fork_point == len(self.cs.chain):
            return self.cs
        resume_height = fork_point - 1
        if resume_height == 0:
            # State right after genesis is always trivially known (empty
            # balances, zero iterations, genesis carries no transactions
            # and contributes nothing to cumulative_iterations), no need
            # to consult the cache for this one.
            return ChainState(remote_chain[:fork_point], state_mod.State(), 0)
        cached = self._recent_states.get(resume_height)
        if cached is None:
            return None
        base_state, base_iterations = cached
        return ChainState(remote_chain[:fork_point], base_state.snapshot(), base_iterations)

    def _evaluate_remote_chain(self, remote_chain):
        """Pure evaluation of a candidate remote chain. No committed state
        is mutated (the _recent_states cache is only ever read here).

        Returns (ok, err, fork_point, tail, remote_cs) on success,
        or (False, err, None, None, None) on rejection.

        Order of operations matters for security:
          1. Genesis check  cheap, stops wrong-network chains immediately.
          2. Fork point     O(min(local, remote)) hash comparisons.
          3. Build remote_cs: resume from a cached/in-memory state when
             possible, otherwise a full replay from genesis.
          4. is_better_than fork choice, only after we know it's valid.
        """
        if not remote_chain or remote_chain[0]["hash"] != self.cs.genesis_hash:
            log.warning("[sync] rejected genesis mismatch  remote=%s  expected=%s",
                        (remote_chain[0]["hash"][:12] if remote_chain else "empty"),
                        self.cs.genesis_hash[:12])
            return False, "genesis mismatch", None, None, None

        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.cs.chain, remote_chain))
             if a["hash"] != b["hash"]),
            min(len(self.cs.chain), len(remote_chain))
        )
        tail = remote_chain[fork_point:]

        base_cs = self._resume_point(fork_point, remote_chain)
        if base_cs is not None:
            # Extension or shallow reorg: validate and apply just the new
            # tail onto an already-known state, instead of re-deriving the
            # whole chain from scratch, ChainState.from_chain (the branch
            # below) replays every block since genesis on every call, so
            # without this a node syncing far behind in many small pages
            # (or hitting a routine reorg) would redo that full replay each
            # time, O(chain length) work per attempt instead of O(new work).
            # Wrapped like the fallback branch below: tail is untrusted peer
            # data, and validation can raise on a malformed block (e.g. one
            # missing a required field) rather than cleanly returning False.
            try:
                cs = base_cs
                for blk in tail:
                    ok, err, cs = cs.validate_and_apply(blk)
                    if not ok:
                        log.warning("[sync] rejected: invalid block at %s: %s",
                                   blk.get("height"), err)
                        return False, f"invalid block at {blk.get('height')}: {err}", None, None, None
                remote_cs = cs
            except Exception as e:
                log.warning("[sync] validation failed: %s", e)
                return False, f"validation error: {e}", None, None, None
        else:
            # Deeper than the cache, or the cache hasn't filled yet (e.g.
            # shortly after a restart): fall back to a full replay. Also
            # the right conservative behavior for what would be a
            # genuinely abnormal, deep divergence. _validate_tail already
            # builds the resulting ChainState as it validates, reuse it
            # directly rather than replaying the same chain a second time
            # via a separate ChainState.from_chain(remote_chain) call.
            try:
                ok, err, remote_cs = _validate_tail(tail, remote_chain[:fork_point])
            except Exception as e:
                log.warning("[sync] chain replay failed: %s", e)
                return False, f"chain replay error: {e}", None, None, None
            if not ok:
                log.warning("[sync] rejected: %s", err)
                return False, err, None, None, None

        if not remote_cs.is_better_than(self.cs):
            log.debug("[sync] remote chain not better  remote_h=%d  local_h=%d",
                      remote_cs.height, self.cs.height)
            return False, "remote chain not better", fork_point, tail, None

        return True, None, fork_point, tail, remote_cs

    def _readd_valid_txs(self, txs, exclude_hashes, state):
        """Re-add unconfirmed txs to the mempool, each validated against
        state first, a stale-nonce or otherwise now-invalid tx must not
        be silently re-admitted. Also re-floods each one: it fell out of a
        reorged-away block, so other nodes on the now-canonical chain may
        never have seen it at all, and without this it would just sit in
        our own mempool alone until we mine it ourselves or it expires."""
        for t in txs:
            if tx_mod.tx_hash(t) in exclude_hashes:
                continue
            ok, _ = tx_mod.validate(t, state)
            if ok:
                added, _ = self.mempool.add(t)
                if added:
                    self._spread(t, gossip_mod.KIND_TX, tx_mod.tx_hash(t))

    def _salvage_fork_txs(self, fork_point, tail):
        """Re-add unconfirmed, still-valid txs from a rejected fork into the
        local mempool. self.cs is unchanged here (the remote chain lost),
        so txs are checked against the current local state."""
        confirmed = {tx_mod.tx_hash(t)
                     for blk in self.cs.chain[fork_point:]
                     for t in blk.get("transactions", [])}
        txs = (t for blk in tail for t in blk.get("transactions", []))
        self._readd_valid_txs(txs, confirmed, self.cs.state)

    def apply_better_chain(self, remote_chain):
        """Accept remote_chain if it is better than local. Used by syncer."""
        ok, err, fork_point, tail, remote_cs = self._evaluate_remote_chain(remote_chain)
        if not ok:
            if fork_point is not None and tail:
                self._salvage_fork_txs(fork_point, tail)
            return False, err

        # Storage write first; if it fails, mempool and self.cs stay untouched
        # and consistent with each other.
        self.storage.replace_chain_and_state(fork_point, tail, remote_cs.state)
        old_chain = self.cs.chain  # abandoned branch; _reorg_mempool needs
                                    # this, not the new chain we're about to swap in
        # self.cs/self.view must track storage the instant it succeeds. A
        # failure past this point (e.g. a malformed re-added tx) must not
        # leave storage on the new chain while self.cs, what mining and
        # validation actually run against, still points at the old one.
        # Stale cache entries at or above fork_point belonged to the branch
        # just abandoned; a later reorg landing on one of those heights
        # must not reuse state from blocks that are no longer canonical.
        self._forget_states_from(fork_point)
        self.cs = remote_cs
        self._remember_state(self.cs)
        self.view = NodeView(self.cs)
        try:
            self._reorg_mempool(fork_point, old_chain, remote_chain, remote_cs.state)
        except Exception:
            log.exception("[reorg] mempool re-add failed; chain state already "
                          "committed, mempool may hold stale entries until pruned")

        # How much of our own chain was thrown away, which is not the same
        # as how much we took on: a heavier fork can be shorter.
        discarded = len(old_chain) - fork_point
        if discarded:
            log.warning("[sync] replaced our last %d block(s): now on block %d",
                        discarded, self.cs.height)
            self._record_reorg(discarded)
        else:
            log.info("[sync] now on block %d", self.cs.height)
        return True, None

    def _record_reorg(self, discarded):
        """Keep the deepest reorg this node has seen, and how many.

        Only reorgs that replaced more than one block. A one-block swap is
        the draw settling a tie (_reorg_to_sibling), which is routine and
        constant by design, and counting it would bury the rare event this
        exists to surface under noise from the common one. Filtering on
        depth rather than on which caller it came from means there is one
        place doing the recording, and a one-block swap from any other
        source is judged the same way.

        Kept in storage rather than memory. A deep reorg may happen once in
        a node's life, and a counter that forgets it on restart is not
        worth reading.
        """
        if discarded < REORG_NOTABLE_DEPTH:
            return
        count = int(self.storage.get_meta("reorg_count", 0) or 0) + 1
        previous_deepest = int(self.storage.get_meta("reorg_deepest", 0) or 0)
        deepest = max(previous_deepest, discarded)
        self.storage.set_meta("reorg_count", count)
        self.storage.set_meta("reorg_deepest", deepest)
        # Strictly deeper, compared against the previous record rather than
        # against the one just updated to include this reorg. Against the
        # latter every reorg that merely equals the standing record passed,
        # and rewrote the date to say the record was set today when it was
        # not.
        if discarded > previous_deepest:
            self.storage.set_meta("reorg_deepest_at", int(time.time()))
        log.warning("[sync] that was a deep reorg: %d blocks replaced "
                    "(deepest this node has seen: %d, %d in total)",
                    discarded, deepest, count)

    def reorg_stats(self):
        """Deepest reorg seen and how many, for display."""
        return {
            "deepest": int(self.storage.get_meta("reorg_deepest", 0) or 0),
            "count":   int(self.storage.get_meta("reorg_count", 0) or 0),
            "deepest_at": int(self.storage.get_meta("reorg_deepest_at", 0) or 0),
        }

    def _reorg_mempool(self, fork_point, old_chain, new_chain, new_state):
        """Re-add unconfirmed txs from the abandoned local branch, validated
        against the new chain's state (not the old chain being replaced.
        A tx that's no longer valid under the new chain must not be
        silently re-admitted, or it can stall this node's own block
        production every cycle until it's pruned)."""
        old_txs = [t for blk in old_chain[fork_point:]
                   for t in blk.get("transactions", [])]
        new_confirmed = {tx_mod.tx_hash(t)
                         for blk in new_chain[fork_point:]
                         for t in blk.get("transactions", [])}
        self.mempool.remove_many(new_confirmed)
        self._readd_valid_txs(old_txs, new_confirmed, new_state)
