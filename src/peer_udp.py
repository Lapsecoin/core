"""UDP peer transport: the single socket that replaces all HTTP peer communication.

One UDP socket per node handles everything:
  - PING/PONG    : reachability check + NAT-observed public IP discovery
  - PEERS        : peer list exchange
  - BLOCK        : block gossip
  - TX           : transaction gossip (Dandelion stem/fluff)
  - GETSYNC      : request chain segment
  - SYNC         : chain segment response (chunked for large syncs)
  - PUNCH_REQ    : ask relay to coordinate a hole punch to a third peer
  - PUNCH_GO     : relay telling us to punch toward a peer right now

Reliability layer
-----------------
Large messages (SYNC, full block) are chunked into MAX_CHUNK_SIZE UDP datagrams.
Each chunk is numbered; for a genuinely multi-chunk message (chunk_total > 1),
the receiver ACKs its current chunk holdings (MT_ACK) after every chunk it
gets, and the sender resends only what's still missing, for up to
CHUNK_ACK_MAX_ROUNDS rounds (see _send_chunked / _retransmit_until_acked /
_handle_ack). A message still stuck incomplete after that, or one whose
reassembly buffer just goes stale from real inactivity, is dropped
(_Reassembler.evict_stale), same as before. Talking to a peer too old to
know MT_ACK exists degrades safely to exactly that prior behavior: no ACK
ever arrives, every round resends the whole message (wasteful but harmless,
chunks are idempotent to re-receive), and it's abandoned once
CHUNK_ACK_MAX_ROUNDS is exhausted rather than hanging. The caller's own
retry cadence (periodic syncer re-poll, gossip re-broadcast) is still the
backstop above this layer, same as always.
Small messages (PING, PEERS, TX, small blocks) fit in one datagram, stay
pure fire-and-forget (no ACK tracking overhead), with application-level
retry handled by the caller.

Wire format
-----------
All datagrams: [1 byte msg_type][2 byte chunk_id][2 byte chunk_total][payload]
If chunk_total == 1 the message is not chunked (fire-and-forget).
Payload is msgpack-encoded for compactness (falls back to json).

Public interface
----------------
  UDPTransport(host, port, genesis_hash, on_block, on_tx, on_peers, pool)
  .start()                    bind socket, start recv loop thread
  .stop()
  .ping(addr)                 fire-and-forget
  .send_block(block, peers, stemming)  put a block on the wire
  .send_tx(tx, peers, stemming)       put a tx on the wire
     Neither relays on the receiver's behalf: what to forward, to whom,
     and whether it is still private is decided in gossip.py after Node
     has validated the item.
  .request_sync(addr, from_h) request chain from peer, returns list|None
  .send_peers(addr, peers)    send peer list to addr
  .punch_via(relay, target)   ask relay to coordinate punch to target
  .broadcast_discover()       announce our data port on the shared
                               LAN_DISCOVERY_PORT; peers with the same
                               genesis on this network segment find and
                               ping us back regardless of their own data
                               port, no DHT/NAT/punching needed
  .our_external_addr          best-known external ip:port (str or None)

Module-level:
  probe_lan_ports(genesis_hash)   standalone, run before choosing a data
                                   port: returns ports other nodes on this
                                   LAN are already using, so a second
                                   machine here doesn't default onto one
                                   already claimed
"""

import ipaddress
import logging
import secrets
import select
import socket
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import msgpack

from params import BLOCK_SIZE_LIMIT

log = logging.getLogger("ec.udp")

# Message type constants
MT_PING      = 0x01
MT_PONG      = 0x02
MT_PEERS     = 0x03
MT_TX        = 0x05
MT_GETSYNC   = 0x06
MT_SYNC      = 0x07
MT_ACK       = 0x08
MT_PUNCH_REQ = 0x09
MT_PUNCH_GO  = 0x0A
MT_GETINFO   = 0x0B   # request peer tip info (height + hash)
MT_INFO      = 0x0C   # response: {"height": N, "tip_hash": "...", "wallet": "..."}
# Blocks, zlib'd. 0x04 was the uncompressed form and is retired rather
# than kept alongside: a datagram still carrying it matches no branch and
# is ignored, which is the point. Supporting both indefinitely is how a
# codebase accumulates a path per format change forever, and the protocol
# floor below is what makes retiring one safe instead of silent.
MT_BLOCK     = 0x0D

# Level 1 rather than 6: on the wire this competes with pacing, not disk.
# It reaches within about a point of the ratio at a third of the CPU, and
# every hop pays the decompress, so the cheaper end is the right one here.
BLOCK_COMPRESS_LEVEL = 1

# Protocol floor. Same job genesis_hash already does, one step along: that
# one refuses a peer on a different network, this one refuses a peer too
# old to speak this network's current wire format. Having it in the
# handshake is what lets a format be replaced instead of accumulated,
# because the check lives in one place rather than once per message type.
#
# Bump both when the wire changes incompatibly, and delete the old format
# in the same commit. A peer below the floor is not partially supported;
# it is not peered with, and it needs to update.
#
# NOT for a validation rule that only tightens what this node itself
# accepts (tx.py's field whitelist and output-count cap, the case that
# briefly moved this to 3): that protection lives entirely in
# tx.validate()/block.validate(), runs regardless of what any peer's
# version is, and isn't weakened by this number at all. Bumping the
# floor for it anyway meant refusing to peer with anyone still on the
# old code at all, over a shape of transaction nobody had actually
# sent, which found the immediate real cost (two live nodes cut off from
# each other) trading against a purely speculative future one (a fork
# that only happens once someone crafts that transaction). A real floor
# bump is still the right tool for an actual wire-format change, or once
# a validation tightening's fork risk is no longer speculative; it
# should be its own deliberate, announced step then, not folded silently
# into an unrelated feature commit the way this one was.
PROTOCOL_VERSION     = 2
MIN_PROTOCOL_VERSION = 2

MAX_CHUNK_SIZE   = 1400   # bytes, safe below MTU
RECV_TIMEOUT     = 2.0    # seconds select/recvfrom timeout
SYNC_TIMEOUT     = 30.0   # seconds to wait for a full sync response
PING_TIMEOUT     = 8.0    # seconds to wait for PONG

# LAN discovery: a small fixed port every node also listens on, separate
# from its actual data port (self.port, which may be anything, two nodes
# behind the same router commonly use different ports on purpose, since a
# router can only port-forward one external port to one internal machine).
# Announcing "I'm on port X" over this shared, well-known port lets nodes on
# the same network segment find each other regardless of what data port
# either one runs on.
# Deliberately well outside the 8333-and-a-few-up range a data port ends up
# in after PORT_BIND_RETRIES fallback, so the two can never collide with
# each other on one machine.
LAN_DISCOVERY_PORT = 18334

PORT_BIND_RETRIES = 5   # how many ascending ports to try if the requested one is taken

# The PING burst that opens a NAT hole: enough datagrams, spaced widely
# enough, to cover the gap between when we start punching and when the
# other side does. See UDPTransport._punch_burst.
PUNCH_BURST_COUNT   = 8
PUNCH_BURST_SPACING = 0.05

# Caps against a spoofed-source amplification attack: an attacker who forges
# a peer's source address in a GETSYNC and requests the whole chain would
# otherwise turn one small datagram into a multi-MB reply blasted at the
# victim. This bounds how many blocks one request can pull.
MAX_SYNC_BLOCKS   = 500

# Envelope room on top of a block: the genesis hash, the stemming flag, and
# msgpack's own framing. Small and fixed, but the ceilings below have to
# clear it or a block exactly at the consensus limit misses by a few bytes.
MESSAGE_ENVELOPE_BYTES = 4096

