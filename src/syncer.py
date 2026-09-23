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
# rather than only after. Below FETCH_LATENCY_GROW_BELOW of the timeout,
# there is room to spare and the window grows as before. Between that and
# FETCH_LATENCY_HOLD_BELOW, it holds steady, neither pushing its luck nor
# giving back ground it hasn't been shown to need to. At or past
# FETCH_LATENCY_HOLD_BELOW, it eases back on purpose, before a timeout
# forces the same outcome the hard way. This is what keeps the window
# from sawing all the way up to a failure and back on every cycle: it
# feels the path slowing down (bigger blocks, a loaded peer, a
# congested link) and responds while still succeeding, rather than only
# ever discovering the ceiling by falling through it.
FETCH_LATENCY_GROW_BELOW = 0.4
FETCH_LATENCY_HOLD_BELOW = 0.75

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

        return self._fetch_and_apply(peer, local_chain, fork_from, remote_height,
                                     apply_fn, max_pages, deadline, progress)

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

        Returns True if at least one page was applied.
        """
        applied_any = False
        tail_so_far = []
        pages = 0
        h = fork_from
        chunk = self._peer_chunk.get(peer, FETCH_CHUNK)
        while h <= remote_height:
            if _expired(deadline):
                log.debug("[sync] out of budget after %d pages  peer=%s", pages, peer)
                break
            if max_pages is not None and pages >= max_pages:
                log.debug("[sync] pausing after %d pages, resuming next pass", pages)
                break
            pages += 1
            to_h = min(h + chunk - 1, remote_height)
            t0 = time.monotonic()
            resp, needed_retry = self._request_sync_with_retry(
                peer, from_h=h, to_h=to_h, timeout=SYNC_FETCH_TIMEOUT)
            elapsed = time.monotonic() - t0
            if resp is None:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                # A harder failure than needed_retry (every attempt in
                # _request_sync_with_retry came back empty, not just the
                # first one), so it gets at least as hard a backoff,
                # written back before returning. Otherwise the next pass,
                # a fresh call a few seconds later, would retry this same
                # peer at the same too-large size and fail the same way
                # again, possibly indefinitely.
                self._peer_chunk[peer] = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_BACKOFF))
                break
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                break

            tail_so_far += page
            full_chain = local_chain[:fork_from] + tail_so_far
            if not apply_fn(full_chain):
                log.warning("[sync] page rejected  peer=%s  from_h=%d", peer, h)
                break
            applied_any = True
            if progress:
                progress(fork_from + len(tail_so_far) - 1, remote_height)

            if needed_retry:
                chunk = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_BACKOFF))
            else:
                latency_frac = elapsed / SYNC_FETCH_TIMEOUT
                if latency_frac < FETCH_LATENCY_GROW_BELOW:
                    chunk = min(MAX_FETCH_CHUNK, max(chunk + 1, int(chunk * FETCH_CHUNK_GROWTH)))
                elif latency_frac < FETCH_LATENCY_HOLD_BELOW:
                    pass  # fast enough to keep, not fast enough to push further
                else:
                    chunk = max(MIN_FETCH_CHUNK, int(chunk * FETCH_CHUNK_SOFT_BACKOFF))

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
                # eviction policy worth building.
                self._peer_chunk.clear()

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
        return applied_any

    def _request_sync_with_retry(self, peer, from_h, to_h, timeout):
        """request_sync, retrying a bare timeout/decode-failure a few times
        before giving up. See SYNC_REQUEST_RETRIES for why.

        Returns (resp, needed_retry): needed_retry is True whenever the
        first attempt didn't land, which _fetch_and_apply reads as a sign
        of trouble on this path and reacts to by shrinking the next page,
        independently of whether a later attempt still succeeded.
        """
        for attempt in range(SYNC_REQUEST_RETRIES + 1):
            resp = self.udp.request_sync(peer, from_h=from_h, to_h=to_h, timeout=timeout)
            if resp is not None:
                return resp, attempt > 0
        return None, True

    def _find_fork_point(self, peer, local_chain, deadline=None):
        """Binary search for the common ancestor, returning the first height
        that differs (so the caller fetches from there).

        Searched over a recent window first, widening to the whole chain
        only when the window's own base already diverges. Forks here are
        shallow by construction, a lost race resolves within a block or
        two, so searching from genesis every time charged O(log chain)
        round trips, growing with chain length forever, to rediscover a
        fork a few blocks back. Widening keeps the deep case correct; it
        just stops being the price of the common one.
        """
        window_lo = max(0, len(local_chain) - 1 - FORK_SEARCH_WINDOW)
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
                peer, from_h=mid, to_h=mid, timeout=FORK_PROBE_TIMEOUT)
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
