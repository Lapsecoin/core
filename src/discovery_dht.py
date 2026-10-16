"""BEP44 mutable-item DHT strategy and genesis-hash torrent peer discovery.

Owns the libtorrent session. Calls enqueue_candidate() on Discovery
(the coordinator) when a peer address arrives from either source.

Public interface used by the coordinator:
  DHTDiscovery(enqueue_fn, genesis_hash, port)
  .start()          create session, return (session, my_offset)
  .process_alerts(ses)
  .put(ses, slot, addr)
  .get_all(ses)
  .torrent_announce(ses)
  .torrent_get_peers(ses)
  .save_state(ses)
"""

import hashlib
import json
import logging
import random
import time

import nacl.signing

from logthrottle import Throttle, more
from params import BEP44_SLOT_COUNT

try:
    import libtorrent as lt
except ImportError:
    lt = None  # type: ignore[assignment]

# False means no DHT: this node only finds peers it is given or can see on its
# own network. The dashboard says so while it has no peers.
HAS_LIBTORRENT = lt is not None

log = logging.getLogger("ec.discovery.dht")

DHT_STATE_FILE       = "lapsecoin_lt_dht.dat"
PUT_REFRESH_INTERVAL = 3600
GET_SLOTS_PER_TICK   = 6
PUT_AFTER_BOOTSTRAP  = 30
PUT_BOOTSTRAP_GIVE_UP = 180
PUT_VERIFY_ATTEMPTS  = 6
STALE_ITEM_SECONDS   = 3 * 3600   # an entry older than this is a ghost
SLOT_CONFLICT_AFTER  = 45
MAX_SLOT_MOVES       = 6