# The largest message this transport undertakes to carry, derived from the
# consensus block limit rather than picked independently of it.
#
# These two numbers used to be chosen on their own, and they landed under
# BLOCK_SIZE_LIMIT: at 2000 chunks the wire carried 2.8MB, real blocks
# compress about 1.79x (FALCON signatures are random and do not compress),
# so anything past roughly 5MB could be built and validated by everyone
# and received by nobody. The receiver dropped it with no log line and the
# builder had no way to find out, so half the block limit was unusable and
# silently so. Deriving them here is what stops the two drifting apart
# again: raise BLOCK_SIZE_LIMIT and the transport follows.
#
# Sized for the worst case rather than the typical one. Compression is not
# guaranteed to help (a block full of signatures barely compresses, and a
# hostile sender can make sure of it), so the chunk count assumes a payload
# that did not shrink at all.
MAX_MESSAGE_BYTES = BLOCK_SIZE_LIMIT + MESSAGE_ENVELOPE_BYTES
MAX_CHUNK_TOTAL   = -(-MAX_MESSAGE_BYTES // MAX_CHUNK_SIZE)

# Ceiling on decompressing an inbound payload, see _inflate. Two bounds,
# because one number cannot do this job. The ratio stops a small payload
# from expanding without limit, which is the actual bomb: an attacker has
# to spend proportionally to what we allocate. The absolute cap stops a
# large one, since a ratio alone would license hundreds of MB from a
# message the wire already permits. Real traffic is nowhere near either: a
# block expands about 1.8x and the best case measured, a page of
# near-identical empty blocks, about 12x.
MAX_INFLATE_RATIO = 200                 # vs ~1.8x for a block, ~12x for a page
MAX_INFLATE_BYTES = MAX_MESSAGE_BYTES

# Total bytes held across every in-flight reassembly, all senders together.
#
# The per-message ceiling above is now large enough that it cannot be the
# only bound: nothing has ever capped how many partial messages are held
# at once, so at MAX_CHUNK_TOTAL a peer table could pin gigabytes by
# starting a message each and never finishing it. That was survivable only
# because a single message was capped at 2.8MB. Bounded here instead, so
# raising the per-message ceiling costs memory that is still bounded in
# total. A refused chunk is not a refused message: the sender's own ACK
# loop resends, and by then the stale buffers have aged out.
REASSEMBLY_MAX_BYTES = 64 * 1024 * 1024

# Per-source-IP token bucket: caps how many datagrams/sec one address can
# push into the worker pool, so a flood (PING or otherwise) from one sender
# can't starve processing of legitimate traffic from everyone else.
RATE_LIMIT_PER_SEC = 50
RATE_LIMIT_BURST    = 100

# Chunk-level ACK/retransmit for multi-chunk sends (chunk_total > 1 only,
# most messages fit in one datagram and stay pure fire-and-forget). The
# receiver ACKs its current chunk holdings after every chunk it gets; the
# sender resends only what's still missing, a few times, then gives up.
# Against a peer too old to know MT_ACK exists, no ACK ever arrives, so
# every round resends the whole message, wasteful but harmless (chunks
# are idempotent to re-receive) and still bounded by CHUNK_ACK_MAX_ROUNDS,
# degrading to exactly today's one-shot fire-and-forget behavior once
# exhausted, never hanging.
CHUNK_ACK_TIMEOUT    = 1.5   # seconds to wait for acks before a retransmit round
CHUNK_ACK_MAX_ROUNDS = 4

# Return-routability confirmation for sync requests, see
# UDPTransport._may_serve_sync. A confirmed address is remembered for a
# while so a multi-page sync is not a PING per page; the window is short
# because an address can stop being reachable at any time and the only
# cost of re-confirming is one round trip.
ROUTABLE_TTL_SECONDS  = 300
ROUTABLE_PING_TIMEOUT = 3.0
# Concurrent confirmations in flight. Bounds what a flood of forged source
# addresses can make this node do: past this, sync requests from unknown
# addresses are dropped rather than each starting a PING of its own.
ROUTABLE_MAX_PENDING  = 32

# Workers for the waiter pool (see UDPTransport._waiters). Each one is asleep
# on an Event for nearly all of its life, so this is sized by how many
# chunked sends can plausibly be in flight (a fan-out per peer, plus sync
# replies) rather than by anything to do with CPU.
ACK_WAITER_THREADS = 64

# A receiver acks its chunk holdings while a multi-chunk message arrives.
# Acking after every single chunk, which is what this did, makes the ack
# traffic quadratic in chunk count: a 1300-chunk block drew 1300 acks
# carrying an average of 650 indices each, so the acks competed for the
# same path as the chunks they were acknowledging. Acking every N chunks
# (and always on the last one) keeps the retransmit loop fed with the same
# information at a small fraction of the datagrams. The format is
# unchanged, so a peer on either side of this change still understands the
# other; it only sends fewer.
ACK_EVERY_CHUNKS = 32

# How many peers a multi-chunk message is sent to at once.
#
# A chunked send paces itself (see _send_chunked), so sending to peers one
# after another made the last peer wait for every peer before it: a 50-tx
# block is 121 chunks, so a fan-out to 8 peers spent about 4.8 seconds
# asleep, per level of the broadcast tree. Overlapping them removes that
# multiplier without touching the rate on any single path, so the drop
# risk each receiver sees is exactly what it was.
#
# Bounded rather than unlimited because it does multiply this node's own
# outbound burst: each concurrent send is ~280 KB/s, which is nothing on a
# server and not nothing on a home uplink, and a drop costs a 1.5s
# retransmit round, three hundred times the sleep it would save.
FANOUT_CONCURRENCY = 4

# How many sends may be waiting on that pool before new ones are dropped.
#
# ThreadPoolExecutor queues into a SimpleQueue, which has no bound at all,
# so without this a burst just accumulates, each entry holding a whole
# block. Memory is the lesser problem: a send that leaves the queue thirty
# seconds late is spending bandwidth on a block the network already has,
# so queuing it is worse than dropping it. Gossip is best-effort and our
# own blocks have a retry above this (Node._retry_unconfirmed_spreads),
# which is what makes dropping the right answer rather than a compromise.
#
# Sized at roughly two full fan-outs at the peer cap, so a node is only
# ever dropping when it is genuinely more than a block behind on sending.
FANOUT_PENDING_MAX = 256

# Header: 1 (type) + 4 (msg_id) + 2 (chunk_idx) + 2 (chunk_total) = 9 bytes
HDR_FMT  = "!BIHh"  # chunk_total is signed, but MT_ACK is what actually
# distinguishes an ack datagram (see _handle_ack). It's just an ordinary
# single-chunk (chunk_total=1) message of that type, no sentinel value needed.
HDR_SIZE = struct.calcsize(HDR_FMT)


def _local_ips() -> set[str]:
    """Best-effort set of this machine's own IPs, so a broadcast that loops
    back to the sending host (common: cloud instances with a private VPC IP
    behind a public/elastic one, e.g. AWS's 172.31.x.x, get their own
    broadcast delivered right back) never gets treated as a discovered
    peer."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return ips


def _broadcast_from_all_interfaces(payload: bytes, port: int):
    """Send payload to the LAN broadcast address once per local interface.

    A single unbound (0.0.0.0) broadcast send leaves the OS's routing table
    to pick the egress interface via the default route, on a real machine
    that's very often *not* the LAN adapter, since any VPN client, Docker,
    Hyper-V, or VirtualBox virtual adapter routinely takes that spot. The
    broadcast then goes out silently nowhere useful, no error either side.
    Binding a send explicitly to each real local IP forces it out that
    specific interface, sidestepping the ambiguity entirely."""
    for ip in _local_ips():
        if ip.startswith("127."):
            continue
        # closesocket via `with`, not a close() after the send: this runs on
        # every discovery round, and a broadcast that fails is the norm, not
        # the exception, on a host whose network does not carry one (a cloud
        # VPC, most notably, where sendto raises ENETUNREACH every time). A
        # close() placed after the send is skipped on exactly that path, and
        # leaving the descriptor to be reclaimed by refcounting is not
        # something to rely on when the failing path is the common one.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, 0))
                s.sendto(payload, ("255.255.255.255", port))
        except OSError:
            log.debug("[udp] broadcast via %s failed", ip, exc_info=True)


def _is_lan_source(host: str) -> bool:
    """True for a private, non-loopback address, i.e. one that could only
    have reached us over the local network, never routed from the public
    internet. Loopback is excluded so a node never treats its own broadcast
    echo (same host, different process) as a peer."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def _encode(data: dict) -> bytes:
    return msgpack.packb(data, use_bin_type=True)


def _decode(raw: bytes) -> dict:
    return msgpack.unpackb(raw, raw=False)


def probe_lan_ports(genesis_hash: str, wait: float = 1.5,
                    disc_port: int = LAN_DISCOVERY_PORT) -> set[int]:
    """Ask the local network "who's already running a node here" before
    picking a data port to bind, so a second machine on the same LAN
    doesn't default to a port another machine there is already using.
    Each still keeps its own distinct, independently port-forwardable
    port. Standalone (no running UDPTransport needed): main.py calls this
    before it has even chosen its own port yet.

    Sends and listens on one socket per local interface (see
    _broadcast_from_all_interfaces's docstring for why a single unbound
    socket isn't reliable), so a reply reaching any of this machine's real
    interfaces is caught regardless of which one the OS would have picked
    by default.

    Returns whatever data ports currently-running nodes with the same
    genesis reply with. Best-effort: an empty result just means "nobody
    answered in time" or broadcast doesn't reach on this network, never
    a reason to fail startup."""
    found: set[int] = set()
    ifaces = [ip for ip in _local_ips() if not ip.startswith("127.")] or ["0.0.0.0"]
    socks = []
    for ip in ifaces:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            socks.append(s)
        except OSError:
            log.debug("[udp] LAN port probe socket setup failed for %s", ip, exc_info=True)
    if not socks:
        return found

    def _send_probe():
        payload = _encode({"type": "probe", "genesis": genesis_hash})
        for s in socks:
            try:
                s.sendto(payload, ("255.255.255.255", disc_port))
            except OSError:
                log.debug("[udp] LAN port probe send failed", exc_info=True)

    try:
        # UDP has no delivery guarantee even on a fully working LAN, a
        # single dropped broadcast would otherwise look identical to "no
        # other node here". Re-send a couple more times across the wait
        # window rather than betting the whole check on one packet; replies
        # are naturally deduplicated since found is a set.
        RESEND_COUNT = 3
        _send_probe()
        deadline = time.monotonic() + wait
        next_resend = time.monotonic() + wait / RESEND_COUNT
        resends_left = RESEND_COUNT - 1

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if resends_left > 0 and time.monotonic() >= next_resend:
                _send_probe()
                resends_left -= 1
                next_resend += wait / RESEND_COUNT
            try:
                readable, _, _ = select.select(socks, [], [], min(remaining, 0.2))
            except OSError:
                break
            for s in readable:
                try:
                    data, sender = s.recvfrom(2048)
                except OSError:
                    continue
                try:
                    parsed = _decode(data)
                except Exception:
                    continue
                if not isinstance(parsed, dict) or parsed.get("genesis") != genesis_hash:
                    continue
                if not _is_lan_source(sender[0]):
                    continue
                port = parsed.get("port")
                if isinstance(port, int) and 0 < port <= 65535:
                    found.add(port)
    finally:
        for s in socks:
            s.close()
    return found


def _protocol_ok(data: dict) -> bool:
    """Whether a handshake came from a peer we can actually talk to.

    A peer too old to carry the field at all reads as 0, which is the
    correct answer for it: absent means it predates the floor.
    """
    try:
        return int(data.get("proto", 0)) >= MIN_PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


def _inflate(payload: bytes):
    """Decompress an MT_BLOCK payload, or None if it isn't usable.

    Bounded on purpose. A message is already capped at MAX_CHUNK_TOTAL
    chunks on the wire, but compression breaks the link between what an
    attacker sends and what we allocate: a couple of megabytes of
    well-chosen input expands to gigabytes. Holding the decompressed form
    to the same ceiling an uncompressed message has means a compressed
    block can never cost us more memory than the plain one it stands in
    for, so this adds no new way to exhaust a node.
    """
    # Two bounds, because one number cannot do this job. The ratio stops a
    # small payload from expanding without limit, which is the actual bomb:
    # an attacker has to spend proportionally to what we allocate. The
    # absolute cap stops a large one, since MAX_CHUNK_TOTAL already lets
    # 2.8MB on the wire and a ratio alone would license hundreds of MB from
    # it. Real traffic is nowhere near either: a block expands about 1.8x
    # and the best case measured, a page of near-identical empty blocks,
    # about 12x.
    limit = min(len(payload) * MAX_INFLATE_RATIO, MAX_INFLATE_BYTES)
    try:
        obj = zlib.decompressobj()
        out = obj.decompress(payload, limit)
        if obj.unconsumed_tail:
            log.debug("[udp] compressed block over the size ceiling, dropped")
            return None
        return out
    except zlib.error:
        log.debug("[udp] undecompressable block payload, dropped")
        return None


def _pack(msg_type: int, msg_id: int, chunk_idx: int,
          chunk_total: int, payload: bytes) -> bytes:
    hdr = struct.pack(HDR_FMT, msg_type, msg_id, chunk_idx, chunk_total)
    return hdr + payload


def _unpack(data: bytes):
    if len(data) < HDR_SIZE:
        return None
    msg_type, msg_id, chunk_idx, chunk_total = struct.unpack_from(HDR_FMT, data)
    payload = data[HDR_SIZE:]
    return msg_type, msg_id, chunk_idx, chunk_total, payload


def _split(payload: bytes):
    """Split payload into chunks. Returns list of bytes."""
    chunks = []
    for i in range(0, max(len(payload), 1), MAX_CHUNK_SIZE):
        chunks.append(payload[i:i + MAX_CHUNK_SIZE])
    return chunks


class _Partial:
    """One message being reassembled. Chunks live in their own dict rather
    than alongside bookkeeping keys: the previous version kept both in one
    dict and tested completeness with `len(rec) - 2 == rec["total"]`, which
    counts keys rather than checking that the ones it is about to read are
    actually there. A sender claiming three chunks and sending indices
    10, 11, 12 satisfied that count and then raised KeyError joining
    range(3), inside a worker whose exception nobody sees, leaving the
    buffer behind. Indices are range-checked on the way in now, and
    completeness is a plain count of what was stored."""

    __slots__ = ("total", "ts", "chunks", "nbytes")

    def __init__(self, total):
        self.total  = total
        self.ts     = time.monotonic()
        self.chunks = {}
        self.nbytes = 0

    def add(self, idx, payload):
        previous = self.chunks.get(idx)
        if previous is not None:
            self.nbytes -= len(previous)
        self.chunks[idx] = payload
        self.nbytes += len(payload)

    def complete(self):
        return len(self.chunks) == self.total

    def join(self):
        return b"".join(self.chunks[i] for i in range(self.total))


class _Reassembler:
    """Reassemble chunked messages per (sender_addr, msg_id)."""

    def __init__(self, max_bytes=REASSEMBLY_MAX_BYTES):
        self._pending  = {}   # (addr, msg_id) -> _Partial
        self._lock     = threading.Lock()
        self._max_bytes = max_bytes
        self._held     = 0    # total bytes across every _Partial

    def feed(self, addr, msg_id, chunk_idx, chunk_total, payload):
        """Return complete payload bytes when all chunks arrive, else None."""
        if chunk_total == 1:
            return payload  # single-chunk, no reassembly needed
        if chunk_total <= 0 or chunk_total > MAX_CHUNK_TOTAL:
            # Not silent any more. This is exactly how a block too large for
            # the transport used to disappear: refused here, no log, and the
            # builder left to wonder why nobody took its height.
            log.debug("[udp] refusing %s: claims %d chunks, cap is %d",
                      addr, chunk_total, MAX_CHUNK_TOTAL)
            return None
        if not 0 <= chunk_idx < chunk_total:
            log.debug("[udp] refusing %s: chunk %d outside a %d-chunk message",
                      addr, chunk_idx, chunk_total)
            return None

        key = (addr, msg_id)
        with self._lock:
            rec = self._pending.get(key)
            if rec is None:
                if self._held + len(payload) > self._max_bytes:
                    log.debug("[udp] reassembly buffer full (%d bytes), "
                              "dropping a new message from %s", self._held, addr)
                    return None
                rec = _Partial(chunk_total)
                self._pending[key] = rec
            elif self._held + len(payload) > self._max_bytes:
                log.debug("[udp] reassembly buffer full (%d bytes), "
                          "dropping a chunk from %s", self._held, addr)
                return None
            before = rec.nbytes
            rec.add(chunk_idx, payload)
            self._held += rec.nbytes - before
            if rec.complete():
                full = rec.join()
                del self._pending[key]
                self._held -= rec.nbytes
                return full
        return None

    def evict_stale(self, max_age=60.0):
        cutoff = time.monotonic() - max_age
        with self._lock:
            stale = [k for k, v in self._pending.items() if v.ts < cutoff]
            for k in stale:
                self._held -= self._pending[k].nbytes
                del self._pending[k]

    def held_chunks(self, addr, msg_id):
        """Chunk indices currently held for (addr, msg_id), for ACKing.
        Returns [] once the message has completed (feed() already deleted
        the entry). Callers must special-case the completing feed() call
        themselves if they need to ACK all indices in that case."""
        with self._lock:
            rec = self._pending.get((addr, msg_id))
            return list(rec.chunks) if rec is not None else []

    def held_bytes(self):
        with self._lock:
            return self._held


class _PendingSync:
    """Collects SYNC chunks for a specific GETSYNC request."""

    def __init__(self):
        self.chunks  = {}   # chunk_idx -> payload
        self.total   = None
        self.event   = threading.Event()
        self.result  = None   # set when complete

    def feed(self, chunk_idx, chunk_total, payload):
        if chunk_total <= 0 or chunk_total > MAX_CHUNK_TOTAL:
            return  # bogus or oversized claim from a malicious sync peer
        if not 0 <= chunk_idx < chunk_total:
            # Same range check the reassembler now makes, and it matters
            # more here: joining range(total) over out-of-range indices
            # raised KeyError inside the receive worker, so the event was
            # never set and request_sync sat out its full 30s timeout for
            # an answer that had already arrived and been thrown away.
            return
        self.total = chunk_total
        self.chunks[chunk_idx] = payload
        if len(self.chunks) == chunk_total:
            full = b"".join(self.chunks[i] for i in range(chunk_total))
            # Same ceiling and same reasoning as a block's, see _inflate:
            # a sync response is the largest thing on this wire, which is
            # exactly why it must not be able to inflate past what an
            # uncompressed one could have been.
            full = _inflate(full)
            try:
                self.result = _decode(full) if full is not None else None
            except Exception:
                self.result = None
            self.event.set()


class UDPTransport:

    def __init__(self, port, genesis_hash, on_block, on_tx, on_peers, pool):
        self.port         = port
        self.genesis_hash = genesis_hash
        self._on_block    = on_block
        self._on_tx       = on_tx
        self._on_peers    = on_peers
        self._pool        = pool

        self._sock        = None
        self._running     = False
        self._reassembler = _Reassembler()
        self._pending_sync: dict[int, _PendingSync] = {}  # msg_id -> _PendingSync
        self._sync_lock   = threading.Lock()
        # (target_addr, msg_id) -> {"chunks": {idx: bytes}, "acked": set(),
        # "msg_type": int, "event": Event}. Keyed by target too, not just
        # msg_id, because a broadcast (send_block) reuses one
        # msg_id across many targets, keying by msg_id alone would let
        # concurrent targets clobber each other's ack tracking.
        self._pending_chunked_sends: dict[tuple, dict] = {}
        self._chunk_lock  = threading.Lock()
        self._pong_events: dict[int, threading.Event] = {}
        self._pong_addrs: dict[int, str] = {}  # msg_id -> observed addr
        self._pong_lock   = threading.Lock()
        self._info_events: dict[int, threading.Event] = {}  # msg_id -> event
        self._info_results: dict[int, dict] = {}            # msg_id -> info dict
        self._info_lock   = threading.Lock()

        self.our_external_addr: str | None = None  # set from PONG responses
        self._ext_addr_votes: dict[str, set] = {}  # observed addr -> {voter ip, ...}
        self._seen_msg: dict[int, float] = {}      # msg_id -> ts for dedup
        self._seen_lock = threading.Lock()
        self._rate_buckets: dict[str, list] = {}   # source ip -> [tokens, last_refill]
        self._rate_lock   = threading.Lock()
        self._executor  = ThreadPoolExecutor(max_workers=16, thread_name_prefix="udp-cb")
        # Separate from _executor deliberately: that one dispatches inbound
        # datagrams, and a paced fan-out occupies a worker for as long as the
        # pacing lasts. Sharing would let one large block's fan-out starve
        # the reading of everything arriving while it goes out.
        self._fanout    = ThreadPoolExecutor(max_workers=FANOUT_CONCURRENCY,
                                             thread_name_prefix="udp-fan")
        self._fanout_slots = threading.BoundedSemaphore(FANOUT_PENDING_MAX)
        # And separate again for the ack waiters, for the same reason one
        # step further on. _send_chunked used to put _retransmit_until_acked
        # on _executor, which is precisely the sharing the comment above
        # forbids: a waiter holds its worker for up to
        # CHUNK_ACK_MAX_ROUNDS * CHUNK_ACK_TIMEOUT seconds against a peer
        # that never acks, and one transaction fluffed to a full peer table
        # queues one per peer. Sixteen of those and the node stops reading
        # its socket entirely, right when it is being told about the block
        # it is racing. These threads spend their lives asleep on an Event,
        # so there can be many more of them than there are cores.
        self._waiters   = ThreadPoolExecutor(max_workers=ACK_WAITER_THREADS,
                                             thread_name_prefix="udp-wait")
        # Addresses that have proven they can receive at the address they
        # claim, and the confirmations currently in flight. See
        # _may_serve_sync.
        self._routable: dict[str, float] = {}
        self._routable_pending: set = set()
        self._routable_lock = threading.Lock()
        self._on_punch_go   = None  # set by discovery after init
        self._get_tip_fn    = None  # set by main after node init
        self._on_peer_hint  = None  # set by discovery; called with candidate addrs from a PING
        self._local_ips: set[str] = set()  # populated in start(); guards against self-admit
        self._disc_sock    = None  # LAN discovery broadcast/listen socket (LAN_DISCOVERY_PORT)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for candidate in range(self.port, self.port + PORT_BIND_RETRIES):
            try:
                self._sock.bind(("0.0.0.0", candidate))
                break
            except OSError:
                if candidate == self.port + PORT_BIND_RETRIES - 1:
                    raise
        if self._sock.getsockname()[1] != self.port:
            log.warning("[udp] port %d in use, bound to %d instead",
                        self.port, self._sock.getsockname()[1])
            self.port = self._sock.getsockname()[1]
        self._sock.settimeout(RECV_TIMEOUT)
        self._local_ips = _local_ips()
        self._running = True
        t = threading.Thread(target=self._recv_loop, daemon=True, name="udp-recv")
        t.start()
        log.info("[udp] listening on 0.0.0.0:%d", self.port)
        self._start_lan_discovery()

    def _start_lan_discovery(self):
        """Best-effort: a second small socket on the shared, fixed
        LAN_DISCOVERY_PORT, decoupled from self.port (which may differ
        between two nodes on the same network on purpose). Failing to bind
        it (e.g. another local process already holds it) just means no LAN
        auto-discovery for this node. Never fatal."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", LAN_DISCOVERY_PORT))
            sock.settimeout(RECV_TIMEOUT)
        except OSError:
            log.debug("[udp] LAN discovery port %d unavailable, skipping",
                      LAN_DISCOVERY_PORT, exc_info=True)
            return
        self._disc_sock = sock
        threading.Thread(target=self._disc_recv_loop, daemon=True,
                         name="udp-lan-disc").start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._disc_sock:
            try:
                self._disc_sock.close()
            except Exception:
                pass
        self._executor.shutdown(wait=False)
        self._fanout.shutdown(wait=False)
        self._waiters.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Public send operations
    # ------------------------------------------------------------------

    def ping(self, addr: str, timeout: float = PING_TIMEOUT) -> str | None:
        """Send PING, wait for PONG. Returns observed external addr or None."""
        host, port = addr.rsplit(":", 1)
        target = (host, int(port))
        msg_id = self._new_msg_id()
        ev = threading.Event()
        with self._pong_lock:
            self._pong_events[msg_id] = ev
        payload = {"genesis": self.genesis_hash, "proto": PROTOCOL_VERSION}
        if self.our_external_addr:
            payload["from"] = self.our_external_addr
        self._send_one(MT_PING, msg_id, payload, target)
        if ev.wait(timeout):
            with self._pong_lock:
                result = self._pong_addrs.pop(msg_id, None)
                self._pong_events.pop(msg_id, None)
            # Vote on our external address. Require agreement from 2 distinct
            # voters that are already admitted to the peer pool. Otherwise
            # two throwaway addresses answering our own outbound PINGs could
            # feed a false "observed" address before ever being trusted.
            if result:
                voters = self._ext_addr_votes.setdefault(result, set())
                if addr in self._pool.all_addrs():
                    voters.add(addr)
                if len(voters) >= 2:
                    self.our_external_addr = result
                elif self.our_external_addr is None:
                    self.our_external_addr = result  # tentative until confirmed
            return result
        with self._pong_lock:
            self._pong_events.pop(msg_id, None)
        return None

    def send_block(self, block: dict, peers=None, stemming: bool = False):
        """Send a block to `peers` (default: all).

        stemming marks the private phase, so the receiver knows to apply the
        stem rule rather than treat it as public. Propagation decisions live
        entirely in gossip.py. This layer only puts bytes on the wire, and
        deliberately does not relay on the receiver's behalf (see the
        MT_BLOCK branch in _dispatch)."""
        log.debug("[udp] send_block height=%s stem=%s",
                  block.get("height"), stemming)
        self._broadcast(MT_BLOCK, {"block": block}, peers, stemming, "block")

    def send_tx(self, tx: dict, peers=None, stemming: bool = False):
        """Send a tx to `peers` (default: all). stemming as in send_block."""
        # Chunked, not a single datagram. A transaction carries a FALCON
        # signature and key, so it is ~3.4KB and was going out as one
        # oversized datagram that IP-fragments into three, every one of
        # which has to survive or the transaction is lost. Fragmented UDP
        # is exactly what NAT and middleboxes drop, so this was quietly
        # costing transaction propagation. Compressed for the same reason
        # blocks are, which also takes it to two chunks rather than three.
        self._broadcast(MT_TX, {"tx": tx}, peers, stemming, "transaction")

    def _broadcast(self, msg_type: int, body: dict, peers, stemming: bool,
                   what: str):
        """Compress `body` once and put it on the wire to every peer.

        One path for blocks and transactions, which were two copies of this
        with one difference that turned out to be a bug: the block copy
        handed a paced send to the fan-out pool, and the transaction copy
        did not. A compressed transaction is two chunks, so it paced, so
        fluffing one walked the peer table on the caller's own thread at
        5ms a chunk. That caller is the node loop, which means a hundred
        peers cost it about a second of not draining its queue, per
        transaction, for no reason the block path did not already have the
        answer to.
        """
        if peers is None:
            peers = self._pool.get_all()
        if not peers:
            return
        payload = zlib.compress(
            _encode({"genesis": self.genesis_hash, "stemming": stemming, **body}),
            BLOCK_COMPRESS_LEVEL)
        msg_id = self._new_msg_id()
        self._mark_seen(msg_id)
        targets = [self._addr_tuple(a) for a in peers]
        if len(payload) <= MAX_CHUNK_SIZE:
            # One datagram does no pacing at all, so there is nothing to get
            # off this thread and a pool would only add scheduling to it.
            for target in targets:
                self._send_chunked(msg_type, msg_id, payload, target)
            return
        # Anything paced goes to the pool even when it is going to a single
        # peer. Overlapping peers was only half the point; the other half is
        # that the caller here is the node loop, and a stem hop sends to
        # exactly one peer, so gating this on peer count left the most
        # latency-sensitive path in the system, ten sequential hops of it,
        # still blocking that loop for the whole pacing.
        for target in targets:
            if not self._fanout_slots.acquire(blocking=False):
                log.warning("[udp] send queue full, dropping %s to %s "
                            "(it will be re-sent if nobody echoes it back)",
                            what, target)
                continue
            try:
                self._fanout.submit(self._fanout_send, msg_type, msg_id,
                                    payload, target)
            except RuntimeError:
                self._fanout_slots.release()   # pool already shut down

    def send_peers(self, addr: str, peers: list[str]):
        """Send peer list to addr."""
        self._send_one(MT_PEERS, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "peers": peers},
                       self._addr_tuple(addr))

    def request_sync(self, addr: str, from_h: int,
                     to_h: int = None, timeout: float = SYNC_TIMEOUT):
        """Ask addr for chain[from_h:to_h]. Returns list of blocks or None."""
        msg_id = self._new_msg_id()
        pending = _PendingSync()
        with self._sync_lock:
            self._pending_sync[msg_id] = pending
        self._send_one(MT_GETSYNC, msg_id,
                       {"genesis": self.genesis_hash,
                        "from_h": from_h,
                        "to_h": to_h},
                       self._addr_tuple(addr))
        if pending.event.wait(timeout):
            with self._sync_lock:
                self._pending_sync.pop(msg_id, None)
            return pending.result
        with self._sync_lock:
            self._pending_sync.pop(msg_id, None)
        return None

    def get_info(self, addr: str, timeout: float = 8.0) -> dict | None:
        """Request tip info from peer. Returns {"height": N, "tip_hash": "..."} or None."""
        msg_id = self._new_msg_id()
        ev     = threading.Event()
        with self._info_lock:
            self._info_events[msg_id] = ev
        self._send_one(MT_GETINFO, msg_id,
                       {"genesis": self.genesis_hash},
                       self._addr_tuple(addr))
        if ev.wait(timeout):
            with self._info_lock:
                result = self._info_results.pop(msg_id, None)
                self._info_events.pop(msg_id, None)
            return result
        with self._info_lock:
            self._info_events.pop(msg_id, None)
        return None

    def broadcast_discover(self):
        """Announce our data port on the shared LAN_DISCOVERY_PORT so other
        nodes with the same genesis on this network segment can find us,
        regardless of what data port either of us actually runs on (two
        nodes behind the same router commonly differ on purpose, since a
        router can only port-forward one external port to one internal
        machine). No-op if the discovery socket never came up. Sent from
        every local interface (see _broadcast_from_all_interfaces) rather
        than through self._disc_sock's own wildcard bind, since sending
        from an unbound socket leaves interface selection to the OS's
        default route, unreliable on a machine with any other active
        network adapter (VPN, Docker, Hyper-V, VirtualBox, ...)."""
        if not self._disc_sock:
            return
        payload = _encode({"type": "announce", "genesis": self.genesis_hash,
                           "port": self.port})
        _broadcast_from_all_interfaces(payload, LAN_DISCOVERY_PORT)

    def _disc_recv_loop(self):
        while self._running:
            try:
                data, sender = self._disc_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self._executor.submit(self._handle_disc_message, data, sender)

    def _handle_disc_message(self, data: bytes, sender: tuple):
        try:
            parsed = _decode(data)
        except Exception:
            return
        if not isinstance(parsed, dict) or parsed.get("genesis") != self.genesis_hash:
            return
        host = sender[0]
        if not _is_lan_source(host) or host in self._local_ips:
            return

        if parsed.get("type") == "probe":
            # A node still choosing its own data port (probe_lan_ports, run
            # before it has bound one) asking who's already active on this
            # network. Reply with our own announce directly to it, even
            # though it isn't listening on LAN_DISCOVERY_PORT itself,
            # UDP replies go straight to the sender's actual (ip, port).
            try:
                reply = _encode({"type": "announce", "genesis": self.genesis_hash,
                                 "port": self.port})
                self._disc_sock.sendto(reply, sender)
            except OSError:
                pass
            return

        port = parsed.get("port")
        if not isinstance(port, int) or not (0 < port <= 65535):
            return
        # Confirm reachability at the announced port over the ordinary
        # PING/PONG path, ping() already admits a private-source PONG to
        # the pool on sight, so a genuine node on the other end just peers
        # up from here with no further plumbing needed. Already running off
        # the recv thread (see _disc_recv_loop's own executor.submit), so
        # blocking here on ping()'s PONG wait is fine.
        self.ping(f"{host}:{port}")

    def _ping_payload(self) -> dict:
        """What every PING this node sends carries.

        One definition, because the three places that built this by hand
        did not agree and the disagreement was load-bearing: two of them
        omitted `proto`, and _protocol_ok reads a missing `proto` as 0,
        which is below the floor. So the receiver refused those PINGs
        outright, sent no PONG, and never admitted the sender. Both of the
        relay-assisted hole-punch paths (punch_via, and the PUNCH_GO
        handler acting on a relay's instruction) were sending exactly
        those PINGs, which is to say the punch they exist to perform could
        not complete against any node enforcing the floor. Only
        punch_direct, the one copy that happened to include the field,
        worked.
        """
        payload = {"genesis": self.genesis_hash, "proto": PROTOCOL_VERSION}
        if self.our_external_addr:
            payload["from"] = self.our_external_addr
        return payload

    def _punch_burst(self, target_addr: str):
        """Fire the PING burst that opens our NAT hole toward target.

        Hole punching needs both sides sending toward each other at
        roughly the same moment, so this is a burst rather than one
        datagram: it covers the spread between when we start and when the
        other side does.
        """
        target  = self._addr_tuple(target_addr)
        payload = self._ping_payload()
        for _ in range(PUNCH_BURST_COUNT):
            try:
                self._send_one(MT_PING, self._new_msg_id(), payload, target)
            except Exception:
                pass
            time.sleep(PUNCH_BURST_SPACING)

    def punch_direct(self, target_addr: str):
        """Fire UDP bursts toward target to open our NAT hole.
        No relay needed; both nodes do this simultaneously when they
        discover each other via DHT."""
        self._punch_burst(target_addr)

    def punch_via(self, relay_addr: str, target_addr: str):
        """Ask relay to coordinate a hole punch toward target.
        Simultaneously fire UDP packets toward target to open our NAT hole
        before the relay tells the target to do the same."""
        self._send_one(MT_PUNCH_REQ, self._new_msg_id(),
                       {"genesis": self.genesis_hash,
                        "target": target_addr},
                       self._addr_tuple(relay_addr))
        self._punch_burst(target_addr)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    def _recv_loop(self):
        while self._running:
            try:
                data, sender = self._sock.recvfrom(65535)
            except socket.timeout:
                self._reassembler.evict_stale()
                continue
            except OSError:
                break
            if not self._allow_rate(sender[0]):
                continue
            self._executor.submit(self._handle_datagram, data, sender)

    def _allow_rate(self, source_ip: str) -> bool:
        """Token-bucket check per source IP. Cheap, in the recv thread itself
        so an over-limit sender never even reaches the worker pool."""
        now = time.monotonic()
        with self._rate_lock:
            bucket = self._rate_buckets.get(source_ip)
            if bucket is None:
                self._rate_buckets[source_ip] = [RATE_LIMIT_BURST - 1, now]
                if len(self._rate_buckets) > 20_000:
                    cutoff = now - 60
                    self._rate_buckets = {
                        ip: b for ip, b in self._rate_buckets.items() if b[1] > cutoff
                    }
                return True
            tokens, last = bucket
            tokens = min(RATE_LIMIT_BURST, tokens + (now - last) * RATE_LIMIT_PER_SEC)
            if tokens < 1:
                bucket[1] = now
                return False
            bucket[0] = tokens - 1
            bucket[1] = now
            return True

    @staticmethod
    def _should_ack(chunk_idx: int, chunk_total: int, done: bool) -> bool:
        """Whether this arriving chunk is one we answer with an ACK.

        Single-chunk messages are never acked (they are fire-and-forget by
        design). Beyond that: always when the message just completed, so
        the sender's waiter is released immediately, always on the last
        chunk index, so a message whose tail is what went missing still
        draws an answer, and otherwise every ACK_EVERY_CHUNKS chunks. See
        that constant for why every chunk was the wrong answer.
        """
        if chunk_total <= 1:
            return False
        if done or chunk_idx == chunk_total - 1:
            return True
        return chunk_idx % ACK_EVERY_CHUNKS == ACK_EVERY_CHUNKS - 1

    def _handle_datagram(self, data: bytes, sender: tuple):
        unpacked = _unpack(data)
        if unpacked is None:
            log.debug("[udp] _unpack FAILED from %s:%s  len=%d  raw=%s",
                      sender[0], sender[1], len(data), data[:16].hex())
            return
        msg_type, msg_id, chunk_idx, chunk_total, payload_bytes = unpacked
        log.debug("[udp] recv from %s:%s  type=0x%02x  msg_id=%d  chunk=%d/%d  payload_len=%d",
                  sender[0], sender[1], msg_type, msg_id, chunk_idx, chunk_total, len(payload_bytes))

        # Reassemble chunked messages
        if msg_type in (MT_SYNC,):
            with self._sync_lock:
                pending = self._pending_sync.get(msg_id)
            if pending:
                pending.feed(chunk_idx, chunk_total, payload_bytes)
                if self._should_ack(chunk_idx, chunk_total, pending.event.is_set()):
                    self._send_one(MT_ACK, self._new_msg_id(),
                                   {"acked_msg_id": msg_id,
                                    "acked_chunks": list(pending.chunks.keys())},
                                   sender)
            return

        # For everything else, reassemble then dispatch
        complete = self._reassembler.feed(
            sender, msg_id, chunk_idx, chunk_total, payload_bytes
        )
        if self._should_ack(chunk_idx, chunk_total, complete is not None):
            held = (list(range(chunk_total)) if complete is not None
                    else self._reassembler.held_chunks(sender, msg_id))
            self._send_one(MT_ACK, self._new_msg_id(),
                           {"acked_msg_id": msg_id, "acked_chunks": held},
                           sender)
        if complete is None:
            return

        if msg_type in (MT_BLOCK, MT_TX):
            complete = _inflate(complete)
            if complete is None:
                return

        try:
            parsed = _decode(complete)
        except Exception:
            return

        # Genesis check for peer messages
        if msg_type not in (MT_PING, MT_PONG, MT_ACK):
            if parsed.get("genesis") != self.genesis_hash:
                return

        self._dispatch(msg_type, msg_id, parsed, sender)

    def _dispatch(self, msg_type: int, msg_id: int, data: dict, sender: tuple):
        sender_addr = f"{sender[0]}:{sender[1]}"

        if msg_type == MT_PING:
            peer_genesis = data.get("genesis")
            if peer_genesis == self.genesis_hash:
                if not _protocol_ok(data):
                    log.debug("[udp] refused %s: protocol %s below floor %d",
                              sender_addr, data.get("proto", 0), MIN_PROTOCOL_VERSION)
                    return
                self._pool.touch(sender_addr)
                self._send_one(MT_PONG, msg_id,
                               {"observed": sender_addr,
                                "genesis": self.genesis_hash,
                                "proto": PROTOCOL_VERSION},
                               sender)
                announced = data.get("from", "")
                if announced and self._on_peer_hint:
                    self._on_peer_hint(announced)
                # Also hint the address this packet actually arrived from,
                # not just what the sender claims about itself. `announced`
                # is the sender's own our_external_addr, cached from
                # whichever PONG last confirmed it and reused for every
                # peer after that; under a NAT that hands out a different
                # external port per destination (or right after startup,
                # before that cache is confirmed at all), announced can be
                # stale or simply wrong for us specifically, while
                # sender_addr is the one thing about this packet that
                # cannot be: it's where it actually came from. Without
                # this, a peer whose self-report doesn't happen to match
                # what we'd need to reach them back on can ping us, get a
                # PONG, keep gossiping to us forever, and never once
                # become a candidate we ourselves try to reach.
                if self._on_peer_hint:
                    self._on_peer_hint(sender_addr)
                # A private-range source could only have reached us over the
                # local network (never routed off the public internet), so
                # it's admissible on sight. This is what makes broadcast
                # discovery (and any direct LAN ping) actually peer up,
                # without waiting on the DHT/punch pipeline at all. Excludes
                # our own IPs so a looped-back broadcast doesn't self-admit.
                if _is_lan_source(sender[0]) and sender[0] not in self._local_ips:
                    self._pool.add(sender_addr, allow_private=True)

        elif msg_type == MT_PONG:
            if not _protocol_ok(data):
                log.debug("[udp] ignoring PONG from %s: protocol %s below floor %d",
                          sender_addr, data.get("proto", 0), MIN_PROTOCOL_VERSION)
                return
            observed = data.get("observed", "")
            with self._pong_lock:
                matched = msg_id in self._pong_events
                log.debug("[udp] PONG from %s  msg_id=%d  matched=%s", sender_addr, msg_id, matched)
                if matched:
                    self._pong_addrs[msg_id] = observed
                    self._pong_events[msg_id].set()
            if _is_lan_source(sender[0]) and sender[0] not in self._local_ips:
                self._pool.add(sender_addr, allow_private=True)

        elif msg_type == MT_PEERS:
            peers = data.get("peers", [])
            self._on_peers(peers, sender_addr)

        elif msg_type == MT_BLOCK:
            if self._is_new(msg_id):
                self._pool.touch(sender_addr)
                block = data.get("block")
                if block:
                    log.debug("[udp] recv_block height=%s from=%s",
                              block.get("height"), sender_addr)
                    # Handed up, never relayed from here. Relaying at this
                    # layer would mean forwarding a block nobody has
                    # validated yet, and would bypass the stem/fluff rule
                    # entirely. Propagation is gossip.py's decision, taken
                    # after Node validates, see gossip.relay_block.
                    self._on_block(block, sender_addr,
                                   bool(data.get("stemming", False)))

        elif msg_type == MT_TX:
            if self._is_new(msg_id):
                self._pool.touch(sender_addr)
                tx = data.get("tx")
                if tx:
                    self._on_tx(tx, sender_addr,
                                bool(data.get("stemming", False)))

        elif msg_type == MT_GETSYNC:
            self._handle_getsync(msg_id, data, sender)

        elif msg_type == MT_PUNCH_REQ:
            target = data.get("target", "")
            if target:
                self._handle_punch_req(sender_addr, target)

        elif msg_type == MT_PUNCH_GO:
            target = data.get("target", "")
            if target:
                log.debug("[udp] punch_go -> %s", target)
                self._punch_burst(target)
                if self._on_punch_go:
                    self._on_punch_go(target)

        elif msg_type == MT_GETINFO:
            # Peer requesting our tip info; respond with height + tip hash
            # (+ our wallet address and software version, purely
            # informational, see set_tip_provider).
            self._pool.touch(sender_addr)
            if self._get_tip_fn:
                height, tip_hash, wallet, version, work = self._get_tip_fn()
                self._send_one(MT_INFO, msg_id,
                               {"genesis": self.genesis_hash,
                                "height":   height,
                                "tip_hash": tip_hash,
                                "wallet":   wallet,
                                "version":  version,
                                # Cumulative proven VDF iterations. Height is
                                # not what fork choice compares (see
                                # ChainState.is_better_than): forks retarget
                                # from their own timestamps, so a shorter
                                # chain can carry strictly more work.
                                "work":     work},
                               sender)

        elif msg_type == MT_INFO:
            self._pool.touch(sender_addr)
            with self._info_lock:
                if msg_id in self._info_events:
                    self._info_results[msg_id] = {
                        "height":   data.get("height"),
                        "tip_hash": data.get("tip_hash", ""),
                        # Older peers won't send these fields; defaults keep
                        # readers (Syncer, PeerPool.update_info) working
                        # unchanged against them.
                        "wallet":   data.get("wallet", ""),
                        "version":  data.get("version", ""),
                        # Absent from peers too old to send it; None means
                        # "unknown", never "zero", see Syncer.
                        "work":     data.get("work"),
                    }
                    self._info_events[msg_id].set()

        elif msg_type == MT_ACK:
            self._handle_ack(data, sender)

    def _handle_getsync(self, msg_id: int, data: dict, sender: tuple):
        """Serve a chain segment request, to an address we have reason to
        believe actually sent it.

        A sync page is the largest message this protocol produces, and a
        GETSYNC is one small datagram whose source address nothing checks.
        Forging a victim's address in one turned this node into an
        amplifier pointed at them, at up to MAX_SYNC_BLOCKS blocks per
        datagram, which is the whole shape of a reflection attack.

        Gating on pool membership alone would have been wrong: an inbound
        PING from a public address only touches the pool (a no-op unless
        already present) and queues a hint, so a node that just found us,
        pinged us and wants our chain is genuinely not a peer yet. It
        would have been refused its first sync until our own discovery
        loop got round to probing it back, which is bootstrap broken to
        close a hole.

        So membership is the fast path, and anything else is asked to
        prove it can receive at the address it claims: an ordinary
        PING, answered by an ordinary PONG, both already in the protocol.
        Nothing on the wire changes, so a peer on an older version is
        served exactly as before, it just answers a PING first. A forged
        source only reaches a host that answers our PING with our genesis
        hash, which reduces the reflection target set from the whole
        internet to nodes on this network, and those are the hosts best
        able to absorb it.
        """
        sender_addr = f"{sender[0]}:{sender[1]}"
        self._pool.touch(sender_addr)
        if self._may_serve_sync(sender_addr):
            self._serve_sync(msg_id, data, sender)
        else:
            # Confirm, then answer this same request rather than making the
            # requester wait out its timeout and ask again. The retry is
            # still the backstop if the confirmation fails; this just means
            # a new node's first sync costs a round trip instead of a
            # timeout.
            self._start_confirmation(sender_addr,
                                     lambda: self._serve_sync(msg_id, data, sender))

    def _serve_sync(self, msg_id: int, data: dict, sender: tuple):
        from_h = data.get("from_h", 0)
        if not isinstance(from_h, int) or from_h < 0:
            from_h = 0
        to_h = data.get("to_h")
        capped_to = from_h + MAX_SYNC_BLOCKS - 1
        to_h = capped_to if not isinstance(to_h, int) else min(to_h, capped_to)
        chain  = self._get_chain_fn(from_h, to_h) if self._get_chain_fn else []
        # Compressed for the same reason blocks are, and it pays far more
        # here: a page of 500 blocks shares so much structure that it goes
        # to about 8% of its size, 260 chunks down to 21, which is over a
        # second of pacing removed from every page a catching-up node
        # fetches. This is the largest message the protocol has.
        payload = zlib.compress(
            _encode({"genesis": self.genesis_hash, "chain": chain}),
            BLOCK_COMPRESS_LEVEL)
        self._send_chunked(MT_SYNC, msg_id, payload, sender)

    def _may_serve_sync(self, sender_addr: str) -> bool:
        """Whether this address has already shown it can receive here.

        True for a peer, and for an address confirmed recently enough that
        a multi-page sync is not a PING per page.
        """
        if sender_addr in self._pool.all_addrs():
            return True
        with self._routable_lock:
            seen = self._routable.get(sender_addr)
        return seen is not None and time.monotonic() - seen < ROUTABLE_TTL_SECONDS

    def _start_confirmation(self, sender_addr: str, then):
        """PING sender_addr, and run `then` if it answers.

        Bounded on purpose. A confirmation occupies a slot and, out of
        slots, the request is dropped rather than each forged source
        starting a PING of its own; dropping is something a UDP request
        already has to survive, and the requester's own retry covers it.
        """
        with self._routable_lock:
            if sender_addr in self._routable_pending:
                return
            if len(self._routable_pending) >= ROUTABLE_MAX_PENDING:
                log.debug("[udp] too many address confirmations in flight, "
                          "dropping a sync request from %s", sender_addr)
                return
            self._routable_pending.add(sender_addr)
        try:
            self._waiters.submit(self._confirm_routable, sender_addr, then)
        except RuntimeError:
            with self._routable_lock:
                self._routable_pending.discard(sender_addr)

    def _confirm_routable(self, addr: str, then=None):
        """PING addr, remember it if it answers, then run `then`. Runs off
        the receive path, since it waits on a round trip."""
        answered = False
        try:
            answered = self.ping(addr, timeout=ROUTABLE_PING_TIMEOUT) is not None
            if answered:
                with self._routable_lock:
                    self._routable[addr] = time.monotonic()
                log.debug("[udp] %s answered, its sync requests are servable", addr)
        except Exception:
            log.debug("[udp] address confirmation failed for %s", addr, exc_info=True)
        finally:
            with self._routable_lock:
                self._routable_pending.discard(addr)
        if answered and then is not None:
            try:
                then()
            except Exception:
                log.debug("[udp] serving %s after confirmation failed", addr,
                          exc_info=True)

    def _handle_punch_req(self, requester_addr: str, target_addr: str):
        """Relay: tell both peers to punch toward each other."""
        log.debug("[udp] punch relay %s <-> %s", requester_addr, target_addr)
        # Tell target to punch toward requester
        self._send_one(MT_PUNCH_GO, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "target": requester_addr},
                       self._addr_tuple(target_addr))
        # Tell requester to punch toward target
        self._send_one(MT_PUNCH_GO, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "target": target_addr},
                       self._addr_tuple(requester_addr))

    # ------------------------------------------------------------------
    # Low-level send helpers
    # ------------------------------------------------------------------

    def _send_one(self, msg_type: int, msg_id: int, data: dict | None,
                  target: tuple, raw_payload: bytes = None):
        if raw_payload is None:
            raw_payload = _encode(data or {})
        pkt = _pack(msg_type, msg_id, 0, 1, raw_payload)
        try:
            self._sock.sendto(pkt, target)
        except Exception as e:
            log.debug("[udp] send error to %s: %s", target, e)

    def _send_chunked(self, msg_type: int, msg_id: int,
                      payload: bytes, target: tuple):
        chunks = _split(payload)
        total  = len(chunks)
        chunk_map = dict(enumerate(chunks))

        if total > 1:
            key = (target, msg_id)
            with self._chunk_lock:
                self._pending_chunked_sends[key] = {
                    "chunks": chunk_map, "acked": set(),
                    "msg_type": msg_type, "event": threading.Event(),
                }

        for i, chunk in chunk_map.items():
            pkt = _pack(msg_type, msg_id, i, total, chunk)
            try:
                self._sock.sendto(pkt, target)
            except Exception as e:
                log.debug("[udp] send_chunked error to %s: %s", target, e)
                break
            if total > 1:
                time.sleep(0.005)  # pacing to avoid drops on NAT/internet paths

        if total > 1:
            try:
                self._waiters.submit(self._retransmit_until_acked, target, msg_id, total)
            except RuntimeError:
                # Pool already shut down. The chunks are on the wire either
                # way; only the retransmit round is lost.
                with self._chunk_lock:
                    self._pending_chunked_sends.pop((target, msg_id), None)

    def _fanout_send(self, msg_type, msg_id, payload, target):
        """One queued fan-out send, freeing its slot however it ends."""
        try:
            self._send_chunked(msg_type, msg_id, payload, target)
        finally:
            self._fanout_slots.release()

    def _retransmit_until_acked(self, target: tuple, msg_id: int, total: int):
        """Resend only the chunks a peer hasn't ACKed yet, a bounded number
        of times, then give up and stop tracking, see CHUNK_ACK_* above
        for why this degrades safely against a peer that never ACKs."""
        key = (target, msg_id)
        for _round in range(CHUNK_ACK_MAX_ROUNDS):
            with self._chunk_lock:
                state = self._pending_chunked_sends.get(key)
            if state is None:
                return  # already cleaned up (fully acked, or evicted)
            if state["event"].wait(CHUNK_ACK_TIMEOUT):
                break  # fully acked, _handle_ack set this
            with self._chunk_lock:
                state = self._pending_chunked_sends.get(key)
                if state is None:
                    return
                missing = {i: c for i, c in state["chunks"].items()
                          if i not in state["acked"]}
            for i, chunk in missing.items():
                pkt = _pack(state["msg_type"], msg_id, i, total, chunk)
                try:
                    self._sock.sendto(pkt, target)
                except Exception as e:
                    log.debug("[udp] retransmit error to %s: %s", target, e)
                time.sleep(0.005)
        with self._chunk_lock:
            self._pending_chunked_sends.pop(key, None)

    def _handle_ack(self, data: dict, sender: tuple):
        acked_msg_id = data.get("acked_msg_id")
        acked_chunks = data.get("acked_chunks")
        if not isinstance(acked_msg_id, int) or not isinstance(acked_chunks, list):
            return
        key = (sender, acked_msg_id)
        with self._chunk_lock:
            state = self._pending_chunked_sends.get(key)
            if state is None:
                return
            state["acked"].update(i for i in acked_chunks if isinstance(i, int))
            if state["acked"] >= set(state["chunks"]):
                state["event"].set()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def set_chain_provider(self, fn):
        """fn(from_h, to_h) -> list[block_dict]. Set by Node after init."""
        self._get_chain_fn = fn

    def set_tip_provider(self, fn):
        """fn() -> (height, tip_hash, wallet, version, cumulative_iterations).
        Used for lightweight
        MT_GETINFO responses. wallet is our own address, shared here purely
        so peers can display/use it (e.g. for gifting). It's already
        public the moment we build a block, this just makes it available
        without needing to wait for or find one. version is our own
        software version, so peers can flag when we're outdated."""
        self._get_tip_fn = fn

    def set_punch_go_callback(self, fn):
        """fn(addr) called when PUNCH_GO received; discovery should ping immediately."""
        self._on_punch_go = fn

    def _get_chain_fn(self, from_h, to_h):  # default no-op before Node sets it
        return []

    @staticmethod
    def _addr_tuple(addr: str) -> tuple:
        host, port = addr.rsplit(":", 1)
        return host, int(port)

    @staticmethod
    def _new_msg_id() -> int:
        return secrets.randbits(32)

    def _mark_seen(self, msg_id: int):
        with self._seen_lock:
            self._seen_msg[msg_id] = time.monotonic()
            # Evict old entries
            if len(self._seen_msg) > 50_000:
                cutoff = time.monotonic() - 300
                self._seen_msg = {k: v for k, v in self._seen_msg.items()
                                  if v > cutoff}

    def _is_new(self, msg_id: int) -> bool:
        with self._seen_lock:
            if msg_id in self._seen_msg:
                return False
            self._seen_msg[msg_id] = time.monotonic()
            return True
