"""
Unit tests for node.py

Covers every non-VDF method:
  _validate_tail, NodeView, Node._load_cs, Node.is_signing_active,
  Node.mark_tx_seen, Node.get_info, Node.submit_tx, Node.build_and_sign_tx,
  Node._pick_winner, Node._commit, Node._drain_queue, Node._handle,
  Node._handle_inbound_tx, Node._evaluate_remote_chain,
  Node.apply_better_chain, Node._reorg_mempool.

Storage is backed by a real SQLite in-memory equivalent (tmp_path).
Gossip, syncer, pool, net_in_q are mocked. VDF is mocked.
"""

import os
import sys
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import crypto
import node as node_mod
import state as state_mod
import tx as tx_mod
from chainstate import ChainState
from node import Node, NodeView, _validate_tail
from params import TICKS_PER_LAPSE
from tests.fixtures import (
    address, genesis, keypair, make_block, make_tx,
)


# ---------------------------------------------------------------------------
# VDF mock, applied everywhere
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# Node factory, creates a real Node with mocked networking deps
# ---------------------------------------------------------------------------

@pytest.fixture
def node_env(tmp_path):
    """Return (node, keyfile_path) with real storage, mocked net."""
    sk, pk = keypair(0)
    keyfile = str(tmp_path / "node.key")
    passphrase = "testpass"
    crypto.save_key(keyfile, sk, pk, passphrase)
    kek = crypto.derive_kek(keyfile, passphrase)

    gossip  = MagicMock()
    gossip.mark_seen.return_value = False
    # relay() reports whether the item went public; a bare MagicMock()
    # returns a truthy Mock, which would read as "this fluffed" on every
    # stem hop. See Gossip.relay.
    gossip.relay.return_value = False
    syncer  = MagicMock()
    # Real check_and_sync returns True only when it actually adopted a
    # better chain (syncer.py docstring); a bare MagicMock() call would
    # otherwise return a truthy Mock by default and make _run_cycle think
    # every check found a better chain, cancelling the in-flight VDF for no
    # reason. Tests that want to simulate a real mid-wait reorg override
    # this explicitly (see TestRunCycleSync).
    syncer.check_and_sync.return_value = False
    pool    = MagicMock()
    pool.snapshot.return_value = []
    pool.count.return_value = 3
    net_q   = queue.Queue()
    db_path = str(tmp_path / "chain.db")

    node = Node(
        keyfile=keyfile,
        public_key=pk,
        gossip=gossip,
        syncer=syncer,
        pool=pool,
        net_in_q=net_q,
        db_path=db_path,
    )
    node._loop_thread = threading.current_thread()
    node._kek = kek
    return node, keyfile, kek, gossip, syncer, pool, net_q


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. _validate_tail (module-level pure function)
# ---------------------------------------------------------------------------

