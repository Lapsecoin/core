"""
Unit tests for peer_udp.py's UDPTransport._dispatch, MT_TX path only.

Regression test for a bug where the propagation phase was dropped on
receive, collapsing every inbound tx to an immediate fluff regardless of
what the sender actually put on the wire, defeating Dandelion's stem
phase between processes. No sockets are opened; _dispatch is called
directly with a hand-built message.
"""

import os
import socket
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import peer_udp
from peer_udp import (LAN_DISCOVERY_PORT, MT_GETINFO, MT_INFO, MT_PING, MT_PONG,
                       MT_TX, UDPTransport, _decode, _encode, probe_lan_ports)


def _make_transport(on_tx):
    return UDPTransport(
        port=9999,
        genesis_hash="a" * 64,
        on_block=MagicMock(),
        on_tx=on_tx,
        on_peers=MagicMock(),
        pool=MagicMock(),
    )


def test_dispatch_forwards_the_stem_flag():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 111, {"tx": tx, "stemming": True}, ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", True)


def test_dispatch_defaults_to_public_when_flag_absent():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 222, {"tx": tx}, ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", False)


def test_dispatch_dedups_by_msg_id():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    msg = {"tx": tx, "stemming": False}
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    assert on_tx.call_count == 1


# ---------------------------------------------------------------------------
# MT_GETINFO / MT_INFO wallet/version fields, must stay wire-compatible
# with peers not carrying them yet.
# ---------------------------------------------------------------------------

def test_getinfo_response_includes_wallet_and_version():
    """Responding to MT_GETINFO must include our wallet and version
    alongside height/tip."""
    udp = _make_transport(MagicMock())
    udp.set_tip_provider(lambda: (42, "deadbeef", "some.wallet.address", "0.1.1", 4242))
    sent = []
    udp._send_one = lambda msg_type, msg_id, data, target: sent.append((msg_type, data, target))

    udp._dispatch(MT_GETINFO, 1, {"genesis": udp.genesis_hash}, ("1.2.3.4", 5000))

    assert len(sent) == 1
    msg_type, data, target = sent[0]
    assert msg_type == MT_INFO
    assert data == {"genesis": udp.genesis_hash, "height": 42, "work": 4242,
                    "tip_hash": "deadbeef", "wallet": "some.wallet.address",
                    "version": "0.1.1"}


def test_info_reply_captures_wallet_and_version():
    """A well-formed MT_INFO reply's wallet/version fields land in the
    pending result."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[7] = ev

    udp._dispatch(MT_INFO, 7, {"height": 10, "tip_hash": "abc",
                              "wallet": "peer.wallet", "version": "0.2.0"},
                  ("1.2.3.4", 5000))

    assert udp._info_results[7] == {"height": 10, "tip_hash": "abc", "work": None,
                                    "wallet": "peer.wallet", "version": "0.2.0"}


def test_info_reply_from_older_peer_without_wallet_or_version_field():
    """An old peer's MT_INFO reply (no wallet/version keys at all) must not
    break. Both just come back empty instead of missing/erroring."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[9] = ev

    udp._dispatch(MT_INFO, 9, {"height": 10, "tip_hash": "abc"}, ("1.2.3.4", 5000))

    assert udp._info_results[9] == {"height": 10, "tip_hash": "abc", "work": None,
                                    "wallet": "", "version": ""}


# ---------------------------------------------------------------------------
# LAN auto-admit: a private-source PING or PONG proves direct local
# reachability, so it's admitted on sight instead of needing a manual add.
# ---------------------------------------------------------------------------

def test_ping_from_lan_source_admits_to_pool():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 1, {"genesis": udp.genesis_hash,
                           "proto": peer_udp.PROTOCOL_VERSION},
                  ("192.168.1.42", 8333))
    pool.add.assert_called_once_with("192.168.1.42:8333", allow_private=True)


def test_pong_from_lan_source_admits_to_pool():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._dispatch(MT_PONG, 2, {"observed": "192.168.1.42:8333",
                           "proto": peer_udp.PROTOCOL_VERSION},
                  ("192.168.1.42", 8333))
    pool.add.assert_called_once_with("192.168.1.42:8333", allow_private=True)


