"""Periodic chain sync over UDP.

Uses UDPTransport.get_info() for lightweight tip comparison and
UDPTransport.request_sync() for fetching chain segments.

Fork choice: most cumulative proven VDF work wins; tip hash breaks ties.
See ChainState.is_better_than().
"""

import logging
import time

from peer_udp import MAX_SYNC_BLOCKS, SYNC_FETCH_TIMEOUT, FORK_PROBE_TIMEOUT

log = logging.getLogger("ec.syncer")

FETCH_CHUNK = 50    # starting blocks per GETSYNC request, then adaptive
# peer_udp.py now has real chunk-level ACK/retransmit for multi-chunk UDP
# messages, so a single dropped datagram no longer silently fails an entire
# page, the old rationale for keeping this very small (5) no longer
# applies. 50 is a modest starting point for real sync protocols; the
# window below is what actually sizes the request as the sync runs.
#
# It only starts here, and only for a peer this node has never synced
# from before: _fetch_and_apply adjusts it per page (AIMD, the same shape
# TCP uses for its congestion window) and Syncer._peer_chunk carries the
# result forward across calls, keyed per peer. A page that lands clean on
# the first try grows the next one; a page that needed a retry means
# something on this path is already struggling (loss, peer load, or a
# response too big to reassemble, all look the same from here: no
# answer), so the next one shrinks instead of throwing a second, larger
# request at the same problem. It has to survive past one call: node.py
# only ever asks for a few pages at a time (SYNC_PAGES_PER_PASS), so a
# window that reset to this starting value every call would win back the
# same first growth step and lose it again every pass, forever, and
# never actually reach whatever a good connection could carry.
MIN_FETCH_CHUNK = 10
# The server (_serve_sync in peer_udp.py) already clamps every response to
# MAX_SYNC_BLOCKS blocks regardless of what's asked for, so growing past
# it buys nothing but a bigger, unnecessary request field: the page comes
# back capped either way, and _fetch_and_apply advances by however many
# blocks actually arrive rather than by how many were asked for, so this
# is purely an efficiency ceiling, not a correctness one. A response too
# big to reassemble (oversized real blocks) comes back as no answer, same
# as any other failed attempt, and the backoff below reacts to that the
# same way it reacts to loss.
MAX_FETCH_CHUNK = MAX_SYNC_BLOCKS
FETCH_CHUNK_GROWTH = 1.3   # multiplicative increase after a clean, fast page
FETCH_CHUNK_BACKOFF = 0.5  # multiplicative decrease after a retried page
FETCH_CHUNK_SOFT_BACKOFF = 0.8  # decrease after a clean page that ran slow

# Ceiling on how much validated-but-not-yet-decided tail (Syncer._peer_progress)
# a single peer may keep us holding in memory across calls while we wait to
# find out whether their claimed chain ever becomes better than ours. Without
# this, a peer whose real chain keeps growing (honestly or not -- either way
# every block in it still has to be a genuine, cryptographically proven
# extension to get this far) could keep us accumulating an ever-larger
# unapplied tail indefinitely. Set well above any gap this codebase expects to
# hit in ordinary operation (FORK_SEARCH_WINDOW-scale reorgs, or a node offline
# for a while) so it never fires in practice; a peer that manages to exceed it
# anyway just falls back to the pre-resume behavior (re-fetch from fork_from
# next time) instead of growing further, trading a known inefficiency for a
# bounded memory cost.
MAX_PENDING_TAIL_BLOCKS = 200_000

