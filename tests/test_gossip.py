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

    def test_dead_end_hands_back_publicly_rather_than_dying(self, monkeypatch):
        """The predecessor is our only peer, so the stem cannot continue.
        The walk ends here, and the item goes public back down the one link
        we have.

        Handing it back is not the redundant send it looks like. A stem hop
        relays without admitting, so the predecessor is the one node we can
        be certain does not hold this; returning it publicly is what makes
        it real for them and lets it carry on past them. This used to send
        nothing at all, which killed the item on every walk that reached a
        leaf and left the originator's rework to notice, seconds later,
        that nothing had come back."""
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred])
        g.relay(sample_tx(), gossip_mod.KIND_TX, tx_mod.tx_hash(sample_tx()), pred, stemming=True)
        udp.send_tx.assert_called_once()
        assert udp.send_tx.call_args.kwargs["stemming"] is False
        assert udp.send_tx.call_args.kwargs["peers"] == [pred]

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
        # The predecessor is included: it relayed this without admitting it,
        # so it is the one peer here that does not have it.
        assert udp.send_tx.call_args.kwargs["peers"] == [pred, "5.6.7.8:9000"]

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

    def test_block_dead_end_hands_back_publicly(self, monkeypatch):
        """Same rule as a transaction's, and it matters more here: a block
        that dies at a leaf is an evaluation somebody paid ~120s for."""
        always_stem(monkeypatch)
        pred = "1.2.3.4:9000"
        g, _, udp = make_gossip(peers=[pred])
        g.relay({"height": 1, "hash": "aa" * 32}, gossip_mod.KIND_BLOCK, 'aa' * 32, pred, stemming=True)
        udp.send_block.assert_called_once()
        assert udp.send_block.call_args.kwargs["stemming"] is False
        assert udp.send_block.call_args.kwargs["peers"] == [pred]

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


class TestFluffReachesEveryConnectedNode:
    """Propagation over real topologies, driving the real Gossip objects.

    The requirement is coverage, not best effort: if the graph is
    connected, every node ends up with the item. Two separate defects used
    to break that, both from treating "sent it to me" as "already has it".
    """

    def _network(self, adj):
        sent = []
        nodes = {}

        def udp_for(me):
            class U:
                def send_tx(self, tx, peers, stemming):
                    for p in peers:
                        sent.append((p, me, tx, stemming))
                def send_block(self, *a, **k): pass
            return U()

        class Pool:
            def __init__(self, peers): self._p = peers
            def get_all(self): return list(self._p)

        for n, peers in adj.items():
            nodes[n] = gossip_mod.Gossip(Pool(peers), udp_for(n))
        return nodes, sent

    def _propagate(self, adj, origin):
        """Returns the set of nodes that ended up holding the item."""
        nodes, queue = self._network(adj)
        held = {origin}
        tx = {"h": "tx"}
        nodes[origin].spread(tx, gossip_mod.KIND_TX, "tx")
        steps = 0
        while queue and steps < 10000:
            me, sender, item, stemming = queue.pop(0)
            steps += 1
            if stemming:
                # mirrors Node._handle_inbound_tx: relayed, and admitted
                # only once the walk ends here and goes public
                if nodes[me].relay(item, gossip_mod.KIND_TX, "tx", sender,
                                   stemming=True):
                    held.add(me)
            else:
                held.add(me)
                nodes[me].relay(item, gossip_mod.KIND_TX, "tx", sender,
                                stemming=False)
        return held

    def _ring(self, n):  return {i: [(i - 1) % n, (i + 1) % n] for i in range(n)}
    def _line(self, n):  return {i: [j for j in (i-1, i+1) if 0 <= j < n] for i in range(n)}
    def _star(self, n):  return {0: list(range(1, n)), **{i: [0] for i in range(1, n)}}

    def test_a_ring_is_fully_covered_once_anything_fluffs(self, monkeypatch):
        # Always fluff at the first hop, so the flood is what is under test
        # rather than how long the stem happened to run.
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = self._ring(6)
        for origin in adj:
            assert self._propagate(adj, origin) == set(adj), \
                f"ring left nodes uncovered starting from {origin}"

    def test_a_line_is_fully_covered(self, monkeypatch):
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = self._line(8)
        for origin in adj:
            assert self._propagate(adj, origin) == set(adj)

    def test_a_star_is_fully_covered_from_a_leaf(self, monkeypatch):
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = self._star(6)
        assert self._propagate(adj, 3) == set(adj)

    def test_a_single_bridge_is_crossed(self, monkeypatch):
        # Two cliques joined by one edge: the flood has to traverse it.
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = {0: [1, 2, 3], 1: [0, 2], 2: [0, 1], 3: [0, 4, 5], 4: [3, 5], 5: [3, 4]}
        for origin in adj:
            assert self._propagate(adj, origin) == set(adj)

    def test_the_predecessor_of_a_fluffing_stem_hop_is_included(self, monkeypatch):
        # It is the one peer that provably does not have the item: a stem
        # hop relays without admitting.
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2", "c:3"]
        udp = MagicMock()
        g = gossip_mod.Gossip(pool, udp)
        g.relay({"x": 1}, gossip_mod.KIND_TX, "h", "a:1", stemming=True)
        peers = udp.send_tx.call_args.kwargs["peers"]
        assert "a:1" in peers, "the stem predecessor was excluded from the fluff"

    def test_a_public_relay_still_excludes_its_sender(self):
        # There, excluding is right: they demonstrably have it.
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2", "c:3"]
        udp = MagicMock()
        g = gossip_mod.Gossip(pool, udp)
        g.relay({"x": 1}, gossip_mod.KIND_TX, "h", "a:1", stemming=False)
        assert "a:1" not in udp.send_tx.call_args.kwargs["peers"]

    def test_a_node_floods_an_item_only_once(self):
        pool = MagicMock()
        pool.get_all.return_value = ["a:1", "b:2"]
        udp = MagicMock()
        g = gossip_mod.Gossip(pool, udp)
        for _ in range(5):
            g.relay({"x": 1}, gossip_mod.KIND_TX, "h", "a:1", stemming=False)
        assert udp.send_tx.call_count == 1, "flooding must be idempotent per item"
