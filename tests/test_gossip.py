"""
Unit tests for gossip.py (UDP transport edition)

Covers the one stem/fluff mechanism both blocks and txs go through:
mark_seen dedup, the stem rule (forward to a non-predecessor peer, fluff
when there isn't one), and that fluff floods every peer but the sender,
once per item hash.

UDP calls are mocked via the udp object. No network.
"""

import os
import sys
from unittest.mock import MagicMock, patch
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gossip as gossip_mod
from gossip import Gossip
import state as state_mod
import tx as tx_mod
from tests.fixtures import make_tx, seed_balance
from params import TICKS_PER_LAPSE


def make_gossip(peers=None, peer_count=None):
    pool = MagicMock()
    pool.get_all.return_value = peers or []
    pool.random.return_value = peers[0] if peers else None
    # No peer-count threshold exists any more: the stem rule is the same at
    # every scale and ends itself when the graph runs out of peers.
    pool.count.return_value = peer_count if peer_count is not None else 100
    udp = MagicMock()
    gossip = Gossip(pool=pool, udp=udp)
    return gossip, pool, udp


def sample_tx():
    s = state_mod.State()
    seed_balance(s, 0, 100.0)
    return make_tx(0, 1, TICKS_PER_LAPSE, s)


# ---------------------------------------------------------------------------
# 1. mark_seen
# ---------------------------------------------------------------------------

class TestMarkSeen:
    def test_first_time_returns_false(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("abc123") is False

    def test_second_time_returns_true(self):
        g, _, _ = make_gossip()
        g.mark_seen("abc123")
        assert g.mark_seen("abc123") is True

    def test_different_hashes_each_new(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("hash1") is False
        assert g.mark_seen("hash2") is False
        assert g.mark_seen("hash1") is True

    def test_mark_seen_thread_safe(self):
        g, _, _ = make_gossip()
        errors = []

        def worker(i):
            try:
                g.mark_seen(f"hash_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ---------------------------------------------------------------------------
# 2. The stem rule
# ---------------------------------------------------------------------------

def always_stem(monkeypatch):
    monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 0.0)


def always_fluff(monkeypatch):
    monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)


class TestStemRule:
    def test_stem_goes_to_exactly_one_peer(self, monkeypatch):
        always_stem(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000", "5.6.7.8:9000"])
        g.spread(sample_tx(), gossip_mod.KIND_TX, 'h1')
        udp.send_tx.assert_called_once()
        assert len(udp.send_tx.call_args.kwargs["peers"]) == 1
        assert udp.send_tx.call_args.kwargs["stemming"] is True

    def test_stem_prefers_a_peer_that_is_not_the_predecessor(self, monkeypatch):
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred, "5.6.7.8:9000"])
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), pred, stemming=True)
        assert udp.send_tx.call_args.kwargs["peers"] == ["5.6.7.8:9000"]

    def test_dead_end_stops_here_rather_than_handing_back(self, monkeypatch):
        """The predecessor is our only peer, so there is genuinely nowhere
        for the item to go: it already came from the one node we could send
        it to. The walk ends here.

        This is the case that makes the originator's rework mandatory
        rather than decorative, the sender handed off and has no way to
        know it landed on a leaf. See Node._retry_unconfirmed_spreads."""
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred])
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), pred, stemming=True)
        udp.send_tx.assert_not_called()

    def test_dead_end_with_another_peer_present_goes_public(self, monkeypatch):
        """Same rule, but here the node has somewhere to put it: the stem
        can't continue without handing back, so it fluffs to the peers that
        haven't seen it."""
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred])
        g.pool.get_all.side_effect = [[pred], [pred, "5.6.7.8:9000"]]
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), pred, stemming=True)
        udp.send_tx.assert_called_once()
        assert udp.send_tx.call_args.kwargs["stemming"] is False
        assert udp.send_tx.call_args.kwargs["peers"] == ["5.6.7.8:9000"]

    def test_single_peer_node_still_propagates(self, monkeypatch):
        """A node with one peer has no anonymity to protect, and must still
        get its own item out."""
        always_stem(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000"])
        g.spread(sample_tx(), gossip_mod.KIND_TX, 'h1')
        udp.send_tx.assert_called_once()

    def test_no_peers_at_all_sends_nothing(self, monkeypatch):
        always_stem(monkeypatch)
        g, _, udp = make_gossip(peers=[])
        g.spread(sample_tx(), gossip_mod.KIND_TX, 'h1')
        udp.send_tx.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Fluff
# ---------------------------------------------------------------------------

class TestFluff:
    def test_fluff_floods_every_peer_except_the_sender(self, monkeypatch):
        always_fluff(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred, "5.6.7.8:9000", "9.9.9.9:9000"])
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), pred, stemming=False)
        peers = udp.send_tx.call_args.kwargs["peers"]
        assert pred not in peers
        assert len(peers) == 2
        assert udp.send_tx.call_args.kwargs["stemming"] is False

    def test_each_item_is_fluffed_at_most_once(self, monkeypatch):
        always_fluff(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        g.relay(t, gossip_mod.KIND_TX, tx_mod.tx_hash(t), None, stemming=False)
        g.relay(t, gossip_mod.KIND_TX, tx_mod.tx_hash(t), None, stemming=False)
        assert udp.send_tx.call_count == 1

    def test_different_items_both_fluffed(self, monkeypatch):
        always_fluff(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000"])
        s = state_mod.State()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g.relay(t1, gossip_mod.KIND_TX, tx_mod.tx_hash(t1), None, stemming=False)
        g.relay(t2, gossip_mod.KIND_TX, tx_mod.tx_hash(t2), None, stemming=False)
        assert udp.send_tx.call_count == 2

    def test_a_public_item_is_never_re_stemmed(self, monkeypatch):
        """Privacy is already spent once an item is public; re-stemming it
        would only slow it down."""
        always_stem(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000", "5.6.7.8:9000"])
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), None, stemming=False)
        assert udp.send_tx.call_args.kwargs["stemming"] is False


# ---------------------------------------------------------------------------
# 4. Blocks take the same path as txs
# ---------------------------------------------------------------------------

class TestBlocksUseTheSameMechanism:
    def test_own_block_enters_the_stem(self, monkeypatch):
        always_stem(monkeypatch)
        g, _, udp = make_gossip(peers=["1.2.3.4:9000", "5.6.7.8:9000"])
        g.spread({"height": 1, "hash": "aa" * 32}, gossip_mod.KIND_BLOCK, "aa" * 32)
        udp.send_block.assert_called_once()
        assert len(udp.send_block.call_args.kwargs["peers"]) == 1
        assert udp.send_block.call_args.kwargs["stemming"] is True

    def test_block_dead_end_stops_here(self, monkeypatch):
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred])
        g.relay({"height": 1, "hash": "aa" * 32}, gossip_mod.KIND_BLOCK, 'aa' * 32, pred, stemming=True)
        udp.send_block.assert_not_called()

    def test_block_fluff_excludes_sender_and_dedups(self, monkeypatch):
        always_fluff(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred, "5.6.7.8:9000"])
        blk = {"height": 1, "hash": "aa" * 32}
        g.relay(blk, gossip_mod.KIND_BLOCK, 'aa' * 32, pred, stemming=False)
        g.relay(blk, gossip_mod.KIND_BLOCK, 'aa' * 32, pred, stemming=False)
        assert udp.send_block.call_count == 1
        assert udp.send_block.call_args.kwargs["peers"] == ["5.6.7.8:9000"]


