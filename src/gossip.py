"""Propagation: one Dandelion stem/fluff mechanism for both blocks and txs.

Why one mechanism
-----------------
Blocks and txs are the same problem: get an item to every node without
advertising that this node is where it came from. They used to propagate
two different ways. Blocks epidemically at the transport layer (msg_id
dedup, sender excluded, no stem at all, and relayed *before* anyone
validated them), txs at this layer (hash dedup, no sender exclusion, stem
with a peer-count cliff). Two mechanisms meant two sets of bugs and two
places to reason about scale, for one problem.

Both now go through spread()/relay() below.

Dedup is by item hash, not msg_id
---------------------------------
The hash is what identifies the item; a msg_id is per-send, so re-sending
the same item mints a new one and sails straight through a msg_id-based
dedup, starting a fresh flood wave at every node that sees it. Hash dedup
is idempotent no matter how many times an item is re-originated, which is
what makes the rework path below safe.

Stem rule, one rule at every scale
----------------------------------
At each hop: fluff with probability 1-STEM_CONTINUE_PROB; otherwise
forward to a single peer that is not the one who just sent it to us; and
if there is no such peer, fluff.

That last clause is what replaces the old MIN_PEERS_FOR_STEM cliff, and
it is deliberately not a special case: it is the same condition evaluated
everywhere, it just fires more often on a sparse graph. Note it is a
*preference* against the predecessor, never a hard exclusion, a node
with one peer cannot assume that peer has any onward link, so refusing to
use it would strand the item rather than protect it. Fluffing early costs
a little anonymity; dead-ending loses the item.

Stem length is therefore emergent from the topology and the coin flip,
not a configured hop count: on a 2-peer node the walk fluffs almost
immediately by itself, on a well-connected one it keeps going.

Rework
------
The originator holds an item it started and watches for its own flood to
come back (see Node). No echo means the walk died somewhere, a dropped
datagram, a peer that went away mid-walk, so it is re-sent. That only
happens on failure, so its cost is proportional to the failure rate, not
to traffic.
"""

import logging
import secrets
import threading
from cachetools import LRUCache

import tx as tx_mod

log = logging.getLogger("ec.gossip")

SEEN_CACHE_SIZE = 50_000

# Probability that a stem hop forwards again instead of fluffing, giving a
# geometric stem length with mean 1/(1-q) hops where the topology allows it.
#
# This is a policy constant, not a derived one, and it is worth being blunt
# about that: it trades anonymity against latency, and those two have no
# common unit to optimise over. Nothing a node can measure locally tells it
# the right value. What *is* derived is everything around it, the walk
# stops on its own when the graph runs out of peers, and the rework timeout
# below comes from measured echo latency.
#
# Randomising the stop (rather than counting hops down from a fixed number)
# is the part that actually buys anonymity: a fixed count would put the
# originator a known distance back.
STEM_CONTINUE_PROB = 0.9

def _random_fraction():
    """A uniform [0,1) from the system CSPRNG.

    Deliberately not random.random(): the stem's stop decision and its
    choice of next hop are what an observer would have to guess to place an
    item's origin, and Mersenne Twister is reconstructible from enough
    observed output. Both are cheap here. One draw per hop, per item,
    so there is no reason to use a predictable generator for the one thing
    the stem is keeping secret.
    """
    return secrets.randbits(53) / (1 << 53)


KIND_BLOCK = "block"
KIND_TX    = "tx"