# SYNC_FETCH_TIMEOUT (imported): per-page request timeout, derived in
# peer_udp.py from real numbers (see its own comment there) rather than
# picked here, since every ingredient it's built from (the transport's
# real send pacing, the sync page's real byte budget) lives in that file
# already; re-deriving it here would just be a second copy to keep in
# sync with the first.
#
# needed_retry (a request that got no answer at all inside this timeout)
# is a hard signal: something failed outright, so the response below is a
# hard halving. But by the time that happens the window already overshot
# whatever the path could actually carry, found out by failing, and paid
# a full timeout to learn it. A page that came back cleanly is not
# necessarily far from that same edge: one that used most of its timeout
# budget to arrive succeeded this time only because nothing else went
# wrong, not because there is headroom left.
#
# So elapsed time on a clean page is read the same way TCP Vegas/BBR read
# rising RTT: as an early warning, checked before a request ever fails
# rather than only after.
#
# Against each peer's own recent baseline (Syncer._peer_latency), not
# against a fraction of SYNC_FETCH_TIMEOUT: that timeout is necessarily
# sized for the worst case (a full SYNC_PAGE_BYTE_BUDGET page, see
# peer_udp.py), tens of seconds, while a real page is usually a small
# fraction of that. Fractions of the timeout were checked against that
# real gap and found to almost never fire outside an actual failure: at
# FETCH_LATENCY_GROW_BELOW=0.4 of a 120s timeout, anything under 48
# seconds grew the window regardless of how it compared to what this
# same peer had been delivering, so the early warning was, in practice,
# just the hard failure path wearing a proactive name. A peer's own
# baseline is the number this was always supposed to be read against.
#
# FETCH_LATENCY_GROW_BELOW/HOLD_BELOW are now ratios to that baseline: at
# comfortably close to it, the window grows as before. Meaningfully
# above it but still not close to an outright failure, it holds steady,
# neither pushing its luck nor giving back ground it hasn't been shown
# to need to. Well above it, it eases back on purpose, before a timeout
# forces the same outcome the hard way. This is what keeps the window
# from sawing all the way up to a failure and back on every cycle: it
# feels this specific path slowing down (bigger blocks, a loaded peer, a
# congested link) relative to what it normally does, and responds while
# still succeeding, rather than only ever discovering the ceiling by
# falling through it. A peer with no baseline yet (its first page ever)
# falls back to a fraction of SYNC_FETCH_TIMEOUT, the only reasonable
# thing to compare against before there is a "normal" for this path.
FETCH_LATENCY_GROW_BELOW = 1.5
FETCH_LATENCY_HOLD_BELOW = 3.0
FETCH_LATENCY_BOOTSTRAP_GROW_BELOW = 0.4
FETCH_LATENCY_BOOTSTRAP_HOLD_BELOW = 0.75
# EMA weight for folding a fresh sample into the baseline. Low, on
# purpose: the baseline is what "normal for this path" means, and a
# single unusually fast or slow page (one already-tiny page, one that
# raced a burst of other traffic) shouldn't redefine that on its own.
# Real, sustained change (blocks genuinely got bigger, the path is
# genuinely more congested now) still shows up, just over several
# samples rather than one.
FETCH_LATENCY_EMA_WEIGHT = 0.2
# Floor under the baseline itself, not just under chunk size: a peer on
# an exceptionally fast path (same host, loopback-class latency in
# tests) could otherwise settle on a baseline near zero, where even
# ordinary jitter is a large ratio of it and the classification above
# gets noisy for no real reason. 20ms is comfortably above realistic
# same-host round trips and comfortably below any real network's.
FETCH_LATENCY_BASELINE_FLOOR = 0.02

# Extra attempts before treating an outright timeout/decode-failure (resp is
# None) as authoritative. The UDP transport has no chunk-level retransmission
# (see peer_udp.py), so a single dropped datagram during the binary-search
# fork-point probe previously looked identical to "peer's chain doesn't
# reach this height", and one during a fetch page looked identical to "fetch
# failed", either way narrowing the search or aborting the sync on nothing
# more than packet loss. This does not apply to a real response with an
# empty/missing chain field, which is a legitimate answer, not a timeout.
SYNC_REQUEST_RETRIES = 2

# How far back _find_fork_point looks before widening to the whole chain.
# Matched to node.RECENT_STATE_CACHE_SIZE, which already defines where this
# codebase treats a reorg as abnormal rather than routine: past that depth
# a node replays from genesis anyway, so a wider search there costs nothing
# it wasn't already going to pay. Not a new tuning knob, the same boundary
# read from the other side.
FORK_SEARCH_WINDOW = 20


class _Unanswered(Exception):
    """A peer stopped answering during the fork-point search. Distinct from
    any answer it could have given, including an empty one."""


def _expired(deadline):
    return deadline is not None and time.monotonic() >= deadline