class TestValidateTail:
    def test_empty_tail_passes(self):
        ok, err, cs = _validate_tail([], [genesis()])
        assert ok is True, err
        assert cs.height == 0

    def test_single_valid_block_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        ok, err, cs = _validate_tail([b1], [g])
        assert ok is True, err
        assert cs.height == 1

    def test_invalid_block_in_tail_fails(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b1["previous_hash"] = "00" * 32
        b1["hash"] = block_mod.block_hash(b1)
        ok, err, cs = _validate_tail([b1], [g])
        assert ok is False
        assert cs is None

    def test_two_valid_blocks_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err, cs = _validate_tail([b1, b2], [g])
        assert ok is True, err
        assert cs.height == 2


# ---------------------------------------------------------------------------
# 2. NodeView
# ---------------------------------------------------------------------------

class TestNodeView:
    def test_node_view_exposes_chain_properties(self):
        cs = ChainState.from_genesis()
        v = NodeView(cs)
        assert v.height == 0
        assert v.tip["hash"] == cs.tip["hash"]
        assert v.genesis_hash == cs.genesis_hash

    def test_node_view_state_is_snapshot(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000)
        v = NodeView(cs)
        # Mutate original, view's snapshot should not change
        cs.state.credit(address(0), 9999)
        assert v.state.get_balance(address(0)) == 1000


# ---------------------------------------------------------------------------
# 3. Node._load_cs (genesis path and reload path)
# ---------------------------------------------------------------------------

class TestLoadCs:
    def test_load_cs_creates_genesis_when_empty(self, node_env):
        node, *_ = node_env
        assert node.cs.height == 0

    def test_load_cs_genesis_saved_to_storage(self, node_env):
        node, *_ = node_env
        stored = node.storage.load_all_blocks()
        assert len(stored) == 1
        assert stored[0]["height"] == 0

    def test_load_cs_reloads_from_storage(self, tmp_path):
        """Second Node creation with same db_path reloads existing chain."""
        sk, pk = keypair(1)
        keyfile = str(tmp_path / "node2.key")
        crypto.save_key(keyfile, sk, pk, "pass")
        db_path = str(tmp_path / "chain2.db")

        # First node, creates genesis
        n1 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n1.cs.height == 0

        # Second node, should reload genesis from db
        n2 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n2.cs.height == 0
        assert n2.cs.tip["hash"] == n1.cs.tip["hash"]


# ---------------------------------------------------------------------------
# 4. Simple accessors
# ---------------------------------------------------------------------------

class TestSimpleAccessors:
    def test_is_signing_active_true_when_kek_set(self, node_env):
        node, *_ = node_env
        assert node.is_signing_active() is True

    def test_is_signing_active_false_when_no_kek(self, node_env):
        node, *_ = node_env
        node._kek = None
        assert node.is_signing_active() is False

    def test_mark_tx_seen_delegates_to_gossip(self, node_env):
        node, _, __, gossip, *_ = node_env
        gossip.mark_seen.return_value = False
        result = node.mark_tx_seen("abc")
        gossip.mark_seen.assert_called_once_with("abc")
        assert result is False

    def test_get_info_returns_expected_keys(self, node_env):
        node, *_ = node_env
        info = node.get_info()
        for key in ["height", "tip_hash", "genesis_hash",
                    "mempool_size", "address", "peer_count", "total_minted",
                    "can_mint", "block_reward"]:
            assert key in info

    def test_get_info_can_mint_is_the_pool_not_the_reward(self, node_env):
        """can_mint is the mintable pool; block_reward is one block's cut of it.

        These were once the same key holding two different values: /api/info
        returned the per-block reward under the name can_mint while
        /api/stats returned the pool under that same name.
        """
        node, *_ = node_env
        info = node.get_info()
        sv   = node.view.state
        assert info["can_mint"]     == sv.compute_can_mint()
        assert info["block_reward"] == sv.compute_block_reward()
        assert info["block_reward"] < info["can_mint"]

    def test_get_info_height_is_zero_at_genesis(self, node_env):
        node, *_ = node_env
        assert node.get_info()["height"] == 0

    def test_block_time_ratio_is_none_before_any_build(self, node_env):
        node, *_ = node_env
        assert node.get_info()["block_time_ratio"] is None

    def test_block_time_ratio_is_none_with_only_genesis_even_if_own_builds_exist(self, node_env):
        """No chain-side median is possible with just genesis (no block-to-
        block delta exists yet), so the comparison has nothing to compare
        against even though this node has real build history."""
        node, *_ = node_env
        node._own_build_seconds.append(130.0)
        assert node.get_info()["block_time_ratio"] is None

    def test_block_time_ratio_compares_own_median_to_chain_median(self, node_env):
        """block_time_ratio must be this node's own median build time divided
        by the chain's own recent median block-to-block time. Not this
        node's latest build vs its own history, and not tied to whichever
        node happened to build the current tip."""
        node, *_ = node_env
        cs = ChainState.from_genesis()
        for h in range(1, 6):
            # 150s real gap at every height (120 base + 30*h offset).
            blk = make_block(h, cs.tip["hash"], [], timestamp_offset=30 * h)
            cs = cs.apply_block(blk)
        node.cs = cs
        node.view = NodeView(cs)
        for seconds in [130.0] * 10:
            node._own_build_seconds.append(seconds)
        assert node.get_info()["block_time_ratio"] == pytest.approx(130.0 / 150.0)

    def test_block_time_ratio_window_caps_at_30(self, node_env):
        node, *_ = node_env
        for seconds in [100.0] * 40:
            node._own_build_seconds.append(seconds)
        assert len(node._own_build_seconds) == 30


# ---------------------------------------------------------------------------
# 5. submit_tx
# ---------------------------------------------------------------------------

class TestSubmitTx:
    def test_submit_valid_tx_returns_true(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        ok, result = node.submit_tx(t)
        assert ok is True
        assert len(result) == 64

    def test_submit_tx_adds_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        assert node.mempool.size() == 1

    def test_submit_tx_enters_propagation(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        gossip.spread.assert_called_once()

    def test_submit_invalid_tx_returns_false(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, TICKS_PER_LAPSE, node.cs.state)
        ok, err = node.submit_tx(t)
        assert ok is False

    def test_submit_duplicate_tx_returns_false(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        ok, err = node.submit_tx(t)
        assert ok is False


# ---------------------------------------------------------------------------
# 6. build_and_sign_tx
# ---------------------------------------------------------------------------

class TestBuildAndSignTx:
    def test_build_and_sign_returns_tx_and_fee(self, node_env):
        node, keyfile, *_ = node_env
        node.cs.state.credit(node.addr, 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        # Rebuild view so it reflects the updated state
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": TICKS_PER_LAPSE}]
        t, fee = node.build_and_sign_tx(outputs, fee=100, passphrase="testpass")
        assert isinstance(t, dict)
        assert "signature" in t
        assert fee == 100

    def test_build_and_sign_signature_verifies(self, node_env):
        node, keyfile, *_ = node_env
        node.cs.state.credit(node.addr, 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": TICKS_PER_LAPSE}]
        t, _ = node.build_and_sign_tx(outputs, passphrase="testpass")
        ok, err = tx_mod.validate(t, node.cs.state)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 7. _pick_winner
# ---------------------------------------------------------------------------

class TestPickWinner:
    def _make_candidate(self, cs):
        g = cs.tip
        blk = make_block(g["height"] + 1, g["hash"], [], builder_index=0)
        return blk

    def test_own_candidate_wins_when_no_peers(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is candidate
        assert relay is False

    def test_stale_candidate_returns_none(self, node_env):
        node, *_ = node_env
        candidate = make_block(1, "00" * 32, [])  # wrong previous_hash
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is None

    def test_invalid_peer_block_ignored(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        # Peer block with wrong height
        bad_peer = make_block(99, node.cs.tip["hash"], [])
        winner, relay = node._pick_winner(node.cs, candidate, [bad_peer])
        assert winner is candidate

    def test_lowest_vdf_output_peer_block_wins(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        peer_blk  = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")
        candidate = make_block(1, g["hash"], [], builder_index=0, vdf_output="bb")
        winner, relay = node._pick_winner(node.cs, candidate, [peer_blk])
        # Same rule as ChainState.is_better_than: lowest vdf_output wins,
        # not whichever arrived first.
        assert winner is peer_blk
        assert relay is True

    def test_own_candidate_wins_tie_break_over_peer(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        peer_blk  = make_block(1, g["hash"], [], builder_index=1, vdf_output="zz")
        candidate = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa")
        winner, relay = node._pick_winner(node.cs, candidate, [peer_blk])
        assert winner is candidate
        assert relay is False


# ---------------------------------------------------------------------------
# 8. _commit
# ---------------------------------------------------------------------------

class TestCommit:
    def test_commit_updates_chainstate_height(self, node_env):
        node, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.cs.height == 1

    def test_commit_removes_confirmed_txs_from_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.mempool.add(t)
        blk = make_block(1, node.cs.tip["hash"], [t])
        node._commit(blk)
        assert node.mempool.size() == 0

    def test_commit_updates_view(self, node_env):
        node, *_ = node_env
        old_view = node.view
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.view is not old_view
        assert node.view.height == 1

    def test_commit_does_not_re_propagate(self, node_env):
        """Propagation happens once, where the block first appears: a peer's
        when it arrives (_handle_inbound_block), our own when we build it.
        Re-sending at commit would just be a second copy of something the
        network already has."""
        node, _, __, gossip, *_ = node_env
        g = node.cs.tip
        blk = make_block(1, g["hash"], [])
        node._commit(blk, relay=True)
        gossip.spread.assert_not_called()
        gossip.relay.assert_not_called()

# ---------------------------------------------------------------------------
# 9. _drain_queue / _handle
# ---------------------------------------------------------------------------

class TestDrainQueue:
    def test_drain_empty_queue_returns_empty(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blocks = node._drain_queue()
        assert blocks == []

    def test_drain_block_message(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        net_q.put({"type": "block", "block": blk})
        blocks = node._drain_queue()
        assert len(blocks) == 1
        assert blocks[0]["hash"] == blk["hash"]

    def test_drain_submit_tx_message(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        reply = queue.Queue()
        node.net_in_q.put({"type": "submit_tx", "tx": t, "reply": reply})
        node._drain_queue()
        ok, _ = reply.get_nowait()
        assert ok is True

    def test_drain_unknown_message_type_ignored(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        net_q.put({"type": "unknown_garbage"})
        blocks = node._drain_queue()  # must not raise
        assert blocks == []


# ---------------------------------------------------------------------------
# 10. _handle_inbound_tx
# ---------------------------------------------------------------------------

class TestHandleInboundTx:
    def test_fluff_valid_tx_added_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 1

    def test_fluff_invalid_tx_not_added(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 0

    def test_fluff_duplicate_not_added_again(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        node._handle_inbound_tx(msg)  # second time, duplicate
        assert node.mempool.size() == 1

    def test_stem_tx_is_validated_before_being_forwarded(self, node_env):
        """A tx still in the private phase is forwarded rather than
        admitted, we're a relay for it, not its destination, but it is
        validated first. Relaying something unvalidated would let anyone spend our
        bandwidth, and every downstream peer's, for one crafted datagram."""
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.args[3] == "1.2.3.4:1"
        assert gossip.relay.call_args.kwargs["stemming"] is True
        # forwarded, not admitted
        assert node.mempool.size() == 0

    def test_invalid_stem_tx_is_not_forwarded(self, node_env):
        node, _, __, gossip, *_ = node_env
        t = make_tx(0, 1, TICKS_PER_LAPSE, fresh_state())  # sender has no balance
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        gossip.relay.assert_not_called()

    def test_public_tx_is_admitted_and_passed_on(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": False})
        assert node.mempool.size() == 1
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.kwargs["stemming"] is False


# ---------------------------------------------------------------------------
# 11. _evaluate_remote_chain
# ---------------------------------------------------------------------------

class TestEvaluateRemoteChain:
    def test_empty_remote_chain_fails(self, node_env):
        node, *_ = node_env
        ok, err, *_ = node._evaluate_remote_chain([])
        assert ok is False

    def test_wrong_genesis_fails(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err, *_ = node._evaluate_remote_chain([wrong_g])
        assert ok is False
        assert "genesis" in err

    def test_not_better_than_local_fails(self, node_env):
        node, *_ = node_env
        # Remote chain is identical (same genesis only)
        remote = [node.cs.chain[0]]
        ok, err, *_ = node._evaluate_remote_chain(remote)
        assert ok is False
        assert "not better" in err

    def test_longer_valid_remote_chain_accepted(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err, fork_point, tail, remote_cs = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True, err
        assert remote_cs.height == 2

    def test_invalid_tail_block_fails(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        bad_b1 = make_block(1, "00" * 32, [])  # wrong previous_hash
        ok, err, *_ = node._evaluate_remote_chain([g, bad_b1])
        assert ok is False

    def test_fork_point_correct_for_same_genesis(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, _, fork_point, tail, _ = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True
        assert fork_point == 1  # local is at height 0, diverge at index 1
        assert len(tail) == 2

    def test_malformed_block_in_extension_rejected_not_raised(self, node_env):
        """A block missing a required field can still have a self-consistent
        hash (block_hash just hashes whatever's present), so validation can
        reach a raw dict access like blk["height"] and raise, instead of
        cleanly returning False. The pure-extension fast path must catch
        that the same way the reorg fallback path already does, rather
        than letting it escape as an unhandled exception."""
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        del b1["height"]
        b1["hash"] = block_mod.block_hash(b1)  # stays self-consistent
        ok, err, *_ = node._evaluate_remote_chain([g, b1])
        assert ok is False
        assert err  # some rejection reason, not an unhandled exception


class TestRecentStateCache:
    """_resume_point / _remember_state / _forget_states_from: a shallow
    reorg should resume from a cached state instead of a full replay, and
    that cache must never let a later reorg reuse state from a branch
    that's already been abandoned."""

    def test_shallow_reorg_resumes_from_cache_not_full_replay(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        b3 = make_block(3, b2["hash"], [])
        for b in (b1, b2, b3):
            node._commit(b)
        assert node.cs.height == 3
        assert set(node._recent_states) == {1, 2, 3}

        # A sibling fork at height 3 (same parent b2, different builder),
        # fork_point=3, resume_height=2, which is cached.
        b3_alt = make_block(3, b2["hash"], [], builder_index=1, vdf_output="00" * 100)

        import unittest.mock as _mock
        with _mock.patch.object(ChainState, "from_chain") as mocked_replay:
            ok, err, fork_point, tail, remote_cs = node._evaluate_remote_chain(
                [g, b1, b2, b3_alt])
        assert ok is True, err
        assert fork_point == 3
        mocked_replay.assert_not_called()
        assert remote_cs.height == 3

    def test_reorg_beyond_cache_falls_back_to_full_replay(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod, "RECENT_STATE_CACHE_SIZE", 1)
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        b3 = make_block(3, b2["hash"], [])
        for b in (b1, b2, b3):
            node._commit(b)
        # Cache holds only the most recent 1 entry now, height 1 and 2
        # (needed below, as resume_height=1) were evicted, and it's not
        # the free trivial genesis case either (resume_height != 0).
        assert set(node._recent_states) == {3}

        b2_alt = make_block(2, b1["hash"], [], builder_index=1, vdf_output="00" * 100)
        b3_alt = make_block(3, b2_alt["hash"], [], builder_index=1, vdf_output="00" * 100)

        import unittest.mock as _mock
        with _mock.patch.object(ChainState, "from_chain", wraps=ChainState.from_chain) as spy:
            ok, err, *_ = node._evaluate_remote_chain([g, b1, b2_alt, b3_alt])
        assert ok is True, err
        spy.assert_called_once()

    def test_stale_cache_entry_not_reused_after_reorg(self, node_env):
        """Reorg away from b2 (builder 0) to b2_b (builder 1), then reorg
        again to a third sibling b2_c at the same height. The second reorg
        must not reuse b2_b's now-abandoned state under b2's old cache
        slot. It should resume from the shared, untouched ancestor
        (height 1) instead, same as the first reorg did."""
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2_a = make_block(2, b1["hash"], [], builder_index=0)
        node._commit(b1)
        node._commit(b2_a)
        assert node.cs.tip["builder"] == address(0)

        b2_b = make_block(2, b1["hash"], [], builder_index=1, vdf_output="50" * 100)
        ok, err = node.apply_better_chain([g, b1, b2_b])
        assert ok is True, err
        assert node.cs.tip["builder"] == address(1)
        # The abandoned b2_a's height was purged, not left stale.
        assert node._recent_states[2][0] is not None
        assert node.cs.state.get_balance(address(1)) > 0

        b2_c = make_block(2, b1["hash"], [], builder_index=2, vdf_output="00" * 100)
        ok, err = node.apply_better_chain([g, b1, b2_c])
        assert ok is True, err
        assert node.cs.tip["builder"] == address(2)
        # Builder 1's reward from the now-abandoned b2_b must not linger.
        assert node.cs.state.get_balance(address(1)) == 0
        assert node.cs.state.get_balance(address(2)) > 0

# ---------------------------------------------------------------------------
# 12. apply_better_chain
# ---------------------------------------------------------------------------

class TestApplyBetterChain:
    def test_apply_better_chain_updates_cs(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err = node.apply_better_chain([g, b1, b2])
        assert ok is True, err
        assert node.cs.height == 2

    def test_apply_better_chain_updates_view(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        node.apply_better_chain([g, b1])
        assert node.view.height == 1

    def test_apply_worse_chain_rejected(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        # Same-height, same chain. Not better
        ok, err = node.apply_better_chain([g])
        assert ok is False

    def test_apply_better_chain_wrong_genesis_rejected(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err = node.apply_better_chain([wrong_g])
        assert ok is False
        assert "genesis" in err


# ---------------------------------------------------------------------------
# 13. _reorg_mempool
# ---------------------------------------------------------------------------

class TestReorgMempool:
    def test_reorg_restores_old_chain_txs(self, node_env):
        """Txs from the old chain that aren't in the new chain go back to mempool."""
        node, *_ = node_env
        g = node.cs.chain[0]
        # b0 is shared by both branches, so address(0)'s balance comes from
        # a real block reward replayed in the common prefix. No need to
        # hack a balance into a mocked from_chain, which also means the
        # reorg here lands within _resume_point's cache (fork_point=2,
        # resume_height=1, populated by the _commit(b0) below), exercising
        # the actual fast path rather than a full replay.
        b0 = make_block(1, g["hash"], [], builder_index=0)
        node._commit(b0)
        assert node.cs.height == 1

        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        b1_old = make_block(2, b0["hash"], [t])
        node._commit(b1_old)
        assert node.cs.height == 2

        # New chain shares b0 but replaces b1 with one that doesn't include t.
        b1_new = make_block(2, b0["hash"], [], builder_index=1)
        b2_new = make_block(3, b1_new["hash"], [], builder_index=1)
        full_chain = [g, b0, b1_new, b2_new]

        import unittest.mock as _mock
        with _mock.patch.object(node.cs.__class__, "is_better_than", return_value=True):
            ok, err = node.apply_better_chain(full_chain)
        assert ok is True, err
        # t was in the old chain at fork_point=2, is not in the new chain,
        # and is still valid (nonce/balance) against the new chain's state.
        assert node.mempool.get(tx_mod.tx_hash(t)) is not None

    def test_reorg_drops_confirmed_txs(self, node_env):
        """Txs confirmed in both old and new chains are NOT restored to the mempool.
        Calls _reorg_mempool directly since we're testing its logic, not full validation.
        """
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE

        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        h = tx_mod.tx_hash(t)
        g = node.cs.chain[0]

        # Place t in the old chain (node.cs.chain[1]) so _reorg_mempool sees it
        b1_old = make_block(1, g["hash"], [t])
        # Manually set cs to a chain containing b1_old so old_txs picks up t
        node.cs = ChainState.from_genesis()
        node.cs.chain.append(b1_old)  # add to chain list directly

        # New chain also contains t (same tx confirmed there too)
        b1_new = make_block(1, g["hash"], [t], builder_index=1)
        node._reorg_mempool(fork_point=1, old_chain=node.cs.chain,
                           new_chain=[g, b1_new], new_state=node.cs.state)

        # t is confirmed in new chain -> must NOT appear in mempool
        assert node.mempool.get(h) is None

    def test_reorg_does_not_readd_tx_invalid_under_new_state(self, node_env):
        """A tx from the abandoned branch that's no longer valid against the
        new chain's state (e.g. its nonce is already used there by a
        different tx) must not be silently re-admitted, doing so would
        make every subsequent self-produced block fail validation."""
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE

        g = node.cs.chain[0]
        t_old = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)  # nonce 1
        b1_old = make_block(1, g["hash"], [t_old])
        node.cs = ChainState.from_genesis()
        node.cs.chain.append(b1_old)

        # New chain confirms a *different* tx from address(0) at the same
        # nonce, so t_old's nonce is now stale against the new state.
        new_state = state_mod.State()
        new_state.credit(address(0), 100 * TICKS_PER_LAPSE)
        new_state.total_minted += 100 * TICKS_PER_LAPSE
        t_new = make_tx(0, 2, TICKS_PER_LAPSE, new_state, nonce_override=1)
        new_state.apply_tx(t_new)
        b1_new = make_block(1, g["hash"], [t_new], builder_index=1)

        node._reorg_mempool(fork_point=1, old_chain=node.cs.chain,
                           new_chain=[g, b1_new], new_state=new_state)

        assert node.mempool.get(tx_mod.tx_hash(t_old)) is None


# ---------------------------------------------------------------------------
# 15. _run_cycle: event-driven sync and the staleness guard
# ---------------------------------------------------------------------------

class TestRunCycleSync:
    """Sync happens on evidence, not on a schedule. The evidence is an
    inbound block above our tip, which propagation delivers for free and
    which names the peer holding it. If a sync adopts a better chain
    mid-wait, the in-flight VDF was computed for a tip that no longer
    exists and must be discarded, not committed."""

    def _slow_fake_evaluate(self, sleep_seconds):
        def _evaluate(challenge, iterations, handle=None):
            time.sleep(sleep_seconds)
            return "aa" * 100, "bb" * 100, sleep_seconds
        return _evaluate

    def test_quiet_network_still_probes_one_random_peer(self, node_env, monkeypatch):
        """Nothing arrived, so no peer has told us anything, which is
        exactly the state an eclipsed node is in too. A probe is one
        datagram that ends on the work comparison, so it is affordable
        every cycle; what used to make polling expensive was the fork
        search and fetch behind it, not its frequency."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        monkeypatch.setattr(node, "_probe_spacing", lambda: 0.0)
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))

        node._run_cycle()

        syncer.check_and_sync.assert_called()
        assert syncer.check_and_sync.call_args.kwargs["peer"] == "random.peer:1"

    def test_block_above_our_tip_triggers_a_sync_against_its_sender(
        self, node_env, monkeypatch
    ):
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        ahead = make_block(7, "00" * 32, [])
        net_q.put({"type": "block", "block": ahead, "sender": "9.9.9.9:1"})

        node._run_cycle()

        syncer.check_and_sync.assert_called()
        assert syncer.check_and_sync.call_args_list[0].kwargs["peer"] == "9.9.9.9:1"

    def test_a_block_at_or_below_our_tip_is_not_a_hint(self, node_env, monkeypatch):
        """It proves nothing about being behind, so it must not steer who
        we ask, the background probe picks at random instead."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        g = node.cs.tip
        net_q.put({"type": "block", "block": g, "sender": "9.9.9.9:1"})

        node._run_cycle()

        assert node._sync_hint is None
        for call_ in syncer.check_and_sync.call_args_list:
            assert call_.kwargs["peer"] != "9.9.9.9:1"

    def test_silence_past_the_chains_own_pace_polls_the_highest_peer(
        self, node_env, monkeypatch
    ):
        """The one case no inbound block can ever report: nothing is
        arriving at all. Threshold comes from the chain's measured median
        interval, and the peer from heights the pool already caches."""
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        # Threshold longer than this test's cycle, so the one poll silence
        # earns isn't repeated on every loop tick.
        monkeypatch.setattr(node, "_silence_threshold", lambda: 5.0)
        node._last_block_seen = time.monotonic() - 60
        # Relative, like _last_block_seen above. Left at its initial 0.0 it
        # reads as "long ago" only once time.monotonic() has climbed past
        # the threshold, which on Linux means once the machine has been up
        # that long, so the test would quietly depend on uptime.
        node._last_silence_poll = time.monotonic() - 60
        pool.snapshot.return_value = [
            ("low:1", 0, True, 3, "", "", "", None),
            ("high:1", 0, True, 99, "", "", "", None),
            ("offline:1", 0, False, 500, "", "", "", None),
        ]

        node._run_cycle()

        syncer.check_and_sync.assert_called()
        assert syncer.check_and_sync.call_args_list[0].kwargs["peer"] == "high:1"

    def test_mid_wait_reorg_discards_stale_candidate(self, node_env, monkeypatch):
        """Evidence that arrives *after* the VDF has started. The proof we
        end up with was computed for a tip that no longer exists, so it has
        to be thrown away rather than spliced onto the wrong parent.

        (Evidence arriving *before* the VDF starts is a different and better
        case: the cycle simply builds on the corrected tip, no work wasted.
        That's what test_block_above_our_tip_triggers_a_sync covers.)"""
        node, *_, syncer, pool, net_q = node_env
        replacement_cs = ChainState.from_genesis()

        def fake_check_and_sync(chain, apply_fn, **kwargs):
            node.cs = replacement_cs
            return True

        def evaluate_then_land_a_reorg(challenge, iterations, handle=None):
            net_q.put({"type": "block",
                       "block": make_block(7, "00" * 32, []),
                       "sender": "9.9.9.9:1"})
            time.sleep(0.3)
            return "aa" * 100, "bb" * 100, 0.3

        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", evaluate_then_land_a_reorg)
        syncer.check_and_sync.side_effect = fake_check_and_sync
        commit_spy = MagicMock()
        monkeypatch.setattr(node, "_commit", commit_spy)

        node._run_cycle()

        assert node.cs is replacement_cs
        commit_spy.assert_not_called()

    def test_no_stale_reorg_commits_normally(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.15))
        commit_spy = MagicMock()
        monkeypatch.setattr(node, "_commit", commit_spy)

        node._run_cycle()

        commit_spy.assert_called_once()

    def test_status_line_reflects_vdf_computation(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))

        node._run_cycle()

        assert "block 1" in node.status_line

    def test_status_line_updates_on_heartbeat(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod, "VDF_HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.3))

        seen = []
        real_drain = node._drain_queue

        def spying_drain(*args, **kwargs):
            seen.append(node.status_line)
            return real_drain(*args, **kwargs)

        monkeypatch.setattr(node, "_drain_queue", spying_drain)

        node._run_cycle()

        assert any("elapsed" in s for s in seen)


# ---------------------------------------------------------------------------
# 17. Rework: an item that never comes back gets re-sent
# ---------------------------------------------------------------------------

class TestRework:
    """A stem hands an item to one peer and forgets it. If that peer is a
    dead end (its only link is back to us) or the datagram is lost, the item
    stops there and nobody else hears about it, and the sender cannot tell
    either case from success. Noticing it never came back is the only signal
    available, and it's what makes stemming safe on a graph we can't see.
    Without it, a block could cost its builder the whole evaluation."""

    def test_item_that_echoes_back_is_not_re_sent(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        h = tx_mod.tx_hash(t)

        node._spread(t, "tx", h)
        assert h in node._unconfirmed_spreads
        node._note_echo(h)

        node._retry_unconfirmed_spreads()
        gossip.force_fluff.assert_not_called()

    def test_item_that_never_comes_back_is_flooded(self, node_env):
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])

        node._spread(blk, "block", blk["hash"])
        # Pretend the deadline has passed rather than waiting it out.
        item, kind, _, target = node._unconfirmed_spreads[blk["hash"]]
        node._unconfirmed_spreads[blk["hash"]] = (
            item, kind, time.monotonic() - 3600, target)

        node._retry_unconfirmed_spreads()

        gossip.force_fluff.assert_called_once()
        assert gossip.force_fluff.call_args.args[0] is blk
        # Cleared, so it isn't re-flooded on every subsequent tick.
        assert blk["hash"] not in node._unconfirmed_spreads

    def test_retry_floods_rather_than_stemming_again(self, node_env):
        """The first attempt already spent what privacy a stem buys, and the
        walk has now demonstrably failed once. Another private hand-off
        risks the same silent death; delivery wins on the retry."""
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._spread(blk, "block", blk["hash"])
        item, kind, _, target = node._unconfirmed_spreads[blk["hash"]]
        node._unconfirmed_spreads[blk["hash"]] = (
            item, kind, time.monotonic() - 3600, target)

        node._retry_unconfirmed_spreads()

        gossip.force_fluff.assert_called_once()
        gossip.spread.assert_called_once()   # only the original, no re-stem

    def test_echo_deadline_uses_measured_times_once_there_are_enough(self, node_env):
        node, *_ = node_env
        assert node._echo_deadline_seconds() == node_mod.ECHO_BOOTSTRAP_SECONDS
        for _ in range(10):
            node._echo_seconds.append(4.0)
        # p95 of the node's own round trips, doubled for the ordinary tail.
        assert node._echo_deadline_seconds() == 8.0


# ---------------------------------------------------------------------------
# 18. Never idle: advance now, settle the height afterwards
# ---------------------------------------------------------------------------

class TestNoIdleWaiting:
    def _fake_evaluate(self, seconds):
        def _evaluate(challenge, iterations, handle=None):
            elapsed = 0.0
            while elapsed < seconds:
                if handle is not None and handle._cancelled:
                    raise node_mod.vdf_mod.Cancelled()
                time.sleep(0.02)
                elapsed += 0.02
            return "aa" * 100, "bb" * 100, seconds
        return _evaluate

    def test_sibling_with_lower_output_is_taken_locally(self, node_env):
        """A candidate for a height we already committed is not late. It is
        a chain of equal work with a lower vdf_output, so we swap to it,
        built from the chain we already hold, no round trip, nobody asked."""
        node, *_, syncer, pool, net_q = node_env
        g = node.cs.tip
        mine  = make_block(1, g["hash"], [], builder_index=0, vdf_output="zz")
        theirs = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")
        node._commit(mine)
        assert node.cs.tip["hash"] == mine["hash"]

        node._handle_inbound_block(
            {"block": theirs, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == theirs["hash"]
        syncer.check_and_sync.assert_not_called()   # settled for free

    def test_sibling_with_higher_output_is_ignored(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        mine   = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa")
        theirs = make_block(1, g["hash"], [], builder_index=1, vdf_output="zz")
        node._commit(mine)
        node._handle_inbound_block(
            {"block": theirs, "sender": "1.2.3.4:1", "stemming": False}, [])
        assert node.cs.tip["hash"] == mine["hash"]

    def test_does_not_abandon_while_uncontested(self, node_env):
        """With no competitor in hand there is nothing to lose by finishing
        and everything to lose by stopping."""
        node, *_ = node_env
        node._own_build_seconds.extend([999.0] * 5)
        assert node._should_abandon(node.cs, [], time.monotonic()) is False

    def test_does_not_abandon_without_a_basis_to_predict(self, node_env):
        """A node that has never finished an evaluation can't estimate how
        long this one has left, so it doesn't guess. It finishes."""
        node, *_ = node_env
        g = node.cs.tip
        contender = make_block(1, g["hash"], [], builder_index=1)
        assert node._own_build_seconds == collections_deque_empty()
        assert node._should_abandon(node.cs, [contender], time.monotonic()) is False


def collections_deque_empty():
    import collections
    return collections.deque(maxlen=30)


# ---------------------------------------------------------------------------
# 19. Hints are evidence, not proof
# ---------------------------------------------------------------------------

class TestLyingPeers:
    def test_far_ahead_block_is_never_relayed(self, node_env):
        """We cannot validate a block whose parents we don't have, so we
        must not pass it on: relaying what we can't vouch for would let one
        crafted datagram spend the whole network's bandwidth."""
        node, _, __, gossip, *_ = node_env
        lie = make_block(999999, "00" * 32, [])
        node._handle_inbound_block(
            {"block": lie, "sender": "1.2.3.4:1", "stemming": False}, [])
        gossip.relay.assert_not_called()

    def test_far_ahead_block_only_decides_who_to_ask(self, node_env):
        node, *_ = node_env
        lie = make_block(999999, "00" * 32, [])
        node._handle_inbound_block(
            {"block": lie, "sender": "1.2.3.4:1", "stemming": False}, [])
        assert node._sync_hint == "1.2.3.4:1"

    def test_a_hint_that_leads_nowhere_costs_the_liar_a_strike(self, node_env):
        """Believing a hint is never what decides truth: the peer's chain
        still has to validate. One that doesn't is a peer worth trusting
        less, or an attacker gets a free sync attempt per crafted datagram."""
        node, *_, syncer, pool, net_q = node_env
        syncer.check_and_sync.return_value = False
        node._sync_hint = "1.2.3.4:1"
        node._sync_if_triggered()
        pool.strike.assert_called_once_with("1.2.3.4:1")

    def test_a_hint_that_pays_off_does_not_strike(self, node_env):
        node, *_, syncer, pool, net_q = node_env
        syncer.check_and_sync.return_value = True
        node._sync_hint = "1.2.3.4:1"
        node._sync_if_triggered()
        pool.strike.assert_not_called()


# ---------------------------------------------------------------------------
# 20. Privacy switch
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 21. Background probe: blocks arriving is not proof we're on the best chain
# ---------------------------------------------------------------------------

class TestBackgroundProbe:
    @pytest.fixture(autouse=True)
    def _probe_now(self, node_env, monkeypatch):
        node = node_env[0]
        monkeypatch.setattr(node, "_probe_spacing", lambda: 0.0)

    def test_healthy_stream_of_blocks_is_still_probed_around(self, node_env):
        """An eclipsed node, or one partitioned onto a consistent but
        inferior fork, sees blocks arriving normally and never goes silent,
        so neither the hint nor the silence trigger ever fires. This is the
        only trigger that looks outside whatever set is feeding us."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        node._last_block_seen = time.monotonic()      # not silent
        node._last_probe = time.monotonic() - 10_000

        node._sync_if_triggered()

        syncer.check_and_sync.assert_called_once()
        assert syncer.check_and_sync.call_args.kwargs["peer"] == "random.peer:1"

    def test_probe_picks_at_random_not_best_known(self, node_env):
        """Best-known is exactly who an eclipsing attacker would arrange for
        us to keep asking."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        pool.snapshot.return_value = [("attacker:1", 0, True, 10**9, "", "", None)]
        node._last_block_seen = time.monotonic()
        node._last_probe = time.monotonic() - 10_000

        node._sync_if_triggered()
        assert syncer.check_and_sync.call_args.kwargs["peer"] == "random.peer:1"

    def test_probe_does_not_strike_on_a_quiet_answer(self, node_env):
        """Only a hint we chose to act on is a bet that can be lost. A
        random probe finding nothing is the normal, expected outcome."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        syncer.check_and_sync.return_value = False
        node._last_block_seen = time.monotonic()
        node._last_probe = time.monotonic() - 10_000

        node._sync_if_triggered()
        pool.strike.assert_not_called()

    def test_a_sync_pass_is_bounded_so_the_loop_keeps_forwarding(self, node_env):
        """A long sync that ran to completion inline blocked the loop for
        minutes, and a node that isn't draining forwards nothing, under a
        stem that silently kills whatever hop was handed to it."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        node._last_probe = time.monotonic() - 10_000
        node._sync_if_triggered()
        assert syncer.check_and_sync.call_args.kwargs["max_pages"] == \
            node_mod.SYNC_PAGES_PER_PASS


# ---------------------------------------------------------------------------
# 22. The draw
# ---------------------------------------------------------------------------

class TestDraw:
    """Among candidates for one height the lowest vdf_output wins, so that
    the height goes to whoever's evaluation actually earned it rather than
    to whoever reached us first. Fork choice can't deliver that on its own:
    is_better_than compares output only between chains of equal work, so as
    soon as anyone builds on top of the first arrival, every sibling loses
    no matter its output. Hence an explicit window, during which we are
    building on the next height anyway, so it costs no head start."""

    def test_better_sibling_wins_while_the_draw_is_open(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        first  = make_block(1, g["hash"], [], builder_index=0, vdf_output="zz")
        better = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")

        node._commit(first)                      # adopting opens the draw
        node._handle_inbound_block(
            {"block": better, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == better["hash"]

    def test_worse_sibling_never_wins(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        first = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa")
        worse = make_block(1, g["hash"], [], builder_index=1, vdf_output="zz")

        node._commit(first)
        node._handle_inbound_block(
            {"block": worse, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == first["hash"]

    def test_a_sibling_arriving_after_the_draw_closes_does_not_win(self, node_env):
        """The window has to end, or a node would keep redoing next-height
        work indefinitely on every straggler."""
        node, *_ = node_env
        g = node.cs.tip
        first  = make_block(1, g["hash"], [], builder_index=0, vdf_output="zz")
        better = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")

        node._commit(first)
        node._draw_closes = time.monotonic() - 1     # window has run out
        node._handle_inbound_block(
            {"block": better, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == first["hash"]

    def test_window_length_is_adjustable(self, node_env, monkeypatch):
        import settings as settings_mod
        node, *_ = node_env
        monkeypatch.setenv("LAPSECOIN_DRAW_WINDOW_SECONDS", "0")
        g = node.cs.tip
        first  = make_block(1, g["hash"], [], builder_index=0, vdf_output="zz")
        better = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")

        node._commit(first)                      # zero-length window
        node._handle_inbound_block(
            {"block": better, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == first["hash"]

    def test_our_own_finished_candidate_leaves_the_draw_running(self, node_env, monkeypatch):
        """Finishing our own evaluation must not end the window early.
        Closing it here gave a node that finished its own block a
        zero-length window while a node that abandoned got a full one, two
        different rules for the same event, and the length of our own
        window ended up set by how long we took."""
        node, *_ = node_env

        def _evaluate(challenge, iterations, handle=None):
            return "aa" * 100, "bb" * 100, 0.01

        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", _evaluate)
        node._run_cycle()

        assert node.cs.height == 1
        assert node._draw_is_open(1)

    def test_the_window_is_anchored_to_the_first_candidate_not_our_commit(self, node_env):
        """Anchored to our own commit instead, a node that took 30s longer
        would collect for 30s longer, so the slower a node is the more time
        it gets to be beaten and the less a fast node's speed buys it."""
        node, *_ = node_env
        g = node.cs.tip
        first = make_block(1, g["hash"], [], builder_index=0, vdf_output="mm")

        node._consider_inbound_block(first, node.cs, [])
        anchored_at = node._draw_closes
        assert node._draw_height == 1

        node._commit(first)
        assert node._draw_closes == anchored_at

    def test_abandons_a_block_that_cannot_make_the_window(self, node_env):
        """A node running behind the leader would otherwise finish a block
        that missed every draw it could have entered, and start the next
        height late, every height, forever."""
        node, *_ = node_env
        node._own_build_seconds.extend([120.0] * 5)
        node._draw_height = node.cs.height + 1
        node._draw_closes = time.monotonic() + 15.0

        # ~120s of our own work left against 15s of window
        assert node._should_abandon(node.cs, [object()], time.monotonic()) is True

    def test_keeps_going_when_it_can_still_make_the_window(self, node_env):
        node, *_ = node_env
        node._own_build_seconds.extend([120.0] * 5)
        node._draw_height = node.cs.height + 1
        node._draw_closes = time.monotonic() + 15.0

        # 118s already spent, so ~2s left against 15s of window
        assert node._should_abandon(
            node.cs, [object()], time.monotonic() - 118.0) is False

    def test_abandons_once_the_window_is_gone_even_if_overdue(self, node_env):
        """An evaluation past its own estimate has a negative `remaining`,
        so comparing it against an equally negative `window_left` would say
        keep going on a height whose draw already closed."""
        node, *_ = node_env
        node._own_build_seconds.extend([120.0] * 5)
        node._draw_height = node.cs.height + 1
        node._draw_closes = time.monotonic() - 5.0      # closed 5s ago

        # 150s spent against a 120s estimate, so more overdue than the
        # window is
        assert node._should_abandon(
            node.cs, [object()], time.monotonic() - 150.0) is True

    def test_the_window_is_exactly_what_was_configured(self, node_env):
        """One number, the operator's. It used to widen itself from the gap
        between a height's first candidate and each later one, which is not
        propagation but how far apart the builders are in speed, so a node
        with a real lead had the window stretch to cover that lead and turn
        it into a coin flip."""
        import settings as settings_mod
        node, *_ = node_env

        assert node._draw_window_seconds() == node.settings.get(
            settings_mod.DRAW_WINDOW_SECONDS)

    def test_changing_the_setting_changes_the_window(self, node_env, monkeypatch):
        import settings as settings_mod
        node, *_ = node_env
        monkeypatch.setenv("LAPSECOIN_DRAW_WINDOW_SECONDS", "4")

        assert node._draw_window_seconds() == 4.0
        node.open_draw(1)
        # Not exact equality: _draw_closes is now + 4.0 and _draw_anchor is
        # that same now, both taken from time.monotonic(), and (x + 4.0) - x
        # is only guaranteed exactly 4.0 for some values of x, not all.
        # Whether it round-trips exactly depends on x's own bit pattern,
        # which here is however long this machine has been up. Passed
        # every local run and failed on a CI runner with a different
        # uptime for exactly that reason; the code is right, the exact
        # comparison wasn't.
        assert node._draw_closes - node._draw_anchor == pytest.approx(4.0)

    def test_a_slower_field_does_not_stretch_it(self, node_env):
        """The case that motivated removing the widening: competitors
        arriving late must not buy themselves a wider window, or the draw
        grows to absorb exactly the speed differences it exists to settle."""
        import settings as settings_mod
        node, *_ = node_env
        configured = node.settings.get(settings_mod.DRAW_WINDOW_SECONDS)
        g = node.cs.tip

        for i in range(6):
            node._consider_inbound_block(
                make_block(1, g["hash"], [], builder_index=i,
                           vdf_output=f"{i:02x}" * 100),
                node.cs, [])

        assert node._draw_window_seconds() == configured

    def test_a_new_contest_at_a_height_we_held_before_gets_a_window(self, node_env):
        """Nothing resets _draw_height when a window expires, so a height
        number we already used must not be able to suppress the window for
        a genuinely new contest at it after a reorg."""
        node, *_ = node_env
        node.open_draw(1)
        node._draw_closes = time.monotonic() - 1        # expired

        node.open_draw(1)                               # new contest, same height
        assert node._draw_is_open(1)


# ---------------------------------------------------------------------------
# 23. Hints can't be shouted down
# ---------------------------------------------------------------------------

class TestHintSelection:
    def test_the_strongest_claim_wins_not_the_latest(self, node_env):
        """A single last-writer-wins slot would let anyone bury a real hint
        under a stream of weaker ones."""
        node, *_ = node_env
        node._handle_inbound_block(
            {"block": make_block(900, "00" * 32, []), "sender": "real:1",
             "stemming": False}, [])
        node._handle_inbound_block(
            {"block": make_block(5, "00" * 32, []), "sender": "noise:1",
             "stemming": False}, [])

        assert node._sync_hint == "real:1"

    def test_a_stronger_later_claim_does_replace(self, node_env):
        node, *_ = node_env
        node._handle_inbound_block(
            {"block": make_block(5, "00" * 32, []), "sender": "low:1",
             "stemming": False}, [])
        node._handle_inbound_block(
            {"block": make_block(900, "00" * 32, []), "sender": "high:1",
             "stemming": False}, [])

        assert node._sync_hint == "high:1"


class TestProbeSpacing:
    def test_probe_does_not_fire_on_every_loop_tick(self, node_env, monkeypatch):
        """The wait loop asks on every tick; a probe is cheap but not free,
        and once per block is already as fine-grained as the thing it is
        watching for."""
        node, *_, syncer, pool, net_q = node_env
        pool.random.return_value = "random.peer:1"
        node._last_block_seen = time.monotonic()
        monkeypatch.setattr(node, "_probe_spacing", lambda: 60.0)
        node._last_probe = time.monotonic() - 10_000

        assert node._sync_if_triggered() is not None
        syncer.check_and_sync.reset_mock()
        node._sync_if_triggered()
        syncer.check_and_sync.assert_not_called()


# ---------------------------------------------------------------------------
# 24. Unvalidated input must not be able to spend our resources
# ---------------------------------------------------------------------------

class TestUnvalidatedInputIsInert:
    def test_garbage_at_our_height_plus_one_does_not_contest_the_height(self, node_env):
        """_should_abandon reads the candidate list to decide whether a
        height is contested, and a contested height can cancel an evaluation
        that is most of the way done. If unvalidated blocks reached that
        list, one crafted datagram would throw away ~120s of real work."""
        node, *_ = node_env
        node._own_build_seconds.extend([999.0] * 5)
        g = node.cs.tip
        junk = make_block(1, g["hash"], [])
        junk["vdf_iterations"] = 1          # fails validation

        accumulated = []
        node._consider_inbound_block(junk, node.cs, accumulated)

        assert accumulated == []
        assert node._should_abandon(node.cs, accumulated, time.monotonic()) is False

    def test_garbage_does_not_hold_the_silence_trigger_open(self, node_env):
        """Otherwise an eclipsing peer keeps us quiet for the price of one
        datagram: we never notice silence, because junk keeps arriving."""
        node, *_ = node_env
        node._last_block_seen = 0.0
        junk = make_block(1, node.cs.tip["hash"], [])
        junk["vdf_iterations"] = 1

        node._handle_inbound_block(
            {"block": junk, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node._last_block_seen == 0.0

    def test_a_worse_sibling_costs_no_chain_work(self, node_env, monkeypatch):
        """A sibling that can't win the draw is settled by a string compare.
        Without that, anyone could make us re-derive chain state and verify
        a VDF proof once per datagram."""
        node, *_ = node_env
        g = node.cs.tip
        mine  = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa")
        worse = make_block(1, g["hash"], [], builder_index=1, vdf_output="zz")
        node._commit(mine)

        called = []
        monkeypatch.setattr(node, "apply_better_chain",
                            lambda chain: called.append(chain) or (False, None))
        node._reorg_to_sibling(worse, node.cs)

        assert called == []


class TestEchoCannotBeFaked:
    def test_the_peer_we_stemmed_to_cannot_confirm_delivery(self, node_env):
        """That peer already has the item by construction, so it can drop it
        and hand it straight back. Counting that as delivery means one
        datagram from the single node we chose silently loses the block."""
        node, _, __, gossip, *_ = node_env
        gossip.spread.return_value = "stem.target:1"
        blk = make_block(1, node.cs.tip["hash"], [])

        node._spread(blk, "block", blk["hash"])
        node._note_echo(blk["hash"], sender="stem.target:1")

        assert blk["hash"] in node._unconfirmed_spreads   # still unconfirmed

    def test_an_echo_from_anyone_else_does_confirm(self, node_env):
        node, _, __, gossip, *_ = node_env
        gossip.spread.return_value = "stem.target:1"
        blk = make_block(1, node.cs.tip["hash"], [])

        node._spread(blk, "block", blk["hash"])
        node._note_echo(blk["hash"], sender="someone.else:1")

        assert blk["hash"] not in node._unconfirmed_spreads


class TestJudgementCache:
    def test_a_flood_of_junk_cannot_switch_the_cache_off(self, node_env):
        """A cache that stopped accepting entries once full could be
        disabled by anyone willing to send enough junk, which is exactly
        the traffic it exists to absorb. An LRU ages the junk out instead."""
        node, *_ = node_env
        g = node.cs.tip
        for i in range(node_mod.JUDGED_CACHE_SIZE + 50):
            node._remember_judgement({"hash": f"junk{i}"}, node.cs, False)

        real = make_block(1, g["hash"], [])
        node._remember_judgement(real, node.cs, True)
        assert node._judged(real, node.cs) is True

    def test_a_verdict_does_not_survive_the_tip_moving(self, node_env):
        """Validity is relative to the tip, so a verdict from before a
        reorg says nothing about after it."""
        node, *_ = node_env
        g = node.cs.tip
        blk = make_block(1, g["hash"], [])
        node._remember_judgement(blk, node.cs, True)
        node._commit(blk)
        assert node._judged(blk, node.cs) is None


# ---------------------------------------------------------------------------
# 26. What the node says it is doing
# ---------------------------------------------------------------------------

def _make_silent(node):
    """Put the node past its silence threshold, relative to the clock.

    Setting the timestamps to 0.0 instead reads as "long ago" only if
    time.monotonic() is already larger than the threshold, and on Linux
    that clock counts from boot: it says 30,000 on a workstation and 45 on
    a CI runner that just started. So the same test passed on one machine
    and failed on the other, which is the kind of green that is worse than
    a red one.
    """
    now = time.monotonic()
    stale = now - node._silence_threshold() - 1
    node._last_block_seen = stale
    node._last_silence_poll = stale


class TestStatusLine:
    def test_syncing_says_so_instead_of_still_claiming_vdf(self, node_env):
        """A sync runs for many seconds on the loop thread. While it did not
        touch status_line, a node catching up and a node stuck looked
        identical from outside, which is exactly what an operator checks."""
        node, *_, syncer, pool, _q = node_env
        pool.random.return_value = "1.2.3.4:8333"
        _make_silent(node)
        node.status_line = "computing VDF for block 5"

        seen = []
        syncer.check_and_sync.side_effect = (
            lambda *a, **kw: seen.append(node.status_line) or False)

        node._sync_if_triggered()
        assert seen and "1.2.3.4:8333" in seen[0]

    def test_progress_reports_how_far_along_it_is(self, node_env):
        node, *_ = node_env
        node._note_sync_progress(1200, 8739)
        assert "1,200" in node.status_line
        assert "8,739" in node.status_line

    def test_a_check_that_finds_nothing_restores_what_it_interrupted(self, node_env):
        """Otherwise a routine background probe leaves a finished check on
        screen in place of what the node is actually doing."""
        node, *_, syncer, pool, _q = node_env
        pool.random.return_value = "1.2.3.4:8333"
        _make_silent(node)
        syncer.check_and_sync.return_value = False
        node.status_line = "computing VDF for block 5"

        node._sync_if_triggered()
        assert node.status_line == "computing VDF for block 5"


# ---------------------------------------------------------------------------
# 27. Reorg depth, as a number an operator can act on
# ---------------------------------------------------------------------------

class TestReorgStats:
    def test_nothing_recorded_on_a_fresh_node(self, node_env):
        node, *_ = node_env
        assert node.reorg_stats() == {"deepest": 0, "count": 0, "deepest_at": 0}

    def test_a_one_block_swap_is_not_counted(self, node_env):
        """That is the draw settling a tie. It happens constantly and by
        design, and counting it would bury the rare event this exists to
        surface under noise from the common one."""
        node, *_ = node_env
        node._record_reorg(1)
        assert node.reorg_stats()["count"] == 0
        assert node.reorg_stats()["deepest"] == 0

    def test_a_deeper_one_is_counted(self, node_env):
        node, *_ = node_env
        node._record_reorg(5)
        stats = node.reorg_stats()
        assert stats == {"deepest": 5, "count": 1, "deepest_at": stats["deepest_at"]}
        assert stats["deepest_at"] > 0

    def test_it_keeps_the_deepest_not_the_latest(self, node_env):
        node, *_ = node_env
        node._record_reorg(9)
        node._record_reorg(2)
        assert node.reorg_stats() == {"deepest": 9, "count": 2,
                                      "deepest_at": node.reorg_stats()["deepest_at"]}

    def test_it_survives_a_restart(self, node_env):
        """A deep reorg may happen once in a node's life, so a counter that
        forgets it on restart is not worth reading."""
        node, keyfile, *_ = node_env
        node._record_reorg(7)
        reopened = node_mod.Node(
            keyfile=keyfile, public_key=node.pk, gossip=node.gossip,
            syncer=node.syncer, pool=node.pool, net_in_q=node.net_in_q,
            db_path=node.storage.path)
        assert reopened.reorg_stats()["deepest"] == 7
        assert reopened.reorg_stats()["count"] == 1

    def test_a_real_sibling_swap_through_the_draw_records_nothing(self, node_env):
        """End to end rather than by calling the recorder directly: the draw
        path must not register, and it is the reason the filter is on depth
        rather than on which caller it came from."""
        node, *_ = node_env
        g = node.cs.tip
        first  = make_block(1, g["hash"], [], builder_index=0, vdf_output="zz")
        better = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")

        node._commit(first)
        node._handle_inbound_block(
            {"block": better, "sender": "1.2.3.4:1", "stemming": False}, [])

        assert node.cs.tip["hash"] == better["hash"]   # the draw did fire
        assert node.reorg_stats()["count"] == 0        # and was not counted


class TestTheFluffingNodeKeepsTheTransaction:
    """A stemming transaction is relayed, not admitted, while it is still
    private. But the stem rule can decide to end the walk here and
    broadcast it to everyone, and at that point it is public and there is
    nothing left to hide by not keeping it.

    This node used to broadcast it and keep nothing, so the one node that
    put a transaction in front of the whole network was the one node that
    could not then mine it. A simulation over the real Gossip class put
    the cost at roughly a fifth of nodes missing the transaction on a
    20-node graph."""

    def test_a_stem_hop_that_fluffs_admits_it(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        gossip.relay.return_value = True          # the walk ended here
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        assert node.mempool.size() == 1

    def test_a_stem_hop_that_forwards_does_not(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        gossip.relay.return_value = False         # still private, still moving
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        assert node.mempool.size() == 0, \
            "a transaction still in its private phase must not show up in our mempool"

    def test_it_is_still_relayed_either_way(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        for went_public in (True, False):
            gossip.relay.reset_mock()
            gossip.relay.return_value = went_public
            node.mempool.remove_many(list(node.mempool.pending_hashes()))
            node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
            gossip.relay.assert_called_once()
            assert gossip.relay.call_args.kwargs["stemming"] is True


class TestOurOwnTipIsStillRelayed:
    """Blocks ride the same Dandelion stem/fluff as transactions (see
    gossip.py), so they had the same defect at the originator, and it bites
    harder here.

    The builder stems its candidate to one peer and commits it moments
    later. When the walk fluffs and a copy comes back, its height is no
    longer tip+1, it IS our tip, and that branch used to relay only a
    *sibling*. So the one node that produced the block was the one node
    that never put it on the public wire, and any peer reachable only
    through the builder never heard of it. Simulated over the real Gossip
    objects, a builder at the centre of a 5-node star reached everyone 17%
    of the time; on a 50-node random graph, 47%."""

    def _inbound(self, node, blk, sender="1.2.3.4:1", stemming=False):
        node._handle_inbound_block(
            {"block": blk, "sender": sender, "stemming": stemming}, [])

    def test_a_copy_of_our_own_tip_is_passed_on(self, node_env):
        node, _, __, gossip, *_ = node_env
        tip = node.cs.tip
        gossip.relay.reset_mock()
        self._inbound(node, tip)
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.args[1] == "block"

    def test_it_is_passed_on_in_the_phase_it_arrived_in(self, node_env):
        node, _, __, gossip, *_ = node_env
        for stemming in (True, False):
            gossip.relay.reset_mock()
            self._inbound(node, node.cs.tip, stemming=stemming)
            assert gossip.relay.call_args.kwargs["stemming"] is stemming

    def test_the_chain_is_not_disturbed_by_it(self, node_env):
        # Relaying our own tip must not look like a reorg or move anything.
        node, _, __, gossip, *_ = node_env
        before_hash, before_height = node.cs.tip["hash"], node.cs.height
        self._inbound(node, node.cs.tip)
        assert node.cs.tip["hash"] == before_hash
        assert node.cs.height == before_height

    def test_an_unrelated_block_at_our_height_is_not_relayed_blindly(self, node_env):
        # Only our own tip takes the new path; anything else at this height
        # is a sibling and still has to win its draw first.
        node, _, __, gossip, *_ = node_env
        stranger = dict(node.cs.tip, hash="ff" * 32)
        gossip.relay.reset_mock()
        self._inbound(node, stranger)
        gossip.relay.assert_not_called()


class TestBuildTimeEstimate:
    """A node slower than the field never finishes an evaluation: its tip
    moves first, the cycle cancels, nothing is recorded. own_vdf_median
    stayed None for the life of the process however long it ran, which
    emptied the odds page and, worse, made _should_abandon decline to
    abandon, so the node that most needed to stop early never did."""

    def test_the_estimate_fills_in_before_any_real_build(self, node_env):
        node, *_ = node_env
        assert node.own_vdf_median() is None      # nothing measured, nothing calibrated
        node._vdf_seconds_per_iteration = 1e-5
        expected = 1e-5 * block_mod.get_vdf_iterations(node.view.chain)
        assert node.own_vdf_median() == pytest.approx(expected)
        assert node.own_vdf_is_estimate()

    def test_a_real_build_supersedes_the_estimate(self, node_env):
        node, *_ = node_env
        node._vdf_seconds_per_iteration = 1e-5
        node._own_build_seconds.append(42.0)
        assert node.own_vdf_median() == 42.0
        assert not node.own_vdf_is_estimate()

    def test_the_estimate_scales_with_the_required_iterations(self, node_env):
        # It is a per-iteration rate, so a chain that has retargeted upward
        # gets a correspondingly longer estimate.
        node, *_ = node_env
        node._vdf_seconds_per_iteration = 1e-5
        base = node.own_vdf_median()
        import block as blk_mod
        original = blk_mod.get_vdf_iterations
        try:
            blk_mod.get_vdf_iterations = lambda chain: original(chain) * 2
            assert node.own_vdf_median() == pytest.approx(base * 2)
        finally:
            blk_mod.get_vdf_iterations = original

    def test_the_rate_survives_a_restart(self, node_env):
        node, *_ = node_env
        node._vdf_seconds_per_iteration = 2.5e-6
        node.storage.set_meta(node._VDF_RATE_META_KEY, repr(2.5e-6))
        assert node._load_vdf_rate() == pytest.approx(2.5e-6)

    def test_an_unreadable_stored_rate_is_ignored(self, node_env):
        node, *_ = node_env
        node.storage.set_meta(node._VDF_RATE_META_KEY, "not a number")
        assert node._load_vdf_rate() is None

    def test_a_slow_node_can_now_abandon(self, node_env):
        # The point of the estimate: with no median at all _should_abandon
        # returned False every time, so the slowest node on the network was
        # the only one that never gave up on a hopeless height.
        node, *_ = node_env
        node._vdf_seconds_per_iteration = 1e-3        # very slow machine
        node._draw_height = node.cs.height + 1
        node._draw_closes = time.monotonic() + 0.5    # window nearly shut
        assert node._should_abandon(node.cs, [{"any": "candidate"}],
                                    time.monotonic()) is True


class TestLivenessNotes:
    """A node says "I am active, pay me here" and the note travels the
    network like any other item. The address is therefore never tied to
    the IP it came from, which is what the advertised-wallet mechanism
    could not avoid doing."""

    def test_announcing_records_ourselves_and_spreads(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.announce_alive()
        assert node.addr in node.active_addresses(3600)
        gossip.spread.assert_called_once()
        assert gossip.spread.call_args.args[1] == "alive"

    def test_a_received_note_is_recorded_and_passed_on(self, node_env):
        node, _, __, gossip, *_ = node_env
        other = address(3)
        node._handle_inbound_alive({"note": {"address": other},
                                    "sender": "1.2.3.4:1", "stemming": False})
        assert other in node.active_addresses(3600)
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.args[1] == "alive"

    def test_a_malformed_address_is_ignored_entirely(self, node_env):
        node, _, __, gossip, *_ = node_env
        node._handle_inbound_alive({"note": {"address": "not an address"},
                                    "sender": "1.2.3.4:1", "stemming": False})
        assert node.active_addresses(3600) == set()
        gossip.relay.assert_not_called()

    def test_a_missing_address_is_ignored(self, node_env):
        node, _, __, gossip, *_ = node_env
        node._handle_inbound_alive({"note": {}, "sender": "1.2.3.4:1",
                                    "stemming": False})
        gossip.relay.assert_not_called()

    def test_notes_age_out_of_the_window(self, node_env):
        node, *_ = node_env
        other = address(3)
        node._record_alive(other)
        node._alive_seen[other] = time.time() - 7200      # two hours ago
        assert other not in node.active_addresses(3600)
        assert other in node.active_addresses(10800)

    def test_what_is_tracked_is_bounded(self, node_env):
        node, *_ = node_env
        import node as node_mod
        original = node_mod.ALIVE_MAX_TRACKED
        try:
            node_mod.ALIVE_MAX_TRACKED = 10
            for i in range(50):
                node._record_alive(f"addr{i}")
            assert len(node._alive_seen) <= 10
        finally:
            node_mod.ALIVE_MAX_TRACKED = original

    def test_the_same_address_announcing_again_just_refreshes_it(self, node_env):
        node, *_ = node_env
        other = address(3)
        node._record_alive(other)
        node._alive_seen[other] = time.time() - 1000
        node._record_alive(other)
        assert len(node._alive_seen) == 1
        assert other in node.active_addresses(60)

    def test_a_note_routes_through_the_queue_like_any_other_message(self, node_env):
        node, _, __, gossip, *_ = node_env
        other = address(4)
        node._handle({"type": "alive", "note": {"address": other},
                      "sender": "1.2.3.4:1", "stemming": False}, [])
        assert other in node.active_addresses(3600)
