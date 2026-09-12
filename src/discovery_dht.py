"""BEP44 mutable-item DHT strategy and genesis-hash torrent peer discovery.

Owns the libtorrent session. Calls enqueue_candidate() on Discovery
(the coordinator) when a peer address arrives from either source.

Public interface used by the coordinator:
  DHTDiscovery(enqueue_fn, genesis_hash, port, node_pubkey_hex)
  .start()          create session, return (session, my_slot, my_offset)
  .process_alerts(ses)
  .put(ses, slot, addr)
  .get_all(ses, my_slot)
  .torrent_announce(ses)
  .torrent_get_peers(ses)
  .save_state(ses)
"""

import hashlib
import json
import logging
import time

import nacl.signing

from params import BEP44_SLOT_COUNT

try:
    import libtorrent as lt
except ImportError:
    lt = None  # type: ignore[assignment]

log = logging.getLogger("ec.discovery.dht")

DHT_STATE_FILE       = "lapsecoin_lt_dht.dat"
PUT_DELAY            = 30
PUT_REFRESH_INTERVAL = 3600


class DHTDiscovery:

    def __init__(self, enqueue_fn, genesis_hash, port, node_pubkey_hex=""):
        self._enqueue       = enqueue_fn
        self.genesis_hash   = genesis_hash
        self.port           = port
        self.node_pubkey_hex = node_pubkey_hex
        self.bootstrapped   = False  # set once dht_bootstrap_alert is seen
        self._slot_keys     = {}     # slot index -> (sk_64, pk), see _slot_keypair

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

        Also returns (my_slot, my_offset) for the caller's timing loop.
        """
        if lt is None:
            log.warning("[dht] libtorrent not installed: DHT peer discovery "
                        "is unavailable, this node still finds peers via "
                        "LAN broadcast, its saved peer cache, and any "
                        "peers given on the command line")
            return None, 0, 0
        ses = self._make_session()
        my_slot   = self._my_slot_index()   if self.node_pubkey_hex else 0
        my_offset = self._my_write_offset() if self.node_pubkey_hex else 0
        return ses, my_slot, my_offset

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
                try:
                    item = a.item
                    if isinstance(item, dict):
                        raw = item.get("value") or item.get(b"value") or b"{}"
                    else:
                        raw = item or b"{}"
                    data = json.loads(raw)
                    addr = data.get("addr", "")
                    if addr and ":" in addr:
                        log.info("[dht] BEP44 candidate  addr=%s", addr)
                        self._enqueue(addr)
                except Exception:
                    pass
            elif isinstance(a, lt.dht_get_peers_reply_alert):
                try:
                    for ep in a.peers():
                        addr = f"{ep.address()}:{ep.port()}"
                        log.info("[dht] torrent candidate  addr=%s", addr)
                        self._enqueue(addr)
                except Exception:
                    pass
            elif isinstance(a, lt.dht_put_alert):
                if a.num_success > 0:
                    log.info("[dht] BEP44 put accepted by %d node(s)", a.num_success)
                else:
                    log.debug("[dht] BEP44 put num_success=0")
            elif isinstance(a, lt.dht_bootstrap_alert):
                log.info("[dht] bootstrap complete")
                self.bootstrapped = True

    # ------------------------------------------------------------------
    # BEP44 put / get
    # ------------------------------------------------------------------

    def put(self, ses, slot_index, my_addr):
        sk_64, pk = self._slot_keypair(slot_index)
        value = json.dumps({"addr": my_addr, "ts": int(time.time())}).encode()
        try:
            ses.dht_put_mutable_item(sk_64, pk, value, b"lapsecoin-v1")
            log.info("[dht] BEP44 put  slot=%d  addr=%s", slot_index, my_addr)
        except Exception:
            log.exception("[dht] BEP44 put error")

    def get_all(self, ses, my_slot=None):
        for i in range(BEP44_SLOT_COUNT):
            if i == my_slot:
                continue
            _, pk = self._slot_keypair(i)
            try:
                ses.dht_get_mutable_item(pk, b"lapsecoin-v1")
            except Exception:
                pass
        log.debug("[dht] BEP44 get  slots=%d",
                  BEP44_SLOT_COUNT - (1 if my_slot is not None else 0))

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
            log.debug("[dht] torrent get_peers issued")
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

    def _my_slot_index(self):
        return int(hashlib.sha256(
            self.node_pubkey_hex.encode()
        ).hexdigest()[:8], 16) % BEP44_SLOT_COUNT

    def _my_write_offset(self):
        return int(hashlib.sha256(
            (self.node_pubkey_hex + "-offset").encode()
        ).hexdigest()[:8], 16) % 3600

    def _make_session(self):
        settings = lt.default_settings()
        settings["enable_dht"]    = True
        settings["enable_lsd"]    = False
        settings["enable_upnp"]   = False
        settings["enable_natpmp"] = False
        settings["listen_interfaces"] = f"0.0.0.0:{self.port + 3}"
        settings["alert_mask"] = (
            lt.alert.category_t.dht_notification
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
