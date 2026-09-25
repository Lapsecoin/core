"""Syncthing relay protocol v1 wire format: header + the 8 message shapes,
in plain XDR (RFC 4506) as the relay pool actually speaks it.

Verified against a full local clone of Syncthing's own source, not just
docs or memory (lib/relay/protocol/{protocol,packets,packets_xdr}.go, plus
the actual XDR library it depends on, github.com/calmh/xdr, cloned
separately since Syncthing's own repo only imports it):
  - header = magic(uint32) + messageType(int32) + messageLength(int32),
    12 bytes, messageLength counts the payload only. (packets.go: header
    struct; packets_xdr.go: header.MarshalXDRInto uses plain MarshalUint32
    for all three fields.)
  - magic = 0x9E79BC40, max payload length = 1024 bytes. (protocol.go)
  - message type IDs: packets.go's own `const (... int32 = iota ...)`
    block, in file order: Ping=0, Pong=1, JoinRelayRequest=2,
    JoinSessionRequest=3, Response=4, ConnectRequest=5,
    SessionInvitation=6, RelayFull=7.
  - response codes: protocol.go's `var (Response{0,...} ... )` block:
    Success=0, NotFound=1, AlreadyConnected=2, WrongToken=3,
    UnexpectedMessage=100.
  - field order/types for every message: packets.go's struct defs, mirrored
    1:1 by the classes below.
  - the one thing not directly stated in Syncthing's own repo: exactly how
    MarshalUint16 (SessionInvitation.Port) and MarshalBool (ServerSocket)
    pad their sub-4-byte values. Confirmed in calmh/xdr/marshaller.go:
    MarshalUint16 and MarshalUint8 (which MarshalBool calls) both just
    delegate to MarshalUint32, i.e. every scalar in this protocol,
    regardless of width, is a plain 4-byte big-endian word. Implemented
    that way below and round-trip tested (see tests/test_relay_wire.py).
"""

import struct

MAGIC           = 0x9E79BC40
MAX_MESSAGE_LEN = 1024
HEADER_LEN      = 12  # 3 x uint32/int32, XDR: always 4-byte-aligned

TYPE_PING                = 0
TYPE_PONG                = 1
TYPE_JOIN_RELAY_REQUEST  = 2
TYPE_JOIN_SESSION_REQUEST = 3
TYPE_RESPONSE            = 4
TYPE_CONNECT_REQUEST     = 5
TYPE_SESSION_INVITATION  = 6
TYPE_RELAY_FULL          = 7


class RelayProtocolError(Exception):
    pass


# ---------------------------------------------------------------------
# XDR primitives (RFC 4506): every scalar is a 4-byte big-endian word;
# variable-length opaque data / strings are a 4-byte BE length prefix,
# the raw bytes, then zero-padding out to the next 4-byte boundary.
# ---------------------------------------------------------------------

def _pack_uint32(n: int) -> bytes:
    return struct.pack(">I", n & 0xFFFFFFFF)


def _pack_int32(n: int) -> bytes:
    return struct.pack(">i", n)


def _pack_bool(b: bool) -> bytes:
    return _pack_uint32(1 if b else 0)


def _pad_len(n: int) -> int:
    return (4 - (n % 4)) % 4


def _pack_opaque(data: bytes) -> bytes:
    return _pack_uint32(len(data)) + data + b"\x00" * _pad_len(len(data))


def _pack_string(s: str) -> bytes:
    return _pack_opaque(s.encode("utf-8"))


class _Reader:
    """Sequential XDR reader over an in-memory payload. Raises
    RelayProtocolError rather than a bare struct/IndexError on any
    malformed or truncated input, this parses bytes a stranger's relay
    (or a stranger pretending to be one) sent us."""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise RelayProtocolError("truncated message")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def uint32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def int32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def uint16_padded(self) -> int:
        # See module docstring: inferred 4-byte-word encoding.
        return self.uint32() & 0xFFFF

    def bool_(self) -> bool:
        return self.uint32() != 0

    def opaque(self, max_len=None) -> bytes:
        n = self.uint32()
        if n > MAX_MESSAGE_LEN or (max_len is not None and n > max_len):
            raise RelayProtocolError(f"opaque field too long ({n})")
        data = self._take(n)
        self._take(_pad_len(n))
        return data

    def string(self, max_len=None) -> str:
        return self.opaque(max_len).decode("utf-8", errors="replace")

    def at_end(self) -> bool:
        return self.pos >= len(self.buf)


# ---------------------------------------------------------------------
# Messages. Each mirrors one upstream struct 1:1 (see docstring).
# ---------------------------------------------------------------------

class Ping:
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, buf: bytes): return cls()


class Pong:
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, buf: bytes): return cls()


class RelayFull:
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, buf: bytes): return cls()