class TestRelayReportsWhetherItWentPublic:
    """A stemming item is deliberately not admitted locally while it is
    still private, so the caller has to be able to tell the difference
    between 'stemmed onward' and 'fluffed here'."""

    def test_a_stem_hop_that_forwards_reports_false(self, monkeypatch):
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2", "c:3"]
        g = gossip_mod.Gossip(pool, MagicMock())
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 0.0)  # always stem
        assert g.relay({"x": 1}, gossip_mod.KIND_TX, "h1", "a:1", stemming=True) is False

    def test_a_stem_hop_that_fluffs_reports_true(self, monkeypatch):
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2", "c:3"]
        g = gossip_mod.Gossip(pool, MagicMock())
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)  # always fluff
        assert g.relay({"x": 1}, gossip_mod.KIND_TX, "h2", "a:1", stemming=True) is True

    def test_a_dead_end_fluffs_and_reports_true(self, monkeypatch):
        # Only peer is the predecessor, so the walk ends here whatever the
        # coin flip says.
        pool = MagicMock()
        pool.get_all.return_value = ["a:1"]
        g = gossip_mod.Gossip(pool, MagicMock())
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 0.0)
        assert g.relay({"x": 1}, gossip_mod.KIND_TX, "h3", "a:1", stemming=True) is True

    def test_a_public_relay_reports_true(self):
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2"]
        g = gossip_mod.Gossip(pool, MagicMock())
        assert g.relay({"x": 1}, gossip_mod.KIND_TX, "h4", "a:1", stemming=False) is True