class DHTDiscovery:

    def __init__(self, enqueue_fn, genesis_hash, port):
        self._enqueue       = enqueue_fn
        self.genesis_hash   = genesis_hash
        self.port           = port
        self.bootstrapped   = False  # set once dht_bootstrap_alert is seen
        self._slot_keys     = {}     # slot index -> (sk_64, pk), see _slot_keypair
        self._pk_to_slot    = None   # public key bytes -> slot index, for logging
        self.put_failed     = False  # last put reached no node, retry early
        self._candidate_log = Throttle(600)  # same address, one line per 10 min
        self._started_at    = time.monotonic()
        self._bootstrapped_at = None
        self._own_slot      = None   # slot of our last put
        self._own_visible   = False  # a read round has seen that put
        self._own_addr      = None   # what we last wrote there
        self._put_acked_at  = None   # when a node last accepted that put
        self._slot_nonce    = 0      # bumped to move off a slot that is taken
        self._stale_skipped = 0
        self.slot_taken     = False  # our slot holds somebody else's value
        self._put_attempts  = 0
        self._pending_gets  = []     # slots still to query in this round
        self._round_total   = 0      # slots queried this round
        self._round_done    = 0      # of those, how many have answered
        self._round_hits    = []     # slots that held an address

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Create and return the libtorrent session, or None if libtorrent
        isn't installed. DHT discovery is one of several ways to find
        peers, not the only one (see Discovery.run: LAN broadcast, the
        peer cache, and manually-added/bootstrap peers all work without
        it), so a missing optional C++ dependency should cost DHT
        discovery specifically, not the whole discovery thread. The
        caller (Discovery.run) checks for None once and skips every other
        method here entirely rather than calling into a session that
        doesn't exist.

        Also returns my_offset for the caller's timing loop. The slot is
        not known yet: it derives from our external ip:port (see slot_for).
        """
        if lt is None:
            log.warning("[dht] libtorrent not installed: DHT peer discovery "
                        "is unavailable, this node still finds peers via "
                        "LAN broadcast, its saved peer cache, and any "
                        "peers given on the command line")
            return None, 0
        ses = self._make_session()
        my_offset = self._my_write_offset()
        return ses, my_offset

    def save_state(self, ses):
        try:
            state = ses.save_state()
            with open(DHT_STATE_FILE, "wb") as f:
                f.write(lt.bencode(state))
        except Exception:
            log.debug("[dht] state save failed", exc_info=True)

    # ------------------------------------------------------------------
    # Alert processing
    # ------------------------------------------------------------------

    def process_alerts(self, ses):
        for a in ses.pop_alerts():
            if isinstance(a, lt.dht_mutable_item_alert):
                addr, ts = "", 0
                try:
                    item = a.item
                    if isinstance(item, dict):
                        raw = item.get("value") or item.get(b"value") or b"{}"
                    else:
                        raw = item or b"{}"
                    data = json.loads(raw)
                    addr = data.get("addr", "")
                    ts = int(data.get("ts", 0) or 0)
                except Exception:
                    addr = ""
                self._note_item(a, addr)
                if addr and ":" in addr:
                    if ts and time.time() - ts > STALE_ITEM_SECONDS:
                        # libtorrent keeps stored items forever by default,
                        # so slots of nodes long gone still answer. The
                        # entry carries the time it was written.
                        self._stale_skipped += 1
                        continue
                    held = self._candidate_log.allow(addr)
                    if held is not None:
                        log.info("[dht] BEP44 candidate  addr=%s%s", addr, more(held))
                    self._enqueue(addr)
            elif isinstance(a, lt.dht_get_peers_reply_alert):
                try:
                    for ep in a.peers():
                        addr = f"{ep.address()}:{ep.port()}"
                        held = self._candidate_log.allow(addr)
                        if held is not None:
                            log.info("[dht] torrent candidate  addr=%s%s", addr, more(held))
                        self._enqueue(addr)
                except Exception:
                    pass
            elif isinstance(a, lt.dht_put_alert):
                if a.num_success > 0:
                    log.info("[dht] BEP44 put accepted by %d node(s)", a.num_success)
                    self.put_failed = False
                    self._put_acked_at = time.monotonic()
                else:
                    # A put that reaches nobody leaves our slot empty until
                    # the next hourly refresh, so the caller retries early.
                    log.info("[dht] BEP44 put reached no node, will retry")
                    self.put_failed = True
            elif isinstance(a, lt.dht_bootstrap_alert):
                log.info("[dht] bootstrap complete")
                self.bootstrapped = True
                self._bootstrapped_at = time.monotonic()

    def _note_item(self, alert, addr):
        """Track one slot answer: fold the round into a single log line, and
        check our own slot from the outside. Our slot only counts as visible
        when it holds OUR address. Any other value there (a node that hashed
        to the same slot, or a ghost left by one that is gone, whose higher
        sequence number wins every read) means readers never see us, so the
        slot is changed and the put repeated."""
        try:
            if not getattr(alert, "authoritative", False):
                return
            if self._pk_to_slot is None:
                self._pk_to_slot = {self._slot_keypair(i)[1]: i
                                    for i in range(BEP44_SLOT_COUNT)}
            slot = self._pk_to_slot.get(bytes(alert.key), -1)
            seq = getattr(alert, "seq", 0)
            self._round_done += 1
            if slot == self._own_slot and seq > 0:
                if addr == self._own_addr:
                    self._own_visible = True
                    self.slot_taken = False
                elif (self._put_acked_at is not None
                      and time.monotonic() - self._put_acked_at >= SLOT_CONFLICT_AFTER):
                    if not self.slot_taken:
                        log.info("[dht] slot %d holds %s, not us; moving to another slot",
                                 slot, addr or "something else")
                    self.slot_taken = True
            if seq > 0:
                self._round_hits.append(slot)
            if self._round_done >= self._round_total > 0:
                hits = ", ".join(str(i) for i in sorted(self._round_hits))
                stale = (f", {self._stale_skipped} too old to use"
                         if self._stale_skipped else "")
                log.debug("[dht] BEP44 read round: %d slots answered, %d hold an address%s%s",
                          self._round_done, len(self._round_hits), stale,
                          f" (slots {hits})" if hits else "")
                self._round_total = 0
                self._stale_skipped = 0
        except Exception:
            pass

    # ------------------------------------------------------------------
    # BEP44 put / get
    # ------------------------------------------------------------------

    def ready_to_put(self):
        """A put sent into a routing table that has only just bootstrapped
        lands on whichever few nodes it knows, not the ones closest to the
        slot, and then no reader's lookup ever reaches it (seen: a node's own
        slot unreadable for 12 minutes after a put one second before
        bootstrap finished). So wait a little after bootstrap, but never
        longer than a few minutes if the alert does not arrive."""
        now = time.monotonic()
        if self._bootstrapped_at is not None:
            return now - self._bootstrapped_at >= PUT_AFTER_BOOTSTRAP
        return now - self._started_at >= PUT_BOOTSTRAP_GIVE_UP

    def needs_reput(self):
        """True while our last put has not shown up in a read round, up to
        PUT_VERIFY_ATTEMPTS tries. Our own slot is read like any other, so
        this checks the put from the outside, the way a peer would see it."""
        if self.slot_taken and self._slot_nonce < MAX_SLOT_MOVES:
            return True
        return (self._own_slot is not None and not self._own_visible
                and 0 < self._put_attempts < PUT_VERIFY_ATTEMPTS)

    def move_slot_if_taken(self):
        """Step to the next candidate slot when ours is held by someone
        else. Returns True if it moved."""
        if self.slot_taken and self._slot_nonce < MAX_SLOT_MOVES:
            self._slot_nonce += 1
            self.slot_taken = False
            return True
        return False

    def put(self, ses, slot_index, my_addr):
        self._own_addr = my_addr
        self._put_acked_at = None
        if slot_index == self._own_slot and not self._own_visible:
            self._put_attempts += 1
        else:
            self._put_attempts = 1
        self._own_slot, self._own_visible = slot_index, False
        sk_64, pk = self._slot_keypair(slot_index)
        value = json.dumps({"addr": my_addr, "ts": int(time.time())}).encode()
        self.put_failed = False
        try:
            ses.dht_put_mutable_item(sk_64, pk, value, b"lapsecoin-v1")
            log.info("[dht] BEP44 put  slot=%d  addr=%s", slot_index, my_addr)
        except Exception:
            log.exception("[dht] BEP44 put error")

    def get_all(self, ses):
        """Start a read round over every slot. The lookups are issued a few
        per tick by pump_gets, not all at once: 255 simultaneous traversals
        plus libtorrent's 8000 B/s DHT quota meant many of them starved,
        and a fresh node's thin routing table made it worse."""
        self._pending_gets = list(range(BEP44_SLOT_COUNT))
        self._round_total, self._round_done, self._round_hits = BEP44_SLOT_COUNT, 0, []
        random.shuffle(self._pending_gets)
        self.pump_gets(ses)

    def pump_gets(self, ses, n=GET_SLOTS_PER_TICK):
        for _ in range(min(n, len(self._pending_gets))):
            i = self._pending_gets.pop()
            _, pk = self._slot_keypair(i)
            try:
                ses.dht_get_mutable_item(pk, b"lapsecoin-v1")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Genesis-hash torrent (BEP 5 get_peers / announce)
    # ------------------------------------------------------------------

    def torrent_announce(self, ses):
        try:
            ses.dht_announce(self._genesis_info_hash(), self.port, 0)
            log.debug("[dht] torrent announced  port=%d", self.port)
        except Exception:
            log.debug("[dht] torrent announce failed", exc_info=True)

    def torrent_get_peers(self, ses):
        try:
            ses.dht_get_peers(self._genesis_info_hash())
        except Exception:
            log.debug("[dht] torrent get_peers failed", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _genesis_info_hash(self):
        raw = hashlib.sha1(bytes.fromhex(self.genesis_hash)).digest()
        return lt.sha1_hash(raw)

    def _slot_keypair(self, slot_index):
        """The BEP44 keypair for one slot, derived once and kept.

        A pure function of the genesis hash and the slot index, so it never
        changes for the life of the node, yet get_all re-derived all
        BEP44_SLOT_COUNT of them on every pass: an Ed25519 key construction
        plus two hashes per slot, 255 of them, once a minute, forever, for
        values that were identical every time.
        """
        cached = self._slot_keys.get(slot_index)
        if cached is None:
            cached = self._derive_slot_keypair(slot_index)
            self._slot_keys[slot_index] = cached
        return cached

    def _derive_slot_keypair(self, slot_index):
        seed = hashlib.sha256(
            b"lapsecoin-peers-v1:"
            + self.genesis_hash.encode()
            + b":"
            + str(slot_index).encode()
        ).digest()
        sk_obj = nacl.signing.SigningKey(seed)
        pk     = bytes(sk_obj.verify_key)
        h      = bytearray(hashlib.sha512(seed).digest())
        h[0]  &= 248
        h[31] &= 127
        h[31] |= 64
        sk_64  = bytes(h)
        return sk_64, pk

    def slot_for(self, addr):
        """Deterministic slot for this node's published ip:port, stable while
        the node keeps its address. Nothing about who runs the node goes into
        it: the slot only has to differ between nodes, and where they can be
        reached already does that."""
        ident = addr
        if self._slot_nonce:
            ident += f":{self._slot_nonce}"
        return int(hashlib.sha256(ident.encode()).hexdigest()[:8], 16) % BEP44_SLOT_COUNT

    def _my_write_offset(self):
        """Where in the hour this node's writes fall, so nodes started
        together do not all write at once. Only spreading is wanted, so it is
        simply random for this run."""
        return random.randrange(3600)

    def _make_session(self):
        settings = lt.default_settings()
        settings["enable_dht"]    = True
        settings["enable_lsd"]    = False
        settings["enable_upnp"]   = False
        settings["enable_natpmp"] = False
        settings["listen_interfaces"] = f"0.0.0.0:{self.port + 3}"
        # dht_get_peers_reply_alert is a dht_operation alert, not a plain
        # dht one. Without that category the genesis-torrent lookups ran
        # but their replies were never delivered, so the one channel that
        # has no shared slots (every node announces its own ip:port) found
        # nobody. No log in the field ever showed a "torrent candidate".
        settings["alert_mask"] = (
            lt.alert.category_t.dht_notification
            | lt.alert.category_t.dht_operation_notification
            | lt.alert.category_t.status_notification
        )
        settings["dht_bootstrap_nodes"] = (
            "router.bittorrent.com:6881,"
            "router.utorrent.com:6881,"
            "dht.transmissionbt.com:6881,"
            "dht.aelitis.com:6881"
        )
        ses = lt.session(settings)
        try:
            with open(DHT_STATE_FILE, "rb") as f:
                state = lt.bdecode(f.read())
            ses.load_state(state)
            log.info("[dht] routing table loaded")
        except Exception:
            pass
        return ses