class JoinRelayRequest:
    """Token: opaque relay-specific auth token, "" for a relay that
    doesn't require one (the public pool doesn't). Empty payload (older
    protocol revisions had no Token field at all) unmarshals to token=""
    rather than erroring, matching upstream's own back-compat handling."""

    def __init__(self, token: str = ""):
        self.token = token

    def marshal(self) -> bytes:
        return _pack_string(self.token)

    @classmethod
    def unmarshal(cls, buf: bytes):
        if not buf:
            return cls(token="")
        r = _Reader(buf)
        return cls(token=r.string())


class JoinSessionRequest:
    def __init__(self, key: bytes):
        if len(key) > 32:
            raise RelayProtocolError("key too long")
        self.key = key

    def marshal(self) -> bytes:
        return _pack_opaque(self.key)

    @classmethod
    def unmarshal(cls, buf: bytes):
        r = _Reader(buf)
        return cls(key=r.opaque(max_len=32))


class Response:
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message

    def marshal(self) -> bytes:
        return _pack_int32(self.code) + _pack_string(self.message)

    @classmethod
    def unmarshal(cls, buf: bytes):
        r = _Reader(buf)
        code = r.int32()
        message = r.string()
        return cls(code=code, message=message)


# Response codes, as returned by a real relay.
RESPONSE_SUCCESS           = 0
RESPONSE_NOT_FOUND         = 1
RESPONSE_ALREADY_CONNECTED = 2
RESPONSE_WRONG_TOKEN       = 3
RESPONSE_UNEXPECTED        = 100


class ConnectRequest:
    def __init__(self, id_: bytes):
        if len(id_) > 32:
            raise RelayProtocolError("id too long")
        self.id = id_

    def marshal(self) -> bytes:
        return _pack_opaque(self.id)

    @classmethod
    def unmarshal(cls, buf: bytes):
        r = _Reader(buf)
        return cls(id_=r.opaque(max_len=32))


class SessionInvitation:
    def __init__(self, from_: bytes, key: bytes, address: bytes,
                 port: int, server_socket: bool):
        for name, val in (("From", from_), ("Key", key), ("Address", address)):
            if len(val) > 32:
                raise RelayProtocolError(f"{name} too long")
        self.from_ = from_
        self.key = key
        self.address = address
        self.port = port
        self.server_socket = server_socket

    def marshal(self) -> bytes:
        return (_pack_opaque(self.from_) + _pack_opaque(self.key)
                + _pack_opaque(self.address) + _pack_uint32(self.port & 0xFFFF)
                + _pack_bool(self.server_socket))

    @classmethod
    def unmarshal(cls, buf: bytes):
        r = _Reader(buf)
        from_ = r.opaque(max_len=32)
        key = r.opaque(max_len=32)
        address = r.opaque(max_len=32)
        port = r.uint16_padded()
        server_socket = r.bool_()
        return cls(from_=from_, key=key, address=address,
                    port=port, server_socket=server_socket)


_TYPE_TO_CLASS = {
    TYPE_PING: Ping,
    TYPE_PONG: Pong,
    TYPE_JOIN_RELAY_REQUEST: JoinRelayRequest,
    TYPE_JOIN_SESSION_REQUEST: JoinSessionRequest,
    TYPE_RESPONSE: Response,
    TYPE_CONNECT_REQUEST: ConnectRequest,
    TYPE_SESSION_INVITATION: SessionInvitation,
    TYPE_RELAY_FULL: RelayFull,
}
_CLASS_TO_TYPE = {cls: t for t, cls in _TYPE_TO_CLASS.items()}


def encode_message(msg) -> bytes:
    """A full header+payload frame ready to write to the relay socket."""
    msg_type = _CLASS_TO_TYPE.get(type(msg))
    if msg_type is None:
        raise RelayProtocolError(f"unknown message type {type(msg)!r}")
    payload = msg.marshal()
    if len(payload) > MAX_MESSAGE_LEN:
        raise RelayProtocolError("payload too large")
    header = _pack_uint32(MAGIC) + _pack_int32(msg_type) + _pack_int32(len(payload))
    return header + payload


def decode_header(raw: bytes):
    """Parse exactly HEADER_LEN bytes into (msg_type, payload_len).
    Raises RelayProtocolError on a bad magic or an out-of-range length,
    same validation upstream's ReadMessage does before trusting the
    length enough to read that many more bytes off the wire."""
    if len(raw) != HEADER_LEN:
        raise RelayProtocolError("short header")
    r = _Reader(raw)
    magic = r.uint32()
    if magic != MAGIC:
        raise RelayProtocolError("magic mismatch")
    msg_type = r.int32()
    msg_len = r.int32()
    if msg_len < 0 or msg_len > MAX_MESSAGE_LEN:
        raise RelayProtocolError(f"bad length ({msg_len})")
    return msg_type, msg_len


def decode_payload(msg_type: int, payload: bytes):
    """The message object for a (msg_type, payload) pair already split
    out by decode_header + reading exactly that many bytes."""
    cls = _TYPE_TO_CLASS.get(msg_type)
    if cls is None:
        raise RelayProtocolError(f"unknown message type id {msg_type}")
    return cls.unmarshal(payload)
