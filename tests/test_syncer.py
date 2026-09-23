"""
Unit tests for syncer.py (UDP transport edition)

Covers: check_and_sync (no peers, peer not ahead, fetch error, success,
multi-page pagination, a later page being rejected),
_find_fork_point (binary search, shared tip, genesis diverge, error).

UDP calls are mocked via udp.request_sync. No network.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from syncer import Syncer, FETCH_CHUNK, MIN_FETCH_CHUNK
from tests.fixtures import genesis, make_block


def make_syncer(peers=None):
    pool = MagicMock()
    pool.random.return_value = peers[0] if peers else None
    udp = MagicMock()
    return Syncer(pool=pool, udp=udp), pool, udp


def chain_of(n):
    chain = [genesis()]
    for h in range(1, n):
        chain.append(make_block(h, chain[-1]["hash"], []))
    return chain


def tail_from(start_height, n):
    """n blocks at heights start_height..start_height+n-1, chained to each
    other. For exercising _fetch_and_apply's own mechanics (page-matches-
    request validation, pagination, backoff), not consensus linkage to any
    particular local chain, which apply_fn is mocked past anyway in these
    tests."""
    blocks = []
    prev_hash = "0" * 64
    for h in range(start_height, start_height + n):
        blk = make_block(h, prev_hash, [])
        blocks.append(blk)
        prev_hash = blk["hash"]
    return blocks


def wrap_chain(blocks):
    """Wrap a block list in the SYNC response envelope."""
    return {"genesis": "test", "chain": blocks}


def wrap_info(height, tip_hash=""):
    """Wrap an info response in the SYNC response envelope."""
    return {"genesis": "test", "chain": {"height": height, "tip_hash": tip_hash}}


# ---------------------------------------------------------------------------
# 1. check_and_sync
# ---------------------------------------------------------------------------

class TestCheckAndSync:
    def test_no_peers_returns_false(self):
        syncer, pool, udp = make_syncer(peers=None)
        assert syncer.check_and_sync(chain_of(3), apply_fn=MagicMock()) is False

    def test_info_request_fails_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = None
        assert syncer.check_and_sync(chain_of(3), apply_fn=MagicMock()) is False
        pool.strike.assert_not_called()

    def test_peer_with_no_more_work_costs_one_round_trip(self):
        """A peer that doesn't even claim more proven work is dropped after
        the GETINFO. It used to cost a full O(log chain) fork-point search
        plus a fetch to reach the same conclusion, every time, growing with
        chain length."""
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 2, "tip_hash": "", "work": 100}
        apply_fn = MagicMock(return_value=False)
        assert syncer.check_and_sync(chain_of(5), apply_fn=apply_fn,
                                     local_work=100) is False
        apply_fn.assert_not_called()
        udp.request_sync.assert_not_called()

    def test_shorter_chain_with_more_work_is_still_fetched(self):
        """Height is not what fork choice compares: forks retarget from
        their own timestamps, so a shorter chain can carry strictly more
        proven work, and a padded-timestamp fork with a low iteration
        requirement is the attack is_better_than exists to defeat. Bailing
        on height would decline to look at the chain that beats us."""
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 2, "tip_hash": "", "work": 999}
        udp.request_sync.return_value = wrap_chain(tail_from(0, 2))
        apply_fn = MagicMock(return_value=False)
        with patch.object(syncer, "_find_fork_point", return_value=0):
            syncer.check_and_sync(chain_of(5), apply_fn=apply_fn, local_work=100)
        apply_fn.assert_called_once()

    def test_peer_too_old_to_report_work_is_not_bailed_on(self):
        """Unknown is not zero: a peer that doesn't send the field at all
        falls through to a real comparison rather than being skipped."""
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 9, "tip_hash": ""}
        udp.request_sync.return_value = wrap_chain(tail_from(0, 2))
        apply_fn = MagicMock(return_value=False)
        with patch.object(syncer, "_find_fork_point", return_value=0):
            syncer.check_and_sync(chain_of(5), apply_fn=apply_fn, local_work=100)
        apply_fn.assert_called_once()

    def test_peer_ahead_is_fetched(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 9, "tip_hash": "", "work": 999}
        udp.request_sync.return_value = wrap_chain(tail_from(0, 2))
        apply_fn = MagicMock(return_value=False)
        with patch.object(syncer, "_find_fork_point", return_value=0):
            syncer.check_and_sync(chain_of(5), apply_fn=apply_fn, local_work=1)
        apply_fn.assert_called_once()

    def test_caller_can_name_the_peer_to_ask(self):
        """node.py learns who is ahead from the block that proved it, so it
        asks that peer instead of paying for a random draw."""
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 1, "tip_hash": ""}
        syncer.check_and_sync(chain_of(5), apply_fn=MagicMock(), peer="9.9.9.9:1")
        pool.random.assert_not_called()
        assert udp.get_info.call_args[0][0] == "9.9.9.9:1"

    def test_fork_point_none_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 10, "tip_hash": ""}
        with patch.object(syncer, "_find_fork_point", return_value=None):
            assert syncer.check_and_sync(chain_of(2), apply_fn=MagicMock()) is False

    def test_empty_tail_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 10, "tip_hash": ""}
        udp.request_sync.return_value = None
        with patch.object(syncer, "_find_fork_point", return_value=0):
            assert syncer.check_and_sync(chain_of(2), apply_fn=MagicMock()) is False

    def test_success_calls_apply_fn(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        remote_tail = chain_of(5)[1:]
        apply_fn = MagicMock(return_value=True)
        udp.get_info.return_value = {"height": 4, "tip_hash": "aa" * 32}
        udp.request_sync.return_value = wrap_chain(remote_tail)
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(local, apply_fn=apply_fn)
        apply_fn.assert_called_once()
        assert result is True

    def test_multi_page_applies_each_page(self):
        # A tail longer than one FETCH_CHUNK should call apply_fn once per
        # page, not once for the whole tail. This is the actual behavior
        # change: height should be able to advance incrementally instead of
        # jumping straight from local height to final height in one step.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        page1 = tail_from(1, FETCH_CHUNK)      # heights 1..FETCH_CHUNK
        page2 = tail_from(FETCH_CHUNK + 1, 2)  # heights FETCH_CHUNK+1..FETCH_CHUNK+2
        responses = iter([wrap_chain(page1), wrap_chain(page2)])
        udp.get_info.return_value = {"height": FETCH_CHUNK + 2, "tip_hash": ""}
        udp.request_sync.side_effect = lambda *a, **kw: next(responses)
        apply_fn = MagicMock(return_value=True)
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(local, apply_fn=apply_fn)
        assert apply_fn.call_count == 2
        assert result is True

    def test_page_rejected_stops_but_keeps_earlier_progress(self):
        # If a later page is rejected, check_and_sync should still report
        # True (earlier pages were already applied) rather than throwing
        # away progress that already landed.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        page1 = tail_from(1, FETCH_CHUNK)
        page2 = tail_from(FETCH_CHUNK + 1, 2)
        responses = iter([wrap_chain(page1), wrap_chain(page2)])
        udp.get_info.return_value = {"height": FETCH_CHUNK + 2, "tip_hash": ""}
        udp.request_sync.side_effect = lambda *a, **kw: next(responses)
        apply_fn = MagicMock(side_effect=[True, False])
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(local, apply_fn=apply_fn)
        assert apply_fn.call_count == 2
        assert result is True


# ---------------------------------------------------------------------------
# 1b. The adaptive window: persistence, backoff, and request validation.
# Added after a real production bug (the window resetting to FETCH_CHUNK
# on every call instead of surviving across the short passes a real sync
# is actually made of) shipped with no test catching it. See syncer.py's
# own Syncer.__init__ comment for the full story.
# ---------------------------------------------------------------------------

class TestAdaptiveWindow:
    def test_window_persists_across_separate_calls(self):
        # The actual bug: node.py calls check_and_sync repeatedly with a
        # small max_pages, not once for a whole catch-up. A window that
        # forgot everything between calls never got room to grow past its
        # very first step, on any connection, however good. Two separate
        # check_and_sync calls, each capped to one page, must still let
        # the second call see whatever the first one learned.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        peer = "1.2.3.4:9000"
        local = chain_of(2)

        def make_response(from_h, to_h, timeout, deadline=None):
            return wrap_chain(tail_from(from_h, min(to_h - from_h + 1, FETCH_CHUNK * 5)))

        udp.request_sync.side_effect = lambda peer, from_h, to_h, timeout: make_response(from_h, to_h, timeout)
        udp.get_info.return_value = {"height": 100_000, "tip_hash": ""}
        apply_fn = MagicMock(return_value=True)

        with patch.object(syncer, "_find_fork_point", return_value=1):
            syncer.check_and_sync(local, apply_fn=apply_fn, peer=peer, max_pages=1)
        first_chunk = syncer._peer_chunk[peer]
        assert first_chunk > FETCH_CHUNK, (
            "a clean, fast page should have grown the window past its starting size")

        with patch.object(syncer, "_find_fork_point", return_value=1):
            syncer.check_and_sync(local, apply_fn=apply_fn, peer=peer, max_pages=1)
        second_chunk = syncer._peer_chunk[peer]
        assert second_chunk > first_chunk, (
            "a second call must build on the first call's window, not reset to FETCH_CHUNK")

    def test_outright_failure_drops_straight_to_minimum(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        peer = "1.2.3.4:9000"
        syncer._peer_chunk[peer] = 10_000  # simulate an already-large, learned window
        udp.get_info.return_value = {"height": 50, "tip_hash": ""}
        udp.request_sync.return_value = None  # every attempt fails outright
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(chain_of(2), apply_fn=MagicMock(), peer=peer)
        assert result is False
        assert syncer._peer_chunk[peer] == MIN_FETCH_CHUNK

    def test_page_starting_at_wrong_height_is_rejected(self):
        # A peer (buggy or hostile) answering with blocks that don't start
        # where they were asked to must not be folded into full_chain and
        # handed to apply_fn as if it might be legitimate.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        wrong_page = tail_from(99, 3)  # asked for height 1, this starts at 99
        udp.get_info.return_value = {"height": 5, "tip_hash": ""}
        udp.request_sync.return_value = wrap_chain(wrong_page)
        apply_fn = MagicMock(return_value=True)
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(chain_of(2), apply_fn=apply_fn)
        apply_fn.assert_not_called()
        assert result is False

    def test_page_longer_than_requested_range_is_rejected(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        # Asked for height 1 only (local height 1, remote height 2), but
        # the peer sends back more blocks than that range could hold.
        oversized_page = tail_from(1, 10)
        udp.get_info.return_value = {"height": 2, "tip_hash": ""}
        udp.request_sync.return_value = wrap_chain(oversized_page)
        apply_fn = MagicMock(return_value=True)
        with patch.object(syncer, "_find_fork_point", return_value=1):
            result = syncer.check_and_sync(chain_of(2), apply_fn=apply_fn)
        apply_fn.assert_not_called()
        assert result is False

    def test_deadline_bounds_total_retry_time(self):
        # _request_sync_with_retry used to always wait up to `timeout` per
        # attempt regardless of the caller's own deadline, so a large
        # SYNC_FETCH_TIMEOUT could make budget (meant to bound how long an
        # unresponsive peer can hold the caller) meaningless. A slow-but-
        # eventually-timing-out peer here must not be allowed to hold the
        # call anywhere near budget * SYNC_REQUEST_RETRIES.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 100, "tip_hash": "", "work": 10**9}

        def slow_request(*a, **kw):
            time.sleep(0.05)
            return None

        udp.request_sync.side_effect = slow_request
        started = time.monotonic()
        with patch.object(syncer, "_find_fork_point", return_value=1):
            syncer.check_and_sync(chain_of(2), apply_fn=MagicMock(return_value=False),
                                  local_work=1, budget=0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, (
            f"took {elapsed:.2f}s against a 0.2s budget; deadline is not bounding retries")


# ---------------------------------------------------------------------------
# 2. _find_fork_point
# ---------------------------------------------------------------------------

class TestFindForkPoint:
    def test_shared_tip_returns_height_plus_one(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)

        def fake_sync(peer, from_h, to_h, timeout):
            return wrap_chain([local[from_h]])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 3

    def test_diverged_at_block_1(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=99)

        def fake_sync(peer, from_h, to_h, timeout):
            if from_h == 0:
                return wrap_chain([local[0]])
            return wrap_chain([remote_blk1])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1

    def test_unanswered_probe_gives_up_rather_than_guessing_genesis(self):
        # A peer that answers nothing at all has told us nothing at all,
        # which is a different event from answering "I don't have that
        # height" (covered by the next test, which does converge to 0).
        # This used to take the same branch as the empty answer, so a few
        # dropped datagrams walked the search down to genesis and reported
        # a fork point of 0 on no evidence, sending the node off to refetch
        # the whole chain from a peer that had gone quiet. None means "ask
        # someone else", and costs one pass instead of a full resync.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = None
        fp = syncer._find_fork_point("1.2.3.4:9000", chain_of(3))
        assert fp is None

    def test_empty_chain_in_response_syncs_from_genesis(self):
        # Empty chain in every response means peer has no matching history;
        # search converges to 0, returning sync-from-genesis.
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = wrap_chain([])
        fp = syncer._find_fork_point("1.2.3.4:9000", chain_of(2))
        assert fp == 0

    def test_genesis_only_shared_returns_one(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=99)

        def fake_sync(peer, from_h, to_h, timeout):
            if from_h == 0:
                return wrap_chain([local[0]])
            return wrap_chain([remote_blk1])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1




class TestPassBudget:
    """max_pages bounds the work we choose to do; the budget bounds the work
    a peer can make us wait for. Every request can time out and retry, so an
    unresponsive peer could otherwise hold the node loop for minutes a pass,
    and claiming a high tip costs an attacker nothing. A blocked loop drains
    nothing and forwards nothing, which under a stem kills whatever hop was
    handed to it."""

    def test_a_stalling_peer_cannot_hold_the_pass_open(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 10_000, "tip_hash": "", "work": 10**9}

        def slow_request(*a, **kw):
            time.sleep(0.05)
            return None            # never answers usefully

        udp.request_sync.side_effect = slow_request
        started = time.monotonic()
        syncer.check_and_sync(chain_of(200), apply_fn=MagicMock(return_value=False),
                              local_work=1, budget=0.2)
        assert time.monotonic() - started < 2.0

    def test_no_budget_behaves_as_before(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 9, "tip_hash": "", "work": 999}
        udp.request_sync.return_value = wrap_chain(tail_from(0, 2))
        apply_fn = MagicMock(return_value=False)
        with patch.object(syncer, "_find_fork_point", return_value=0):
            syncer.check_and_sync(chain_of(5), apply_fn=apply_fn, local_work=1)
        apply_fn.assert_called_once()