class Gossip:

    def __init__(self, pool, udp):
        self.pool = pool
        self.udp  = udp
        # Hash -> True for every item we've already put on the wire in the
        # public (fluff) phase, so each node floods a given item exactly
        # once no matter how many copies reach it.
        self._seen  = LRUCache(maxsize=SEEN_CACHE_SIZE)
        # Items already forwarded in the private phase, see mark_stem_seen.
        self._stem_seen = LRUCache(maxsize=SEEN_CACHE_SIZE)
        self._lock  = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def spread(self, item, kind, item_hash):
        """Start propagating an item this node originated (a block we just
        built, or a tx we just signed). Enters the stem; see the module
        docstring for the rule.

        Returns the peer it was handed to, or None if it went straight
        public. The caller needs to know, because that peer is the one node
        whose word about this item is worth nothing: it is the only one that
        can swallow the item and still echo it back (see Node._note_echo).
        """
        return self._forward(item, kind, item_hash, predecessor=None)

    def relay(self, item, kind, item_hash, sender, stemming):
        """Continue propagating an item that reached us from `sender`.

        stemming: True if it arrived still in the private phase, in which
        case we apply the stem rule again; False if it arrived public, in
        which case we simply flood it onward (privacy is already spent, and
        re-stemming a public item would only slow it down)."""
        if stemming:
            self._forward(item, kind, item_hash, predecessor=sender)
        else:
            self._fluff(item, kind, item_hash, exclude=sender)

    def force_fluff(self, item, kind, item_hash):
        """Flood an item we already put on the wire once, ignoring the seen
        cache. Only for the rework path (Node._retry_unconfirmed_spreads):
        our own first send is what marked this hash, which is exactly what
        would make the resend we're trying to force a silent no-op."""
        peers = self.pool.get_all()
        if not peers:
            return
        log.debug("[gossip] re-flooding %s to %d peers", kind, len(peers))
        self._send(item, kind, peers=peers, stemming=False)

    def mark_seen(self, h):
        """Mark h as already handled. Returns True if it already was."""
        with self._lock:
            if h in self._seen:
                return True
            self._seen[h] = True
            return False

    def mark_stem_seen(self, h):
        """Mark h as already relayed in the private phase. Returns True if
        it already was.

        Kept apart from _seen, which is the public phase's "we have already
        flooded this" and must stay that, or a stem hop would mark an item
        and the fluff that follows it would be swallowed as a duplicate.

        The stem had no dedup of any kind before this. A walk forwards to
        one peer that is merely not its predecessor, so a hop two steps
        back is a perfectly legal next choice and the walk can revisit
        nodes; nothing bounds it but the coin flip that ends it. Every
        revisit cost a full signature verification and a ledger snapshot
        before the item was handed straight on again, and re-sending a
        stemming item was free for anyone who wanted to make that happen
        on purpose. Only receives mark it, never spread(): an originator
        that marked its own item would then ignore its own echo coming
        back and re-flood on every retry.
        """
        with self._lock:
            if h in self._stem_seen:
                return True
            self._stem_seen[h] = True
            return False

    # ------------------------------------------------------------------
    # Stem / fluff
    # ------------------------------------------------------------------

    def _forward(self, item, kind, item_hash, predecessor):
        """One stem hop, or a fluff if the rule says to stop here. Returns
        the peer stemmed to, or None if it fluffed."""
        if _random_fraction() < STEM_CONTINUE_PROB:
            peer = self._stem_peer(predecessor)
            if peer is not None:
                log.debug("[gossip] stem %s to %s", kind, peer)
                self._send(item, kind, peers=[peer], stemming=True)
                return peer
            # No peer other than whoever handed it to us. Continuing would
            # mean handing it straight back, so this is where the walk ends.
        self._fluff(item, kind, item_hash, exclude=predecessor)
        return None

    def _stem_peer(self, predecessor):
        """One random peer that isn't the predecessor, or None if the only
        peers we have are the predecessor (or none at all)."""
        peers = [p for p in self.pool.get_all() if p != predecessor]
        return secrets.choice(peers) if peers else None

    def _fluff(self, item, kind, item_hash, exclude=None):
        """Public phase: flood to every peer except whoever sent it to us,
        at most once per item hash."""
        if self.mark_seen(item_hash):
            return
        peers = [p for p in self.pool.get_all() if p != exclude]
        if not peers:
            return
        log.debug("[gossip] fluff %s to %d peers", kind, len(peers))
        self._send(item, kind, peers=peers, stemming=False)

    def _send(self, item, kind, peers, stemming):
        if kind == KIND_BLOCK:
            self.udp.send_block(item, peers=peers, stemming=stemming)
        else:
            self.udp.send_tx(item, peers=peers, stemming=stemming)
