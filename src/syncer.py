"""Periodic chain sync over UDP.

Uses UDPTransport.get_info() for lightweight tip comparison and
UDPTransport.request_sync() for fetching chain segments.

Fork choice: most cumulative proven VDF work wins; tip hash breaks ties.
See ChainState.is_better_than().
"""

import logging
import time

log = logging.getLogger("ec.syncer")

FETCH_CHUNK = 50    # blocks per GETSYNC request
# peer_udp.py now has real chunk-level ACK/retransmit for multi-chunk UDP
# messages, so a single dropped datagram no longer silently fails an entire
# page, the old rationale for keeping this very small (5) no longer
# applies. 50 is chosen the way real sync protocols size a batch: well
# below the hard caps (MAX_SYNC_BLOCKS=500 blocks per request, and
# MAX_CHUNK_TOTAL=2000 chunks / ~2.8MB per reassembled message in
# peer_udp.py), not matched to them, a block would need to average
# ~56KB for 50 of them to approach that reassembly ceiling even under
# heavy real transaction load (FALCON-512 signatures run large, but not
# that large). Fewer round trips than before for a long initial sync,
# with real recovery underneath if a chunk is still lost along the way.

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
        """Fetch in FETCH_CHUNK-block pages, applying each page as it
        arrives instead of buffering the whole tail and applying it once at
        the end.

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
        while h <= remote_height:
            if _expired(deadline):
                log.debug("[sync] out of budget after %d pages  peer=%s", pages, peer)
                break
            if max_pages is not None and pages >= max_pages:
                log.debug("[sync] pausing after %d pages, resuming next pass", pages)
                break
            pages += 1
            to_h = min(h + FETCH_CHUNK - 1, remote_height)
            resp = self._request_sync_with_retry(peer, from_h=h, to_h=to_h, timeout=30)
            if resp is None:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
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

            if len(page) < FETCH_CHUNK:
                break
            h += FETCH_CHUNK
        return applied_any

    def _request_sync_with_retry(self, peer, from_h, to_h, timeout):
        """request_sync, retrying a bare timeout/decode-failure a few times
        before giving up. See SYNC_REQUEST_RETRIES for why."""
        for _attempt in range(SYNC_REQUEST_RETRIES + 1):
            resp = self.udp.request_sync(peer, from_h=from_h, to_h=to_h, timeout=timeout)
            if resp is not None:
                return resp
        return None

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

            resp = self._request_sync_with_retry(peer, from_h=mid, to_h=mid, timeout=10)
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
