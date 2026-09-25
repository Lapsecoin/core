import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import relay_wire as rw  # noqa: E402


def _roundtrip(msg):
    frame = rw.encode_message(msg)
    header, rest = frame[:rw.HEADER_LEN], frame[rw.HEADER_LEN:]
    msg_type, msg_len = rw.decode_header(header)
    assert msg_len == len(rest)
    return rw.decode_payload(msg_type, rest)


class TestEmptyMessages:
    def test_ping_pong_relayfull_roundtrip(self):
        for cls in (rw.Ping, rw.Pong, rw.RelayFull):
            assert isinstance(_roundtrip(cls()), cls)

    def test_empty_payload_is_zero_length_frame(self):
        frame = rw.encode_message(rw.Ping())
        assert len(frame) == rw.HEADER_LEN


class TestJoinRelayRequest:
    def test_roundtrip_with_token(self):
        out = _roundtrip(rw.JoinRelayRequest(token="abc123"))
        assert out.token == "abc123"

    def test_roundtrip_empty_token(self):
        out = _roundtrip(rw.JoinRelayRequest(token=""))
        assert out.token == ""

    def test_empty_payload_back_compat(self):
        # Older relays never sent a Token field at all; an empty payload
        # must still decode cleanly rather than raise.
        out = rw.decode_payload(rw.TYPE_JOIN_RELAY_REQUEST, b"")
        assert out.token == ""


class TestOpaqueKeyMessages:
    def test_join_session_request_roundtrip(self):
        key = bytes(range(32))
        out = _roundtrip(rw.JoinSessionRequest(key=key))
        assert out.key == key

    def test_connect_request_roundtrip(self):
        id_ = bytes(range(32))
        out = _roundtrip(rw.ConnectRequest(id_=id_))
        assert out.id == id_

    def test_rejects_oversized_key_on_construction(self):
        try:
            rw.JoinSessionRequest(key=bytes(33))
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass

    def test_rejects_oversized_opaque_length_on_decode(self):
        try:
            rw.decode_payload(rw.TYPE_CONNECT_REQUEST, rw._pack_uint32(9999))
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass


class TestResponse:
    def test_roundtrip_with_message(self):
        out = _roundtrip(rw.Response(code=rw.RESPONSE_SUCCESS, message="ok"))
        assert out.code == rw.RESPONSE_SUCCESS
        assert out.message == "ok"

    def test_roundtrip_empty_message(self):
        out = _roundtrip(rw.Response(code=rw.RESPONSE_NOT_FOUND, message=""))
        assert out.code == rw.RESPONSE_NOT_FOUND
        assert out.message == ""


class TestSessionInvitation:
    def test_roundtrip_full(self):
        inv = rw.SessionInvitation(
            from_=bytes(range(32)), key=bytes(range(16)),
            address=b"203.0.113.5", port=22067, server_socket=True)
        out = _roundtrip(inv)
        assert out.from_ == inv.from_
        assert out.key == inv.key
        assert out.address == inv.address
        assert out.port == 22067
        assert out.server_socket is True

    def test_roundtrip_zero_port_and_false_flag(self):
        inv = rw.SessionInvitation(from_=b"a", key=b"b", address=b"c",
                                    port=0, server_socket=False)
        out = _roundtrip(inv)
        assert out.port == 0
        assert out.server_socket is False

    def test_port_survives_non_4_byte_aligned_neighbours(self):
        # address is 3 bytes (odd, needs padding) immediately before the
        # port field -- the exact case that would expose a padding bug.
        inv = rw.SessionInvitation(from_=b"", key=b"", address=b"xyz",
                                    port=54321, server_socket=True)
        out = _roundtrip(inv)
        assert out.port == 54321

    def test_rejects_oversized_field_on_construction(self):
        try:
            rw.SessionInvitation(from_=bytes(33), key=b"", address=b"",
                                  port=0, server_socket=False)
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass


class TestHeaderValidation:
    def test_rejects_short_header(self):
        try:
            rw.decode_header(b"tooshort")
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass

    def test_rejects_bad_magic(self):
        try:
            rw.decode_header(b"\x00\x00\x00\x00" + b"\x00" * 8)
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass

    def test_rejects_negative_length(self):
        bad = rw._pack_uint32(rw.MAGIC) + rw._pack_int32(rw.TYPE_PING) + rw._pack_int32(-1)
        try:
            rw.decode_header(bad)
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass

    def test_rejects_over_max_length(self):
        bad = (rw._pack_uint32(rw.MAGIC) + rw._pack_int32(rw.TYPE_PING)
                + rw._pack_int32(rw.MAX_MESSAGE_LEN + 1))
        try:
            rw.decode_header(bad)
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass

    def test_accepts_valid_header(self):
        frame = rw.encode_message(rw.Response(code=0, message="hi"))
        msg_type, msg_len = rw.decode_header(frame[:rw.HEADER_LEN])
        assert msg_type == rw.TYPE_RESPONSE
        assert msg_len == len(frame) - rw.HEADER_LEN


class TestUnknownType:
    def test_decode_unknown_type_raises(self):
        try:
            rw.decode_payload(999, b"")
            assert False, "should have raised"
        except rw.RelayProtocolError:
            pass