class Syncer:

    def __init__(self, pool, udp):
        self.pool = pool
        self.udp  = udp
        # Per-peer AIMD window, surviving across check_and_sync calls. It
        # used to start over at FETCH_CHUNK every call, which looked fine
        # in isolation but was never actually given room to compound: a
        # real catch-up is many short calls (node.py caps each one at
        # SYNC_PAGES_PER_PASS pages, so as not to block the block-building
        # loop that calls it), not one long one, so the window kept
        # winning back the same first growth step and losing it again
        # every ~10 seconds, forever, regardless of how good the
        # connection was. A real peer's path characteristics don't reset
        # between one pass and the next, so neither should this.
        self._peer_chunk = {}
        # Per-peer EMA baseline round-trip time for a clean page, seconds.
        # See FETCH_LATENCY_GROW_BELOW for why this exists: without it the
        # only real reference point was SYNC_FETCH_TIMEOUT, sized for a
        # worst-case page and too large to tell an ordinary page from a
        # genuinely slowing one. Persisted alongside _peer_chunk for the
        # same reason (survives across the short passes a real sync is
        # actually made of), and cleared together with it.
        self._peer_latency = {}
        # Whether the most recent check_and_sync call reached a real
        # verdict on the peer it asked (adopted their chain, hit a genuinely
        # invalid/unresponsive/malformed answer, or fully compared a claimed
        # chain and found it not better) versus merely pausing mid-fetch
        # (max_pages/budget) with everything seen so far still valid. Callers
        # that penalize a peer for "asked and got nothing" (node.py strikes a
        # hint that led nowhere) need this distinction: a peer with a
        # genuinely longer chain that simply doesn't fit in one pass must not
        # be punished the same as one that lied or went silent, or an honest
        # peer could be banned before its own chain ever finishes arriving.
        # See _fetch_and_apply and _sync_page_outcome (node.py) for the full
        # story. Defaults to True (conclusive): every early-return path in
        # check_and_sync below is already a definitive answer; only a
        # paused, in-progress fetch ever sets this False.
        self.last_attempt_conclusive = True
        # Per-peer resumable fetch: a validated-but-not-yet-decided tail
        # ("not better *yet*", see _fetch_and_apply) that a pass ran out of
        # max_pages/budget before resolving. Without this, every subsequent
        # call re-fetched the exact same blocks from fork_from, over and
        # over, and threw them away again at the same pause point, making
        # real progress depend entirely on the page size (_peer_chunk)
        # eventually growing past the *entire* remaining gap in one page --
        # which never happens for a gap larger than max_pages times however
        # big a page can get. Persisting the tail itself means every call
        # adds new blocks on top of what the last one already fetched,
        # converging in a bounded number of calls for a gap of any size,
        # not just ones small enough for the size-guessing game to win.
        #
        # Keyed by peer, value is {"fork_from", "anchor_hash", "tail", "h"}.
        # anchor_hash pins the shared-history block this tail was built on
        # top of (local_chain[fork_from-1]'s hash, or None at genesis) so a
        # local reorg or a different fork point found on a later probe can
        # never cause a stale tail to be resumed onto the wrong prefix: see
        # _fetch_and_apply's use of it.
        self._peer_progress = {}

    def check_and_sync(self, local_chain, apply_fn, peer=None, info_timeout=8.0,
                       local_work=None, max_pages=None, budget=None,
                       progress=None):
        """Sync from `peer` (default: a random one) if they have a better chain.

        Compares by cumulative proven VDF work (tip hash breaks ties).
        Returns True if the chain was updated.

        peer: who to ask. Callers that already know who is ahead (node.py
        learns it from the block that proved it) pass that address, instead
        of paying for a random draw that probably picks someone who isn't.

        info_timeout: how long to wait for the initial GETINFO probe.

        local_work: our own cumulative proven iterations, used for the
        cheap first-round-trip bail below. Omit it and no bail happens,
        correct, just not free.

        max_pages: stop after this many fetched pages and return, leaving
        the rest for the caller's next pass. A long sync that ran inline to
        completion blocked its caller for minutes, and a node that is not
        draining is a node that forwards nothing, which under a stem
        silently kills whatever hop was handed to it.

        progress: optional progress(height, target) called as each page
        lands. Sync can take many seconds and nothing above this could
        previously tell it was happening at all, so a catching-up node
        looked identical to a stalled one. Purely for display; nothing
        here depends on the caller doing anything with it.

        budget: seconds this whole pass may take. max_pages bounds the work
        we choose to do; this bounds the work a peer can make us wait for.
        Every request here can time out and retry, so an unresponsive peer
        could otherwise hold the caller for several minutes per pass, and
        claiming a high tip costs an attacker nothing. Whatever is left
        undone is picked up next pass, from whoever answers.
        """
        self.last_attempt_conclusive = True
        if peer is None:
            peer = self.pool.random()
        if not peer:
            log.debug("[sync] no peers available")
            return False

        info = self.udp.get_info(peer, timeout=info_timeout)
        if info is None:
            log.debug("[sync] info request failed  peer=%s", peer)
            return False

        if not isinstance(info, dict) or "height" not in info:
            log.debug("[sync] unexpected info response  peer=%s", peer)
            return False

        # Cache for display (e.g. the peers page), regardless of whether a
        # sync ends up happening below.
        self.pool.update_info(peer, height=info.get("height"),
                              version=info.get("version", ""))

        remote_height = info["height"]
        local_height  = len(local_chain) - 1
        local_tip     = local_chain[-1]["hash"] if local_chain else ""
        if local_work is None:
            local_work = -1   # unknown: never bail, always compare properly

        # Stop at the first round trip whenever the peer doesn't even claim
        # more proven work than we already have. Without this, a peer that
        # is level or behind still cost a full O(log chain) binary-search
        # fork probe plus a fetch, all to end at "remote chain not better".
        #
        # Compared on cumulative iterations, never on height. Fork choice
        # does not use height (ChainState.is_better_than) precisely because
        # forks retarget from their own timestamps, so a chain can be
        # *shorter* and still carry strictly more work, and a padded-
        # timestamp fork with a low iteration requirement is exactly the
        # attack that rule exists to defeat. Bailing on height would have
        # declined to even look at the chain that beats it.
        #
        # A peer too old to report work leaves this unknown, and unknown is
        # not "nothing": fall through and let validation decide, the way it
        # did before this shortcut existed.
        remote_work = info.get("work")
        if isinstance(remote_work, int) and remote_work <= local_work:
            log.debug("[sync] peer=%s claims work=%d, not above local=%d",
                      peer, remote_work, local_work)
            return False
        if remote_height == local_height and info.get("tip_hash", "") == local_tip:
            log.debug("[sync] already in sync  peer=%s  height=%d", peer, local_height)
            return False

        log.debug("[sync] comparing  peer=%s  remote=%d  local=%d",
                  peer, remote_height, local_height)

        deadline = None if budget is None else time.monotonic() + budget
        fork_from = self._find_fork_point(peer, local_chain, deadline)
        if fork_from is None:
            log.warning("[sync] fork point search failed  peer=%s", peer)
            return False

        log.info("[sync] fetching blocks %d to %d from %s",
                 fork_from, remote_height, peer)

        applied_any, conclusive = self._fetch_and_apply(
            peer, local_chain, fork_from, remote_height,
            apply_fn, max_pages, deadline, progress)
        self.last_attempt_conclusive = conclusive
        return applied_any

    def _fetch_and_apply(self, peer, local_chain, fork_from, remote_height, apply_fn,
                         max_pages=None, deadline=None, progress=None):
        """Fetch in adaptively-sized pages (see FETCH_CHUNK and the AIMD
        window below), applying each page as it arrives instead of
        buffering the whole tail and applying it once at the end.

        Two reasons: a node many blocks behind would otherwise sit with an
        unchanged height for the entire fetch, however long that takes,
        then jump straight to the final height in one atomic step, nothing
        about the transfer is actually all-or-nothing, only its visibility
        was. And a peer that drops mid-fetch now leaves behind whatever
        pages already landed instead of only the single already-existing
        partial-tail fallback below covering that case.

        Applying per page is only cheap because node.py's
        _evaluate_remote_chain has a fast path for the common case (this
        page's chain is a pure extension of the current tip): it builds on
        the already-in-memory ChainState instead of replaying the whole
        chain from genesis on every page.

        apply_fn returns one of three things, not just a bool, and the
        difference matters: True (this page's chain is valid and now
        carries more proven work than we have -- applied), False (the chain
        is invalid outright -- wrong genesis, a block that fails validation,
        a replay error -- and fetching more of it is pointless, so this
        stops the loop immediately), or None (valid so far, but doesn't yet
        carry more proven work than we have). None must not be treated like
        False: an honest peer with a genuinely longer chain can still fail
        that comparison on an early, partial fetch of it, purely because
        proven work is counted only from what has actually arrived, and
        that peer's real lead does not always fit in one page. Stopping
        there used to make an honestly-longer chain permanently
        unreachable: every retry re-fetched the same early slice, compared
        it against the same fully-caught-up local chain, lost the same way,
        and gave up the same way, forever. None instead keeps the loop
        going -- fetch the next page of the *same* claimed chain and try
        the comparison again with more of it in hand -- right up to
        remote_height, the one point where "still not better" is actually
        a final answer rather than an artifact of not having looked far
        enough yet.

        Returns (applied_any, conclusive). applied_any is True if at least
        one page was applied. conclusive is False only when the loop
        stopped by pausing (max_pages/budget) while every page seen so far
        was still valid (None or True) -- i.e. nothing here says the peer
        did anything wrong, there just wasn't time/allowance to see enough
        of their chain to decide. It is True for every other ending
        (applied, hit remote_height and still not better, or stopped on an
        actually bad/unresponsive/malformed page): all of those are real
        verdicts on the peer's answer, which is what a caller (node.py's
        strike-on-a-bad-hint logic) needs to tell apart from "ask again
        later, nothing wrong so far".

        A pause (conclusive=False) does not throw away what was fetched: see
        Syncer._peer_progress. The next call for the same peer resumes from
        where this one paused instead of re-fetching fork_from all over
        again, so real progress is bounded by (gap / pages per call), not by
        hoping a single page eventually grows to cover the whole gap.
        """
        applied_any = False
        conclusive = True
        pages = 0
        chunk = self._peer_chunk.get(peer, FETCH_CHUNK)
        # Sliced once, not on every page: fork_from never changes inside
        # this loop, so re-slicing local_chain[:fork_from] each time was
        # paying for the same prefix copy again and again. Immaterial for
        # a short catch-up, real for the one caller with no max_pages at
        # all (main.py's startup sync), where a long historical chain
        # means a large, unchanging prefix re-copied on every one of
        # however many pages a full bootstrap takes.
        prefix = local_chain[:fork_from]

        # Resume a still-pending tail left by an earlier, paused call for
        # this same peer, provided it was built on exactly the shared
        # history we still have: same fork point, and the block right
        # before it hasn't changed underneath us (a local reorg since then
        # would make the cached tail's prefix stale). Anything else --
        # different peer, different fork point, local history moved -- and
        # this starts clean, same as before resuming existed; resuming is
        # an efficiency gain only, never something correctness depends on.
        anchor_hash = local_chain[fork_from - 1]["hash"] if fork_from > 0 else None
        cached = self._peer_progress.get(peer)
        if (cached is not None
                and cached["fork_from"] == fork_from
                and cached["anchor_hash"] == anchor_hash):
            tail_so_far = cached["tail"]
            h = cached["h"]
        else:
            tail_so_far = []
            h = fork_from

        while h <= remote_height:
            if _expired(deadline):
                log.debug("[sync] out of budget after %d pages  peer=%s", pages, peer)
                conclusive = False
                break
            if max_pages is not None and pages >= max_pages:
                log.debug("[sync] pausing after %d pages, resuming next pass", pages)
                conclusive = False
                break
            pages += 1
            to_h = min(h + chunk - 1, remote_height)
            t0 = time.monotonic()
            resp, needed_retry = self._request_sync_with_retry(
                peer, from_h=h, to_h=to_h, timeout=SYNC_FETCH_TIMEOUT, deadline=deadline)
            elapsed = time.monotonic() - t0
            if resp is None:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                # A harder failure than needed_retry (every attempt in
                # _request_sync_with_retry came back empty, not just the
                # first one, and each at up to SYNC_FETCH_TIMEOUT), so it
                # gets a harder response: straight to MIN_FETCH_CHUNK
                # rather than the same halving needed_retry gets below.
                # Written back before returning, not left in the local
                # variable: otherwise the next pass, a fresh call a few
                # seconds later, would retry this same peer at the same
                # too-large size and fail the same way again, possibly
                # indefinitely.
                self._peer_chunk[peer] = MIN_FETCH_CHUNK
                self._peer_progress.pop(peer, None)
                break
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                self._peer_progress.pop(peer, None)
                break
            # Checked before this page is trusted enough to even build
            # full_chain from, not left entirely to apply_fn to catch
            # downstream: a peer sending back a page that doesn't start
            # where it was asked to, or that runs past to_h, would
            # otherwise still get concatenated and validated as if it
            # might be legitimate, and h += len(page) below would trust
            # its length regardless of whether validation happened to
            # catch the mismatch some other way. apply_fn still does the
            # real work (hash-chain, VDF, signatures); this only refuses
            # to hand it something already known not to match what was
            # requested.
            if page[0].get("height") != h or len(page) > to_h - h + 1:
                log.warning("[sync] page doesn't match request  peer=%s  "
                           "from_h=%d  to_h=%d  got_height=%s  got_len=%d",
                           peer, h, to_h, page[0].get("height"), len(page))
                self._peer_progress.pop(peer, None)
                break

            tail_so_far += page
            full_chain = prefix + tail_so_far
            result = apply_fn(full_chain)
            if result is False:
                # A real verdict, not a "not yet": the chain itself is bad,
                # so there is nothing to gain from fetching more of it.
                log.warning("[sync] page rejected  peer=%s  from_h=%d", peer, h)
                self._peer_progress.pop(peer, None)
                break
            if result:
                applied_any = True
                if progress:
                    progress(fork_from + len(tail_so_far) - 1, remote_height)
                # Applied means committed: self.cs has already moved onto
                # this tail (apply_fn's real commit happened inside it), so
                # a *stale* copy of it has nothing left to resume from -- the
                # next call computes a fresh fork_from against the new tip.
                self._peer_progress.pop(peer, None)
            else:
                # None: validated fine, just not enough proven work yet.
                # Not a failure of this page or this peer, only of how much
                # of their chain has landed so far -- keep fetching further
                # pages of the same claimed chain rather than giving up on
                # it. See this method's docstring for why this can't be
                # collapsed into the False case.
                log.debug("[sync] page not yet better, continuing  peer=%s  "
                         "up_to=%d", peer, fork_from + len(tail_so_far) - 1)

            # The window-sizing step below reacts to transport/timing health
            # (did the page land, how fast), which is independent of
            # whether it happened to already be enough to win the fork
            # choice comparison -- so it runs the same whether result was
            # True or None, same as before this method could tell the two
            # apart.
            if needed_retry:
                chunk = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_BACKOFF))
            else:
                baseline = self._peer_latency.get(peer)
                if baseline is None:
                    # First clean page ever from this peer: nothing to
                    # compare against yet, so fall back to a fraction of
                    # the worst-case timeout, the only reference point
                    # that exists before there's a "normal" for this path.
                    latency_frac = elapsed / SYNC_FETCH_TIMEOUT
                    if latency_frac < FETCH_LATENCY_BOOTSTRAP_GROW_BELOW:
                        chunk = min(MAX_FETCH_CHUNK, max(chunk + 1, int(chunk * FETCH_CHUNK_GROWTH)))
                    elif latency_frac < FETCH_LATENCY_BOOTSTRAP_HOLD_BELOW:
                        pass
                    else:
                        chunk = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_SOFT_BACKOFF))
                else:
                    latency_ratio = elapsed / baseline
                    if latency_ratio < FETCH_LATENCY_GROW_BELOW:
                        chunk = min(MAX_FETCH_CHUNK, max(chunk + 1, int(chunk * FETCH_CHUNK_GROWTH)))
                    elif latency_ratio < FETCH_LATENCY_HOLD_BELOW:
                        pass  # close enough to normal to keep, not fast enough to push further
                    else:
                        chunk = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_SOFT_BACKOFF))

                # Folded into the baseline after classifying against it,
                # not before: every clean sample updates what "normal"
                # means for next time, bootstrap sample included (that's
                # what turns a None baseline into a real one).
                self._peer_latency[peer] = max(
                    FETCH_LATENCY_BASELINE_FLOOR,
                    elapsed if baseline is None
                    else (1 - FETCH_LATENCY_EMA_WEIGHT) * baseline + FETCH_LATENCY_EMA_WEIGHT * elapsed)

            # Written back after every page, not just at the end: a pass
            # that stops early (max_pages, budget) still keeps whatever
            # the window learned this round, for the next pass to pick up
            # from rather than throw away. See __init__ for why this
            # can't just live in a local variable any more.
            self._peer_chunk[peer] = chunk
            if len(self._peer_chunk) > 1000:
                # Defensive only: real peer counts stay in the hundreds at
                # most (MAX_PEERS), so this is a bound against something
                # having gone wrong upstream, not a cache with a real
                # eviction policy worth building. Both dicts are keyed the
                # same way and grow together, so they're capped together.
                self._peer_chunk.clear()
                self._peer_latency.clear()

            # Advance by what actually came back, not by what was asked
            # for. _serve_sync silently truncates to MAX_SYNC_BLOCKS
            # regardless of to_h, so a page shorter than requested is not
            # on its own proof the peer's chain ends here, only proof of
            # how much landed this round; the loop condition above is what
            # decides completion. Trusting `requested` here would have
            # made growing past the server's own cap a correctness bug,
            # not just a wasted ask: the first server-truncated page would
            # have looked like the end of the chain and stopped the sync
            # early, silently leaving the node behind.
            h += len(page)

        if not conclusive and not applied_any and tail_so_far:
            # Paused mid-fetch with nothing applied yet: keep what validated
            # so far so the next call for this peer resumes from h instead
            # of re-fetching fork_from..h all over again. Capped so a peer
            # whose claimed chain keeps growing without ever resolving can't
            # make us hold an unbounded amount of unapplied history; past
            # the cap this just falls back to the old re-fetch-from-scratch
            # behavior for that peer instead of growing further.
            if len(tail_so_far) <= MAX_PENDING_TAIL_BLOCKS:
                self._peer_progress[peer] = {
                    "fork_from": fork_from,
                    "anchor_hash": anchor_hash,
                    "tail": tail_so_far,
                    "h": h,
                }
            else:
                self._peer_progress.pop(peer, None)
            if len(self._peer_progress) > 1000:
                # Same defensive bound as _peer_chunk/_peer_latency above:
                # real peer counts stay in the hundreds (MAX_PEERS), so this
                # guards against something upstream having gone wrong, not a
                # cache with a real eviction policy worth building.
                self._peer_progress.clear()
        else:
            self._peer_progress.pop(peer, None)

        return applied_any, conclusive

    def _request_sync_with_retry(self, peer, from_h, to_h, timeout, deadline=None):
        """request_sync, retrying a bare timeout/decode-failure a few times
        before giving up. See SYNC_REQUEST_RETRIES for why.

        `timeout` on its own bounds one attempt, not this call: up to
        SYNC_REQUEST_RETRIES+1 attempts at that timeout, unbounded, is
        exactly how a caller's own `deadline` (check_and_sync's `budget`,
        there specifically to cap how long an unresponsive or malicious
        peer can hold the caller, since _expired(deadline) is otherwise
        only checked between pages, never inside one) stopped meaning
        anything once SYNC_FETCH_TIMEOUT grew large enough for a single
        retried attempt to blow through it on its own. Each attempt's
        timeout is clamped to whatever's actually left of `deadline`, and
        a deadline with nothing left to spend stops before ever calling
        udp.request_sync, rather than making one more attempt anyway.

        Returns (resp, needed_retry): needed_retry is True whenever the
        first attempt didn't land, which _fetch_and_apply reads as a sign
        of trouble on this path and reacts to by shrinking the next page,
        independently of whether a later attempt still succeeded.
        """
        for attempt in range(SYNC_REQUEST_RETRIES + 1):
            attempt_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, True
                attempt_timeout = min(timeout, remaining)
            resp = self.udp.request_sync(peer, from_h=from_h, to_h=to_h, timeout=attempt_timeout)
            if resp is not None:
                return resp, attempt > 0
        return None, True

    def _find_fork_point(self, peer, local_chain, deadline=None):
        """Binary search for the common ancestor, returning the first height
        that differs (so the caller fetches from there).

        A single probe at our own tip first, before any binary search at
        all: the ordinary case for a node that's simply behind, not
        actually forked, is that the peer's chain agrees with ours all
        the way to our own tip and only continues past it, which makes
        the real fork point our tip + 1. Binary search still finds that
        correctly, but only after confirming it as the boundary of a
        search range, costing ~log2(FORK_SEARCH_WINDOW) round trips to
        answer a question one direct probe already answers outright. The
        tip is the one height a binary search never tries first (it
        starts at the midpoint of its range), so nothing below was
        already covering this case.

        Only once that probe shows real divergence (the tip itself
        doesn't match, or came back empty) does an actual search start:
        a recent window first, widening to the whole chain only when the
        window's own base already diverges. Forks here are shallow by
        construction, a lost race resolves within a block or two, so
        searching from genesis every time charged O(log chain) round
        trips, growing with chain length forever, to rediscover a fork a
        few blocks back. Widening keeps the deep case correct; it just
        stops being the price of the common one.
        """
        tip = len(local_chain) - 1
        resp, _needed_retry = self._request_sync_with_retry(
            peer, from_h=tip, to_h=tip, timeout=FORK_PROBE_TIMEOUT, deadline=deadline)
        if resp is None:
            log.debug("[sync] peer stopped answering the tip probe  peer=%s", peer)
            return None
        page = resp.get("chain") if isinstance(resp, dict) else None
        if isinstance(page, list) and page and page[0].get("hash") == local_chain[tip]["hash"]:
            return tip + 1
        if _expired(deadline):
            return None

        window_lo = max(0, tip - FORK_SEARCH_WINDOW)
        try:
            if window_lo > 0:
                match = self._highest_common(peer, local_chain, window_lo, deadline)
                if match is not None:
                    return match + 1
                if _expired(deadline):
                    return None
                log.debug("[sync] fork older than recent window, widening  peer=%s", peer)
            match = self._highest_common(peer, local_chain, 0, deadline)
        except _Unanswered:
            # Give up on this peer rather than guess. Returning 0 here is
            # the expensive wrong answer: it claims the fork is at genesis
            # on no evidence and starts refetching the entire chain.
            return None
        if match is None and _expired(deadline):
            return None
        return (match + 1) if match is not None else 0

    def _highest_common(self, peer, local_chain, lo, deadline=None):
        """Highest height in [lo, tip] where our block and the peer's match,
        or None if even `lo` differs.

        Raises _Unanswered if the peer stopped answering. That is not the
        same event as an empty answer and must not be read as one: an empty
        answer means "I do not have that height", which is real information
        and narrows the search downward, while silence means we learned
        nothing at all. Treating the two alike walked the search down to
        nothing on a peer that had simply gone quiet, reported a fork point
        of 0, and sent the node off to refetch the chain from genesis over
        a few dropped datagrams.
        """
        hi = len(local_chain) - 1
        result = None

        while lo <= hi:
            if _expired(deadline):
                log.debug("[sync] fork search out of budget  peer=%s", peer)
                return result
            mid = (lo + hi) // 2
            local_hash = local_chain[mid]["hash"]

            resp, _needed_retry = self._request_sync_with_retry(
                peer, from_h=mid, to_h=mid, timeout=FORK_PROBE_TIMEOUT, deadline=deadline)
            if resp is None:
                log.debug("[sync] peer stopped answering mid fork search  peer=%s", peer)
                raise _Unanswered()
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                # Peer doesn't have this height; their chain is shorter, search lower.
                hi = mid - 1
                continue

            if page[0].get("hash") == local_hash:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return result
