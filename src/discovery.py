"""DHT peer discovery coordinator, UDP edition.

Removes all HTTP probing and UPnP. Candidate pipeline now:
  1. Receive candidate addr from DHT / peer-exchange / --peer CLI
  2. UDP PING the candidate (3s timeout)
  3. PONG confirms reachability AND gives us our external IP
  4. Exchange peer lists via UDP PEERS message
  5. Admit to PeerPool

Separately, a periodic LAN broadcast (UDPTransport.broadcast_discover) finds
nodes on the same local network directly. No DHT round-trip and no
NAT/punching. It announces our own data port on a small fixed
LAN_DISCOVERY_PORT every node also listens on, so two nodes on the same
network that happen to run on different data ports (e.g. two machines
behind the same router, each with its own port-forward) still find each
other; a private-source reply then proves direct reachability on sight
(see peer_udp._is_lan_source). This is what lets two machines behind the
same router/public IP peer automatically instead of needing a manual add.

Hole punching
-------------
When PING times out (node is behind NAT) we attempt a hole punch
via any already-connected peer acting as relay:
  1. Ask relay to forward PUNCH_REQ to target
  2. Both sides fire simultaneous UDP packets
  3. Retry PING after 1s

Our own external address
------------------------
Learned from the first PONG reply (the peer tells us what IP:port
they saw the packet arrive from). This replaces both UPnP and the
ipify.org HTTP call. Also updated every time any PONG arrives.

External public interface (called from main.py):
  Discovery(udp, pool, genesis_hash, port, node_pubkey_hex)
  .enqueue_candidate(addr)
  .add_bootstrap_peer(addr)
  .run()   blocking, run as daemon thread
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from discovery_dht import DHTDiscovery, PUT_REFRESH_INTERVAL
from peerpool import is_routable_peer_addr

log = logging.getLogger("ec.discovery")

PEER_CACHE_FILE      = "lapsecoin_peers.json"
SAVE_INTERVAL        = 300
GET_INTERVAL         = 60
STAGE_FLUSH_INTERVAL = 15
PUT_DELAY_LOCAL      = 30
LAN_BROADCAST_INTERVAL = 60   # seconds between LAN broadcast discovery pings
DHT_BOOTSTRAP_TIMEOUT  = 15   # max wait for DHT bootstrap before first query anyway

# BEP5 torrent announce (BitTorrent's normal "I'm here" for this genesis's
# swarm) is a cheap, ordinary announce_peer call. Nothing like BEP44's
# mutable-item put, which needs the staggered PUT_DELAY_LOCAL/jitter to
# avoid every node's write landing on the DHT at once. Tying it to that same
# once-an-hour cadence (as it used to be) meant a fresh node didn't tell the
# swarm it existed for up to PUT_DELAY_LOCAL + up to 300s of jitter, then
# not again for another hour, real BitTorrent clients re-announce every
# few minutes, not hourly.
TORRENT_ANNOUNCE_INTERVAL = 300

# How often to log where discovery stands. Without this, a node with zero
# peers just goes quiet after the startup burst, indistinguishable from
# hung, same problem the VDF wait had before it got a heartbeat too.
STATUS_LOG_INTERVAL = 60

PUNCH_ATTEMPTS       = 3     # how many relays to try when direct ping fails
PUNCH_WAIT           = 2.5   # seconds to wait after punch before re-pinging


class Discovery:

    def __init__(self, udp, pool, genesis_hash, port, node_pubkey_hex=""):
        self.udp          = udp       # UDPTransport instance
        self.pool         = pool
        self.genesis_hash = genesis_hash
        self.port         = port
        self._candidates  = set()
        self._lock        = threading.Lock()

        if not node_pubkey_hex:
            log.warning("[dht] no node_pubkey_hex; all such nodes share slot 0")

        self._dht   = DHTDiscovery(self.enqueue_candidate, genesis_hash,
                                   port, node_pubkey_hex)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="disc")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def enqueue_candidate(self, addr):
        if isinstance(addr, str) and ":" in addr and is_routable_peer_addr(addr):
            with self._lock:
                self._candidates.add(addr)

    def _is_own_addr(self, addr: str) -> bool:
        """True if addr is this node's own, by either of the two ways an
        address can be self: it matches our confirmed external ip:port, or
        its host is one of our own local interfaces. _flush_candidates has
        always excluded the first; add_bootstrap_peer excluded neither,
        so a --peer pointed at this node's own address (a copy-paste
        mistake, or a config templated the same for every node) could
        ping itself and, on a router that hairpins its own public IP back
        to the sender, actually get a PONG and self-admit. Nothing else
        about _ping_and_admit would have caught that: PeerPool.add has no
        notion of which address is "us"."""
        our_ext = self.udp.our_external_addr or ""
        if addr == our_ext:
            return True
        host = addr.rsplit(":", 1)[0]
        return host in self.udp._local_ips

    def add_bootstrap_peer(self, addr):
        """Admit a --peer CLI address. Tries UDP ping; if it fails attempts
        hole punch through any already-connected peer."""
        if not (isinstance(addr, str) and ":" in addr):
            return
        if self._is_own_addr(addr):
            log.warning("[peers] --peer %s is this node's own address, ignoring", addr)
            return
        if self._ping_and_admit(addr):
            log.info("[peers] connected to the peer given on the command line, %s", addr)
            return
        # Try hole punch via any existing peer
        for relay in self.pool.get_all()[:3]:
            if self._punch_and_admit(relay, addr):
                log.info("[peers] connected to %s (needed a hole punch through NAT)", addr)
                return
        log.warning("[peers] could not reach %s, the peer given on the command line", addr)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self._load_peer_cache()
        # Cached peers from a previous run are already known-good, connect
        # to them immediately rather than waiting on DHT bootstrap timing,
        # which has nothing to do with them. Same for LAN peers: broadcast
        # discovery doesn't touch the DHT at all, so there's no reason to
        # wait on it either.
        self._flush_candidates()
        self.udp.broadcast_discover()

        # ses is None when libtorrent isn't installed (_dht.start already
        # logged why). alert_event still works as a plain 1s-timeout sleep
        # in that case, nothing ever sets it early, so it's left
        # unconditional below; every actual call into ses is guarded.
        ses, my_slot, my_offset = self._dht.start()
        alert_event = threading.Event()
        if ses is not None:
            ses.set_alert_notify(alert_event.set)

        # When we receive PUNCH_GO, ping the target immediately while the hole is open
        def _on_punch_go(addr):
            log.debug("[peer] punch_go received, pinging immediately  addr=%s", addr)
            self._executor.submit(self._ping_and_admit, addr)

        self.udp.set_punch_go_callback(_on_punch_go)
        self.udp._on_peer_hint = self.enqueue_candidate

        if ses is not None:
            log.info("[dht] started, bootstrapping")
        # Seed our external address early so PINGs we send include "from" field.
        # This lets NAT-loopback peers send PONG to our real IP instead of the
        # hairpinned source address.
        if not self.udp.our_external_addr:
            ip = self._fallback_ip()
            if ip:
                self.udp.our_external_addr = f"{ip}:{self.port}"
                log.info("[peers] our address as others see it: %s:%d", ip, self.port)

        # Wait for the DHT routing table to settle before issuing the first
        # query, so it isn't sent into an empty table, but don't just
        # sleep the full worst case: most of the time bootstrap finishes
        # well under this, and firing get_all/get_peers the moment it does
        # (rather than always waiting out a flat 15s) is exactly what makes
        # the very first DHT lookup land sooner instead of on the next
        # periodic retry a minute later.
        bootstrap_deadline = time.monotonic() + DHT_BOOTSTRAP_TIMEOUT
        while (ses is not None and time.monotonic() < bootstrap_deadline
               and not self._dht.bootstrapped):
            alert_event.wait(timeout=1)
            alert_event.clear()
            self._dht.process_alerts(ses)

        put_delay = PUT_DELAY_LOCAL + my_offset % 300
        now = time.monotonic()

        last_flush    = now - STAGE_FLUSH_INTERVAL
        last_get      = now
        last_put      = now - PUT_REFRESH_INTERVAL + put_delay
        last_announce = now
        last_save     = now
        last_lan      = now - LAN_BROADCAST_INTERVAL
        last_status   = now - STATUS_LOG_INTERVAL

        if ses is not None:
            self._dht.get_all(ses, my_slot)
            self._dht.torrent_get_peers(ses)
            self._dht.torrent_announce(ses)   # own cadence, see TORRENT_ANNOUNCE_INTERVAL
        last_get = time.monotonic()

        while True:
            alert_event.wait(timeout=1)
            alert_event.clear()
            if ses is not None:
                self._dht.process_alerts(ses)

            now    = time.monotonic()
            at_max = self.pool.count() >= self.pool._max_peers

            if now - last_flush >= STAGE_FLUSH_INTERVAL:
                self._flush_candidates()
                last_flush = now

            get_interval = 300 if at_max else GET_INTERVAL
            if ses is not None and now - last_get >= get_interval:
                self._dht.get_all(ses, my_slot)
                self._dht.torrent_get_peers(ses)
                last_get = now


            if ses is not None and now - last_put >= PUT_REFRESH_INTERVAL:
                # Use external addr learned from PONG, or fall back to ipify
                ext = self.udp.our_external_addr
                if not ext:
                    ext = self._fallback_ip()
                if ext:
                    my_addr = ext if ":" in ext else f"{ext}:{self.port}"
                    self._dht.put(ses, my_slot, my_addr)
                last_put = now

            if ses is not None and now - last_announce >= TORRENT_ANNOUNCE_INTERVAL:
                self._dht.torrent_announce(ses)
                last_announce = now

            self.pool.evict_stale()

            if now - last_lan >= LAN_BROADCAST_INTERVAL and not at_max:
                self.udp.broadcast_discover()
                log.debug("[peer] LAN broadcast sent")
                last_lan = now

            if now - last_status >= STATUS_LOG_INTERVAL:
                with self._lock:
                    queued = len(self._candidates)
                log.info("[peers] %d connected, %d address(es) left to try",
                         self.pool.count(), queued)
                last_status = now

            if now - last_save >= SAVE_INTERVAL:
                self._save_peer_cache()
                if ses is not None:
                    self._dht.save_state(ses)
                last_save = now

    # ------------------------------------------------------------------
    # Candidate pipeline
    # ------------------------------------------------------------------

    def _flush_candidates(self):
        with self._lock:
            if not self._candidates:
                return
            batch = self._candidates.copy()
            self._candidates.clear()

        known = set(self.pool.all_addrs())
        fresh = [a for a in batch if a not in known and not self._is_own_addr(a)]
        if not fresh:
            return

        # "flushing" read as "discarding" to at least one operator, who
        # concluded their peers were being rejected. It is the opposite:
        # these are about to be tried.
        log.info("[peers] trying %d new address(es): %s",
                 len(fresh), ", ".join(fresh))

        # Probed in parallel, on the pool, because a candidate is almost
        # entirely waiting. One unreachable address costs a PING timeout,
        # then up to PUNCH_ATTEMPTS relayed punches, then a direct one,
        # each with its own wait: about fifty seconds of nothing happening.
        # Run one after another on this thread, as this was, a batch from a
        # DHT round could hold the discovery loop for many minutes, and
        # that loop is also what expires dead peers, processes DHT alerts,
        # re-broadcasts on the LAN and saves the peer cache. All of it
        # stopped while the node waited on addresses that were never going
        # to answer.
        results = list(self._executor.map(self._try_candidate, fresh))
        admitted = sum(1 for ok in results if ok)

        if admitted:
            log.info("[peers] connected to %d of them, %d peer(s) in total",
                     admitted, self.pool.count())
        else:
            log.info("[peers] none of those %d could be reached", len(fresh))

    def _try_candidate(self, addr: str) -> bool:
        """Everything we will try to reach one candidate: a plain ping, then
        a punch relayed through peers we already have, then a direct punch.
        Returns whether it ended up admitted."""
        if self.pool.count() >= self.pool._max_peers:
            return False
        if self._ping_and_admit(addr):
            return True

        for relay in self.pool.get_all()[:PUNCH_ATTEMPTS]:
            if self._punch_and_admit(relay, addr):
                return True

        # No relay worked. Fire UDP bursts directly and re-ping. Both nodes
        # discover each other via DHT simultaneously, so both will fire
        # toward each other at roughly the same time, which is sufficient to
        # open symmetric NAT holes without a relay.
        log.debug("[peer] no relay, direct punch  addr=%s", addr)
        self.udp.punch_direct(addr)
        time.sleep(PUNCH_WAIT)
        if self._ping_and_admit(addr):
            return True
        log.debug("[peer] unreachable (no punch)  addr=%s", addr)
        return False

    def _ping_and_admit(self, addr: str) -> bool:
        """UDP PING addr. If PONG arrives, exchange peers and admit. Returns True on success."""
        observed = self.udp.ping(addr)
        if observed is None:
            return False
        # PONG received; node is reachable
        if self.pool.add(addr):
            log.info("[peers] connected to %s, %d peer(s) in total",
                 addr, self.pool.count())
            # Exchange peer lists
            self.udp.send_peers(addr, self.pool.get_all()[:50])
        return True

    def _punch_and_admit(self, relay: str, target: str) -> bool:
        """Ask relay to coordinate hole punch to target, then re-ping."""
        log.debug("[peer] punch attempt  relay=%s  target=%s", relay, target)
        self.udp.punch_via(relay, target)
        time.sleep(PUNCH_WAIT)
        return self._ping_and_admit(target)

    # ------------------------------------------------------------------
    # IP fallback (only used if no PONG has arrived yet)
    # ------------------------------------------------------------------

    def _fallback_ip(self) -> str | None:
        """Last-resort public IP via HTTP. Only used before first PONG."""
        try:
            import requests
            for url in ("https://api.ipify.org",
                        "https://icanhazip.com",
                        "https://checkip.amazonaws.com"):
                try:
                    r = requests.get(url, timeout=5)
                    if r.status_code == 200:
                        ip = r.text.strip()
                        if ip:
                            log.debug("[ip] public IP via %s: %s", url, ip)
                            return ip
                except Exception:
                    pass
        except ImportError:
            pass
        return None

    # ------------------------------------------------------------------
    # Peer cache
    # ------------------------------------------------------------------

    def _load_peer_cache(self):
        try:
            with open(PEER_CACHE_FILE) as f:
                peers = json.load(f)
            log.info("[peers] remembered %d address(es) from last run", len(peers))
            for addr in peers:
                self.enqueue_candidate(addr)
        except FileNotFoundError:
            pass
        except Exception:
            log.debug("[peer] cache load failed", exc_info=True)

    def _save_peer_cache(self):
        peers = self.pool.all_addrs()
        try:
            with open(PEER_CACHE_FILE, "w") as f:
                json.dump(peers, f)
        except Exception:
            log.debug("[peer] cache save failed", exc_info=True)