def test_ping_from_public_source_does_not_bypass_pool_validation():
    """A public-IP sender still goes through PeerPool.add's own routability
    check (allow_private only makes sense for genuinely private sources)."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 3, {"genesis": udp.genesis_hash,
                           "proto": peer_udp.PROTOCOL_VERSION},
                  ("8.8.8.8", 8333))
    pool.add.assert_not_called()


def test_ping_from_public_source_hints_the_real_observed_address():
    """A public PING's self-reported "from" can be stale or simply wrong for
    us specifically (a NAT handing out a different external port per
    destination, or the sender's own address cache not confirmed yet), so
    it must not be the only candidate this ever offers. The literal
    address the packet arrived from is always hinted too: it's the one
    fact about this packet that cannot be wrong."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    hint = MagicMock()
    udp._on_peer_hint = hint
    udp._dispatch(MT_PING, 30, {"genesis": udp.genesis_hash,
                            "proto": peer_udp.PROTOCOL_VERSION,
                            "from": "203.0.113.9:9001"},  # wrong/stale on purpose
                  ("8.8.8.8", 8333))
    hint.assert_any_call("8.8.8.8:8333")


def test_ping_without_a_from_field_still_hints_the_observed_address():
    """Even with no self-reported address at all (an old peer, or one that
    hasn't confirmed its own external address yet), the sender is still a
    real, reachable address worth trying."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    hint = MagicMock()
    udp._on_peer_hint = hint
    udp._dispatch(MT_PING, 31, {"genesis": udp.genesis_hash,
                            "proto": peer_udp.PROTOCOL_VERSION},
                  ("8.8.8.8", 8333))
    hint.assert_called_once_with("8.8.8.8:8333")


def test_ping_from_loopback_does_not_self_admit():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 4, {"genesis": udp.genesis_hash,
                           "proto": peer_udp.PROTOCOL_VERSION},
                  ("127.0.0.1", 8333))
    pool.add.assert_not_called()


def test_ping_from_own_private_ip_does_not_self_admit():
    """A cloud instance's own broadcast can loop back to itself over its
    private VPC IP (e.g. AWS's 172.31.x.x behind a public/elastic IP).
    That must not self-admit just because the source happens to be private."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._local_ips = {"172.31.17.210"}
    udp._dispatch(MT_PING, 5, {"genesis": udp.genesis_hash,
                           "proto": peer_udp.PROTOCOL_VERSION},
                  ("172.31.17.210", 9999))
    pool.add.assert_not_called()


def test_broadcast_discover_announces_own_port_on_discovery_socket(monkeypatch):
    """The LAN discovery broadcast must carry this node's actual data port
    (self.port) rather than requiring every node to share one port, two
    machines behind the same router commonly use different ports on
    purpose (a router can only forward one external port to one internal
    machine), and this is the whole point of a dedicated discovery port."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = MagicMock()  # just needs to be truthy
    sent = []
    monkeypatch.setattr(peer_udp, "_broadcast_from_all_interfaces",
                        lambda payload, port: sent.append((payload, port)))

    udp.broadcast_discover()

    assert len(sent) == 1
    payload, port = sent[0]
    assert port == LAN_DISCOVERY_PORT
    decoded = _decode(payload)
    assert decoded == {"type": "announce", "genesis": udp.genesis_hash, "port": 9999}


def test_broadcast_discover_noop_without_discovery_socket():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = None
    udp.broadcast_discover()  # must not raise


def test_disc_announce_from_lan_pings_announced_port():
    """A discovery announcement from a LAN address must trigger a PING to
    the *announced* port, not whatever port the announcement itself arrived
    on, that's what lets two nodes on different data ports find each
    other."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == ["192.168.1.50:8444"]


def test_disc_announce_wrong_genesis_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": "different", "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_announce_from_own_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._local_ips = {"192.168.1.50"}
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_announce_from_public_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("8.8.8.8", 8334))

    assert pinged == []


def test_disc_announce_bad_port_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": "not-a-port"})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_probe_gets_announce_reply():
    """A node still picking its own port (probe_lan_ports) sends a bare
    probe with no port of its own, an already-running node must reply
    with its own announce, not try to ping the prober (which has nothing
    listening on a data port yet)."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    sent = []
    udp._disc_sock = MagicMock()
    udp._disc_sock.sendto = lambda payload, target: sent.append((payload, target))
    payload = _encode({"type": "probe", "genesis": udp.genesis_hash})

    udp._handle_disc_message(payload, ("192.168.1.60", 51234))

    assert pinged == []
    assert len(sent) == 1
    reply_payload, target = sent[0]
    assert target == ("192.168.1.60", 51234)
    assert _decode(reply_payload) == {"type": "announce", "genesis": udp.genesis_hash,
                                      "port": 9999}


def test_disc_probe_from_public_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = MagicMock()
    payload = _encode({"type": "probe", "genesis": udp.genesis_hash})

    udp._handle_disc_message(payload, ("8.8.8.8", 51234))

    udp._disc_sock.sendto.assert_not_called()


# ---------------------------------------------------------------------------
# probe_lan_ports: standalone pre-bind port negotiation.
# ---------------------------------------------------------------------------

def test_probe_lan_ports_collects_matching_replies():
    genesis = "a" * 64
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(2)

    def respond_once():
        data, sender = responder.recvfrom(2048)
        parsed = _decode(data)
        assert parsed == {"type": "probe", "genesis": genesis}
        reply = _encode({"type": "announce", "genesis": genesis, "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_once, daemon=True)
    t.start()
    # Point the probe at our own responder's port instead of the real
    # LAN_DISCOVERY_PORT so the test doesn't depend on that port being free.
    found = probe_lan_ports(genesis, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=2)
    responder.close()

    assert found == {8444}


def test_probe_lan_ports_survives_a_dropped_first_probe():
    """UDP has no delivery guarantee even on a working LAN, simulate the
    first probe packet vanishing (respond only from the second one
    onward) and confirm the resend still gets a reply within the wait
    window, instead of the whole check silently coming back empty."""
    genesis = "a" * 64
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(3)

    def respond_from_second_probe():
        responder.recvfrom(2048)  # first probe: dropped, no reply
        data, sender = responder.recvfrom(2048)
        parsed = _decode(data)
        assert parsed == {"type": "probe", "genesis": genesis}
        reply = _encode({"type": "announce", "genesis": genesis, "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_from_second_probe, daemon=True)
    t.start()
    found = probe_lan_ports(genesis, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=3)
    responder.close()

    assert found == {8444}


def test_probe_lan_ports_ignores_wrong_genesis():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(2)

    def respond_once():
        data, sender = responder.recvfrom(2048)
        reply = _encode({"type": "announce", "genesis": "other-chain", "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_once, daemon=True)
    t.start()
    found = probe_lan_ports("a" * 64, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=2)
    responder.close()

    assert found == set()


def test_probe_lan_ports_empty_when_nobody_answers():
    found = probe_lan_ports("a" * 64, wait=0.3, disc_port=59991)
    assert found == set()


# ---------------------------------------------------------------------------
# start() bind-retry: a second node process on the same machine shouldn't
# just crash because its requested port is already taken.
# ---------------------------------------------------------------------------

def test_start_falls_back_to_next_free_port_on_collision():
    # Bind to an ephemeral port for a stable, unused starting point.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    taken_port = probe.getsockname()[1]
    probe.close()

    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", taken_port))
    try:
        second = UDPTransport(port=taken_port, genesis_hash="a" * 64, on_block=MagicMock(),
                              on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
        try:
            second.start()
            assert second.port != taken_port
            assert second.port > taken_port
        finally:
            second.stop()
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# Compressed blocks (MT_BLOCK_Z)
# ---------------------------------------------------------------------------

def test_a_block_on_the_wire_is_compressed():
    """There is one block format, so a datagram carrying the retired
    uncompressed one matches nothing and is simply not a block any more."""
    import zlib
    udp = _make_transport(MagicMock())
    blk = {"height": 7, "hash": "ab" * 32}
    payload = peer_udp._encode({"genesis": udp.genesis_hash, "block": blk,
                                "stemming": False})

    udp._handle_datagram(
        peer_udp._pack(peer_udp.MT_BLOCK, 4242, 0, 1, zlib.compress(payload)),
        ("5.6.7.8", 9999))

    udp._on_block.assert_called_once_with(blk, "5.6.7.8:9999", False)


def test_the_retired_uncompressed_block_type_is_ignored():
    udp = _make_transport(MagicMock())
    blk = {"height": 7, "hash": "ab" * 32}
    payload = peer_udp._encode({"genesis": udp.genesis_hash, "block": blk,
                                "stemming": False})

    udp._handle_datagram(peer_udp._pack(0x04, 777, 0, 1, payload),
                         ("5.6.7.8", 9999))

    udp._on_block.assert_not_called()


def test_a_peer_below_the_protocol_floor_is_not_ponged():
    """Refused in the handshake, the same place and the same way a peer on
    another network already is."""
    udp = _make_transport(MagicMock())
    udp._send_one = MagicMock()

    udp._dispatch(peer_udp.MT_PING, 1,
                  {"genesis": udp.genesis_hash}, ("9.9.9.9", 8333))
    udp._send_one.assert_not_called()

    udp._dispatch(peer_udp.MT_PING, 2,
                  {"genesis": udp.genesis_hash,
                   "proto": peer_udp.PROTOCOL_VERSION}, ("9.9.9.9", 8333))
    assert udp._send_one.call_count == 1


def test_our_own_ping_advertises_the_protocol():
    assert peer_udp._protocol_ok({"proto": peer_udp.PROTOCOL_VERSION})
    assert not peer_udp._protocol_ok({})
    assert not peer_udp._protocol_ok({"proto": "nonsense"})


def test_a_decompression_bomb_is_refused():
    """Compression breaks the link between what a sender spends and what we
    allocate, so the inflated form is held to the same ceiling an
    uncompressed message has."""
    import zlib
    limit = peer_udp.MAX_CHUNK_TOTAL * peer_udp.MAX_CHUNK_SIZE
    bomb = zlib.compress(b"\0" * (limit * 4))
    assert len(bomb) < 100_000          # tiny on the wire, huge inflated

    assert peer_udp._inflate(bomb) is None


def test_a_payload_that_is_not_compressed_at_all_is_refused():
    assert peer_udp._inflate(b"not zlib, just bytes") is None


def test_real_traffic_is_nowhere_near_the_ratio_bound():
    """The bound is on expansion, so what matters is that real payloads sit
    well under it. Measured: a block expands about 1.7x and the most
    compressible thing the protocol sends, a page of near-identical empty
    blocks, about 11.5x, against a limit of 200."""
    import zlib
    from tests.fixtures import make_block

    page = peer_udp._encode(
        {"genesis": "x" * 64,
         "chain": [make_block(h, f"{h:064x}", []) for h in range(50)]})
    compressed = zlib.compress(page, peer_udp.BLOCK_COMPRESS_LEVEL)

    assert len(page) / len(compressed) < peer_udp.MAX_INFLATE_RATIO / 4
    assert peer_udp._inflate(compressed) == page


def test_an_old_peer_answering_our_ping_never_completes_the_handshake():
    """Every way a peer gets into the pool (the on-disk cache, the DHT, a
    peer hint) goes through enqueue_candidate and then a ping, and the only
    pool.add in discovery is behind a successful one. So a stale address
    surviving a restart in the cache is refused by the same floor as
    anything else, and never becomes a peer again."""
    import threading
    import time

    udp = _make_transport(MagicMock())
    udp._send_one = MagicMock()

    def answer(with_proto):
        def run():
            time.sleep(0.05)
            msg_id = list(udp._pong_events)[0]
            data = {"observed": "1.2.3.4:8333"}
            if with_proto:
                data["proto"] = peer_udp.PROTOCOL_VERSION
            udp._dispatch(peer_udp.MT_PONG, msg_id, data, ("1.2.3.4", 8333))
        threading.Thread(target=run, daemon=True).start()

    answer(with_proto=False)
    assert udp.ping("1.2.3.4:8333", timeout=1.0) is None

    answer(with_proto=True)
    assert udp.ping("1.2.3.4:8333", timeout=1.0) == "1.2.3.4:8333"


class TestChunkIndexBounds:
    """A chunk index outside the message it claims to belong to.

    Both reassembly paths counted stored chunks to decide completeness
    instead of checking that the indices they were about to read were the
    ones present, so a sender claiming N chunks and sending N out-of-range
    indices satisfied the count and then raised KeyError joining range(N).
    """

    def test_reassembler_refuses_an_out_of_range_index(self):
        r = peer_udp._Reassembler()
        out = None
        for idx in (10, 11, 12):
            out = r.feed(("1.2.3.4", 9), 777, idx, 3, b"x")
        assert out is None
        # and nothing is left pinned in the buffer
        assert r.held_bytes() == 0

    def test_pending_sync_refuses_an_out_of_range_index(self):
        p = peer_udp._PendingSync()
        for idx in (5, 6):
            p.feed(idx, 2, b"y")
        # Not merely "no crash": the event staying clear used to be the
        # symptom of the crash, with request_sync then waiting out its full
        # timeout for a reply that had already been thrown away.
        assert p.result is None
        assert not p.event.is_set()

    def test_a_real_multi_chunk_message_still_reassembles(self):
        r = peer_udp._Reassembler()
        chunks = [b"a" * 10, b"b" * 10, b"c" * 10]
        out = None
        for idx, c in enumerate(chunks):
            out = r.feed(("1.2.3.4", 9), 778, idx, len(chunks), c)
        assert out == b"".join(chunks)
        assert r.held_bytes() == 0


class TestTransportCarriesAFullBlock:
    """The wire ceilings are derived from BLOCK_SIZE_LIMIT rather than
    chosen separately, so a block at the consensus limit can actually
    reach a peer. They used to sit well under it, and a block past the
    real ceiling was refused with no log line on the receiving side, so
    the builder simply lost the height with nothing to explain it."""

    def test_chunk_ceiling_covers_the_consensus_block_limit(self):
        from params import BLOCK_SIZE_LIMIT
        # Worst case is a payload that did not compress at all, which is
        # close to true for a block full of FALCON signatures.
        needed = -(-BLOCK_SIZE_LIMIT // peer_udp.MAX_CHUNK_SIZE)
        assert peer_udp.MAX_CHUNK_TOTAL >= needed
        assert peer_udp.MAX_INFLATE_BYTES >= BLOCK_SIZE_LIMIT

    def test_chunk_total_still_fits_the_wire_header(self):
        # chunk_total is packed as a signed short.
        assert peer_udp.MAX_CHUNK_TOTAL <= 32767

    def test_a_block_sized_payload_round_trips(self):
        import zlib
        payload = zlib.compress(os.urandom(6 * 1024 * 1024), 1)
        chunks = peer_udp._split(payload)
        assert len(chunks) <= peer_udp.MAX_CHUNK_TOTAL
        r = peer_udp._Reassembler()
        out = None
        for idx, c in enumerate(chunks):
            out = r.feed(("1.2.3.4", 9), 779, idx, len(chunks), c)
        assert out == payload


class TestReassemblyMemoryCap:
    def test_total_held_bytes_are_bounded(self):
        # A much larger per-message ceiling needs a bound on how many
        # partial messages can be held at once, or a peer table could pin
        # arbitrary memory by starting messages and never finishing them.
        r = peer_udp._Reassembler(max_bytes=4096)
        for msg_id in range(50):
            r.feed(("1.2.3.4", 9), msg_id, 0, 8, b"z" * 1024)
        assert r.held_bytes() <= 4096


class TestSyncRequestsNeedAReachableSource:
    """A GETSYNC is one small datagram whose source nothing verifies, and
    the reply is the largest message this protocol makes, so a forged
    source turned this node into an amplifier. Membership is the fast
    path; anything else proves it can receive first."""

    def _transport(self, pool):
        t = peer_udp.UDPTransport(port=0, genesis_hash="ab" * 32,
                                  on_block=lambda *a: None, on_tx=lambda *a: None,
                                  on_peers=lambda *a: None, pool=pool)
        return t

    def test_an_existing_peer_is_served_immediately(self):
        pool = MagicMock()
        pool.all_addrs.return_value = ["1.2.3.4:9000"]
        t = self._transport(pool)
        assert t._may_serve_sync("1.2.3.4:9000") is True

    def test_an_unknown_address_is_not_served_yet(self):
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        assert t._may_serve_sync("9.9.9.9:9000") is False

    def test_an_unknown_address_starts_a_confirmation(self):
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        t._waiters = MagicMock()
        t._start_confirmation("9.9.9.9:9000", lambda: None)
        t._waiters.submit.assert_called_once()

    def test_a_confirmed_address_is_served_without_pinging_again(self):
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        t._routable["9.9.9.9:9000"] = time.monotonic()
        assert t._may_serve_sync("9.9.9.9:9000") is True

    def test_a_stale_confirmation_expires(self):
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        t._routable["9.9.9.9:9000"] = (time.monotonic()
                                       - peer_udp.ROUTABLE_TTL_SECONDS - 1)
        assert t._may_serve_sync("9.9.9.9:9000") is False

    def test_confirmations_in_flight_are_bounded(self):
        # Otherwise a flood of forged sources is a flood of outbound PINGs.
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        t._waiters = MagicMock()
        for i in range(peer_udp.ROUTABLE_MAX_PENDING + 20):
            t._start_confirmation(f"9.9.{i // 256}.{i % 256}:9000", lambda: None)
        assert len(t._routable_pending) <= peer_udp.ROUTABLE_MAX_PENDING

    def test_one_address_does_not_start_two_confirmations(self):
        pool = MagicMock()
        pool.all_addrs.return_value = []
        t = self._transport(pool)
        t._waiters = MagicMock()
        t._start_confirmation("9.9.9.9:9000", lambda: None)
        t._start_confirmation("9.9.9.9:9000", lambda: None)
        assert t._waiters.submit.call_count == 1


class TestStrangerStillBootstraps:
    """Two live transports: the gate above must not cost a node that has
    never spoken to us its first sync. It is served on the original
    request, not after a timeout and a retry."""

    def _pair(self, ports):
        from peerpool import PeerPool
        gen = "ab" * 32
        chain = [{"height": h, "hash": f"{h:064x}"} for h in range(5)]
        made = []
        for port in ports:
            pool = PeerPool()
            t = peer_udp.UDPTransport(port=port, genesis_hash=gen,
                                      on_block=lambda *a: None,
                                      on_tx=lambda *a: None,
                                      on_peers=lambda *a: None, pool=pool)
            t.set_chain_provider(lambda f, to, c=chain: c[f:(to or 4) + 1])
            t.set_tip_provider(lambda c=chain: (4, c[-1]["hash"], "w", "0", 0))
            t.start()
            made.append((t, pool))
        time.sleep(0.4)
        return made

    def test_a_stranger_is_served_on_its_first_request(self):
        (server, server_pool), (client, _) = self._pair([19301, 19302])
        try:
            assert server_pool.all_addrs() == [], "precondition: client is a stranger"
            resp = client.request_sync(f"127.0.0.1:{server.port}",
                                       from_h=0, to_h=4, timeout=8)
            assert resp is not None, "a new node was refused its first sync"
            assert len(resp["chain"]) == 5
        finally:
            server.stop(); client.stop()

    def test_later_pages_need_no_further_confirmation(self):
        (server, _), (client, __) = self._pair([19303, 19304])
        try:
            for _ in range(3):
                resp = client.request_sync(f"127.0.0.1:{server.port}",
                                           from_h=0, to_h=4, timeout=8)
                assert resp is not None and len(resp["chain"]) == 5
        finally:
            server.stop(); client.stop()
