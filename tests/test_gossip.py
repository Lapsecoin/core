"""
Unit tests for gossip.py (UDP transport edition)

Covers the one stem/fluff mechanism both blocks and txs go through:
mark_seen dedup, the stem rule (forward to a non-predecessor peer, fluff
when there isn't one), and that fluff floods every peer but the sender,
once per item hash.

UDP calls are mocked via the udp object. No network.
"""

import os
import random
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
        assert g.mark_seen("abc123", gossip_mod.KIND_TX) is False

    def test_second_time_returns_true(self):
        g, _, _ = make_gossip()
        g.mark_seen("abc123", gossip_mod.KIND_TX)
        assert g.mark_seen("abc123", gossip_mod.KIND_TX) is True

    def test_different_hashes_each_new(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("hash1", gossip_mod.KIND_TX) is False
        assert g.mark_seen("hash2", gossip_mod.KIND_TX) is False
        assert g.mark_seen("hash1", gossip_mod.KIND_TX) is True

    def test_mark_seen_thread_safe(self):
        g, _, _ = make_gossip()
        errors = []

        def worker(i):
            try:
                g.mark_seen(f"hash_{i}", gossip_mod.KIND_TX)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

    def test_kinds_do_not_share_a_budget(self):
        """The whole point of the split: filling one kind's cache to its
        ceiling must not evict, or even touch, another kind's entries."""
        g, _, _ = make_gossip()
        g.mark_seen("shared-hash", gossip_mod.KIND_BLOCK)
        for i in range(gossip_mod.ORDER_SEEN_CACHE_SIZE + 100):
            g.mark_seen(f"order-{i}", gossip_mod.KIND_ORDER)
        assert g.mark_seen("shared-hash", gossip_mod.KIND_BLOCK) is True, \
            "a block hash must survive an order flood"

    def test_same_hash_different_kinds_are_independent(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("h", gossip_mod.KIND_TX) is False
        assert g.mark_seen("h", gossip_mod.KIND_ORDER) is False
        assert g.mark_seen("h", gossip_mod.KIND_TX) is True
        assert g.mark_seen("h", gossip_mod.KIND_BLOCK) is False


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
# Network simulation: real Gossip objects wired together, no mocks on the
# propagation path itself. Shared by the topology-coverage tests below and
# by the scale/flood harness further down, so there is exactly one place
# that decides what "deliver everything that was sent" means.
# ---------------------------------------------------------------------------

def _build_network(adj):
    """One real Gossip per simulated node, wired to fake pool/udp objects
    that record what would have gone out instead of transmitting it.

    Returns (nodes, outboxes), where outboxes has one queue per item kind,
    mirroring the three distinct send_* methods the real transport exposes
    (peer_udp.UDPTransport.send_tx/send_block/send_order): nothing here
    should be able to confuse one kind's traffic for another's.
    """
    outboxes = {gossip_mod.KIND_TX: [], gossip_mod.KIND_BLOCK: [],
                gossip_mod.KIND_ORDER: [], gossip_mod.KIND_CLAIM: []}
    nodes = {}

    def udp_for(me):
        class U:
            def send_tx(self, item, peers, stemming):
                for p in peers:
                    outboxes[gossip_mod.KIND_TX].append((p, me, item, stemming))
            def send_block(self, item, peers, stemming):
                for p in peers:
                    outboxes[gossip_mod.KIND_BLOCK].append((p, me, item, stemming))
            def send_order(self, item, peers, stemming):
                for p in peers:
                    outboxes[gossip_mod.KIND_ORDER].append((p, me, item, stemming))
            def send_claim(self, item, peers, stemming):
                for p in peers:
                    outboxes[gossip_mod.KIND_CLAIM].append((p, me, item, stemming))
        return U()

    class Pool:
        def __init__(self, peers): self._p = peers
        def get_all(self): return list(self._p)

    for n, peers in adj.items():
        nodes[n] = gossip_mod.Gossip(Pool(peers), udp_for(n))
    return nodes, outboxes


def _drain(nodes, outbox, kind, item_hash, held, max_steps=500_000):
    """Deliver every queued message of one kind, mirroring how Node treats
    a stemming item (relayed, admitted only once it goes public here) vs.
    a public one (admitted and relayed on), adding arrivals to `held`.
    """
    steps = 0
    while outbox and steps < max_steps:
        peer, sender, item, stemming = outbox.pop(0)
        steps += 1
        if stemming:
            if nodes[peer].relay(item, kind, item_hash, sender, stemming=True):
                held.add(peer)
        else:
            held.add(peer)
            nodes[peer].relay(item, kind, item_hash, sender, stemming=False)
    if outbox:
        raise AssertionError(f"delivery did not settle within {max_steps} steps")
    return steps


def _propagate(adj, origin, kind=gossip_mod.KIND_TX, item_hash="tx", item=None):
    """Originate one item at `origin` and return the set of nodes that end
    up holding it, once every send it triggered has been delivered."""
    nodes, outboxes = _build_network(adj)
    held = {origin}
    nodes[origin].spread(item if item is not None else {"h": item_hash},
                         kind, item_hash)
    _drain(nodes, outboxes[kind], kind, item_hash, held)
    return held


def _ring(n):  return {i: [(i - 1) % n, (i + 1) % n] for i in range(n)}
def _line(n):  return {i: [j for j in (i - 1, i + 1) if 0 <= j < n] for i in range(n)}
def _star(n):  return {0: list(range(1, n)), **{i: [0] for i in range(1, n)}}


def _mesh(n, extra_edges_per_node, seed):
    """A ring, so the graph is connected the way real bootstrap peering
    guarantees it (every node knows at least a predecessor), plus a
    handful of random extra edges per node, closer to a real peer-to-peer
    graph than a ring, line or star alone. `seed` controls only which
    extra edges exist, not the stem/fluff decisions made over it.
    """
    rnd = random.Random(seed)
    adj = {i: {(i - 1) % n, (i + 1) % n} for i in range(n)}
    for i in range(n):
        for _ in range(extra_edges_per_node):
            j = rnd.randrange(n)
            if j != i:
                adj[i].add(j)
                adj[j].add(i)
    return {i: sorted(peers) for i, peers in adj.items()}


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

    def test_a_ring_is_fully_covered_once_anything_fluffs(self, monkeypatch):
        # Always fluff at the first hop, so the flood is what is under test
        # rather than how long the stem happened to run.
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = _ring(6)
        for origin in adj:
            assert _propagate(adj, origin) == set(adj), \
                f"ring left nodes uncovered starting from {origin}"

    def test_a_line_is_fully_covered(self, monkeypatch):
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = _line(8)
        for origin in adj:
            assert _propagate(adj, origin) == set(adj)

    def test_a_star_is_fully_covered_from_a_leaf(self, monkeypatch):
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = _star(6)
        assert _propagate(adj, 3) == set(adj)

    def test_a_single_bridge_is_crossed(self, monkeypatch):
        # Two cliques joined by one edge: the flood has to traverse it.
        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        adj = {0: [1, 2, 3], 1: [0, 2], 2: [0, 1], 3: [0, 4, 5], 4: [3, 5], 5: [3, 4]}
        for origin in adj:
            assert _propagate(adj, origin) == set(adj)

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


# ---------------------------------------------------------------------------
# 5. Delivery at scale, under the network's own real randomness, then under
#    an order flood. What plan.md 4.1 asks for: measured, not asserted.
# ---------------------------------------------------------------------------

class TestDeliveryUnderRealRandomness:
    """Every other coverage test above pins _random_fraction to an extreme
    so the flood, not the stem, is what gets exercised. That is the right
    default, but it never once lets the real coin flip run, and the real
    coin flip is exactly what decides how many hops a private walk takes
    before anything has been measured.

    Delivery does not mathematically depend on which values that flip
    produces: every walk stops stemming somewhere with probability 1 (a
    dead end forces it even if the draw never would), and once anything
    goes public the flood is a deterministic sweep of the connected graph.
    So this is a property that should hold on a single trial. Running many
    is what turns that argument from something read in the module
    docstring into something watched happening: 100% delivery at the small
    end of this network's expected scale and at the large end, using the
    unpatched, cryptographically-random _random_fraction throughout.
    """

    TRIALS = 30

    # _forward/_fluff never branch on kind (see gossip.Gossip._forward and
    # ._fluff: kind is only used to pick the seen-cache and, in _send, the
    # wire method); the delivery guarantee below is provably the same
    # walk for a tx, a block, an order or a claim. Proven here for all
    # four rather than argued from the source, so an order or claim
    # gaining a kind-specific branch later that quietly weakens its
    # delivery would fail these tests, not just look correct on inspection.
    KINDS = (gossip_mod.KIND_TX, gossip_mod.KIND_BLOCK,
            gossip_mod.KIND_ORDER, gossip_mod.KIND_CLAIM)

    def test_full_delivery_at_3_nodes(self):
        adj = _ring(3)
        for kind in self.KINDS:
            for trial in range(self.TRIALS):
                for origin in adj:
                    held = _propagate(adj, origin, kind=kind,
                                      item_hash=f"{kind}-t{trial}-{origin}")
                    assert held == set(adj), (
                        f"{kind} trial {trial} from node {origin} reached only {held}")

    def test_full_delivery_at_100_nodes(self):
        for kind in self.KINDS:
            for trial in range(self.TRIALS):
                adj = _mesh(100, extra_edges_per_node=3, seed=trial)
                origin = trial % 100
                held = _propagate(adj, origin, kind=kind, item_hash=f"{kind}-t{trial}")
                assert held == set(adj), (
                    f"{kind} trial {trial} from node {origin} reached "
                    f"{len(held)}/100 nodes")

    def test_full_delivery_on_a_sparser_100_node_graph(self):
        """Fewer extra edges than the main scale test: closer to a network
        of nodes with few peers each, where the stem rule's dead-end clause
        (fluff rather than strand when the only peer is the predecessor)
        carries more of the weight."""
        for kind in self.KINDS:
            for trial in range(self.TRIALS):
                adj = _mesh(100, extra_edges_per_node=1, seed=1000 + trial)
                origin = trial % 100
                held = _propagate(adj, origin, kind=kind, item_hash=f"{kind}-s{trial}")
                assert held == set(adj), (
                    f"{kind} trial {trial} from node {origin} reached "
                    f"{len(held)}/100 nodes on the sparse graph")


class TestOrderFloodDoesNotDegradeConsensusDelivery:
    """The property plan.md 4.1 exists for: a swap feature must not be
    able to slow consensus down. Before the per-kind cache split, enough
    distinct orders sharing a block's dedup cache could evict its entry,
    and an evicted block hash means that block gets re-flooded, a
    consensus cost paid for a feature that moves no funds and enters no
    block. The split makes this structural rather than a matter of timing:
    proven here by flooding an order cache well past its own ceiling on
    every node in the network and then measuring, not assuming, that a
    block still reaches all of them.

    Flood generation is pinned to always-fluff, which is a claim about
    speed and determinism for that phase only, not about the property
    under test: it makes each flooded order take the shortest path to
    full coverage so filling the cache costs one pass per item rather
    than an average of ten. The final block delivery, which is what the
    assertion is actually about, runs under real randomness so it is not
    trivially true by construction.
    """

    def test_block_still_reaches_everyone_after_the_order_cache_is_saturated(
            self, monkeypatch):
        n = 8
        adj = _mesh(n, extra_edges_per_node=2, seed=7)
        nodes, outboxes = _build_network(adj)

        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        flood_count = gossip_mod.ORDER_SEEN_CACHE_SIZE + 2_000
        origin = 0
        for k in range(flood_count):
            h = f"order-{k}"
            nodes[origin].spread({"o": h}, gossip_mod.KIND_ORDER, h)
            _drain(nodes, outboxes[gossip_mod.KIND_ORDER],
                  gossip_mod.KIND_ORDER, h, held={origin})
            origin = (origin + 1) % n   # spread the flood's origin around

        for node in nodes.values():
            assert len(node._seen[gossip_mod.KIND_ORDER]) == \
                gossip_mod.ORDER_SEEN_CACHE_SIZE, \
                "the order cache should have filled to its ceiling and no further"

        monkeypatch.undo()   # real randomness for the thing under test
        held = {5}
        nodes[5].spread({"h": "the-real-block"}, gossip_mod.KIND_BLOCK,
                        "the-real-block")
        _drain(nodes, outboxes[gossip_mod.KIND_BLOCK], gossip_mod.KIND_BLOCK,
              "the-real-block", held)
        assert held == set(adj), \
            f"block delivery degraded by the order flood: reached {held}"

    def test_tx_dedup_is_also_untouched_by_the_order_flood(self, monkeypatch):
        """Same property, the other consensus kind. Orders and txs are
        gossiped over the same peers at the same time in practice, so both
        need their own proof, not just block's."""
        n = 8
        adj = _mesh(n, extra_edges_per_node=2, seed=11)
        nodes, outboxes = _build_network(adj)

        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        flood_count = gossip_mod.ORDER_SEEN_CACHE_SIZE + 2_000
        for k in range(flood_count):
            h = f"order-{k}"
            nodes[0].spread({"o": h}, gossip_mod.KIND_ORDER, h)
            _drain(nodes, outboxes[gossip_mod.KIND_ORDER],
                  gossip_mod.KIND_ORDER, h, held={0})

        monkeypatch.undo()
        held = {3}
        nodes[3].spread({"h": "atx"}, gossip_mod.KIND_TX, "atx")
        _drain(nodes, outboxes[gossip_mod.KIND_TX], gossip_mod.KIND_TX,
              "atx", held)
        assert held == set(adj)

    def test_claim_flood_does_not_degrade_order_or_block_delivery(self, monkeypatch):
        """Claims are a fourth kind sharing the same infrastructure as
        orders (see market.py's claim section); they need the identical
        proof orders got, against both a legitimate order and a block."""
        n = 8
        adj = _mesh(n, extra_edges_per_node=2, seed=13)
        nodes, outboxes = _build_network(adj)

        monkeypatch.setattr(gossip_mod, "_random_fraction", lambda: 1.0)
        flood_count = gossip_mod.CLAIM_SEEN_CACHE_SIZE + 500
        for k in range(flood_count):
            h = f"claim-{k}"
            nodes[0].spread({"c": h}, gossip_mod.KIND_CLAIM, h)
            _drain(nodes, outboxes[gossip_mod.KIND_CLAIM],
                  gossip_mod.KIND_CLAIM, h, held={0})

        for node in nodes.values():
            assert len(node._seen[gossip_mod.KIND_CLAIM]) == \
                gossip_mod.CLAIM_SEEN_CACHE_SIZE

        monkeypatch.undo()
        order_held = {1}
        nodes[1].spread({"o": "an-order"}, gossip_mod.KIND_ORDER, "an-order")
        _drain(nodes, outboxes[gossip_mod.KIND_ORDER], gossip_mod.KIND_ORDER,
              "an-order", order_held)
        assert order_held == set(adj)

        block_held = {6}
        nodes[6].spread({"h": "a-block"}, gossip_mod.KIND_BLOCK, "a-block")
        _drain(nodes, outboxes[gossip_mod.KIND_BLOCK], gossip_mod.KIND_BLOCK,
              "a-block", block_held)
        assert block_held == set(adj)
