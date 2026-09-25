"""Loopback integration test for relay_client.py: not a real Syncthing
relay (none is reachable from this environment), but a local TLS server
that speaks the exact same relay_wire framing, so this actually exercises
_join_relay's socket + TLS + XDR-framing path for real, over a real
handshake, rather than only unit-level pieces in isolation.

This validates: our client cert is genuinely usable in a live TLS
handshake, our framing round-trips over a real socket in both directions,
and _join_relay correctly interprets a real server's Response. It cannot
validate actual interop with Syncthing's server implementation (no such
server reachable here); see relay_client.py's module docstring.
"""

import os
import socket
import ssl
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import relay_client as rc  # noqa: E402
import relay_identity as identity  # noqa: E402
import relay_wire as wire  # noqa: E402

GENESIS  = "deadbeef" * 8
OUR_ADDR = "203.0.113.10:9000"


def _make_server_ssl_context():
    """Any valid cert works, our client doesn't verify the server's
    identity (see _tls_context_for's docstring: CERT_NONE by design)."""
    key_pem, cert_pem = identity.identity_pem_for(GENESIS, "relay-server-fake")
    key_fd, key_path = tempfile.mkstemp(suffix=".pem")
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    os.write(key_fd, key_pem)
    os.write(cert_fd, cert_pem)
    os.close(key_fd)
    os.close(cert_fd)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    os.unlink(key_path)
    os.unlink(cert_path)
    return ctx


class _FakeRelayServer:
    """Accepts exactly one TLS connection, reads one message, and replies
    with whatever the test configured. Runs in a background thread."""

    def __init__(self, reply_msg, expect_type=None):
        self.reply_msg = reply_msg
        self.expect_type = expect_type
        self.received = None
        self.error = None
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw.bind(("127.0.0.1", 0))
        raw.listen(1)
        self.host, self.port = raw.getsockname()
        self._raw = raw
        self._ctx = _make_server_ssl_context()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        try:
            conn, _ = self._raw.accept()
            tls_conn = self._ctx.wrap_socket(conn, server_side=True)
            tls_conn.settimeout(5)
            header = rc._recv_exact(tls_conn, wire.HEADER_LEN)
            msg_type, msg_len = wire.decode_header(header)
            payload = rc._recv_exact(tls_conn, msg_len) if msg_len else b""
            self.received = wire.decode_payload(msg_type, payload)
            tls_conn.sendall(wire.encode_message(self.reply_msg))
            tls_conn.close()
        except Exception as e:
            self.error = e

    def relay_url(self):
        return f"relay://{self.host}:{self.port}/?id=fake"

    def join(self):
        self._thread.join(timeout=5)


class TestJoinRelayLoopback:
    def test_successful_join(self):
        server = _FakeRelayServer(
            reply_msg=wire.Response(code=wire.RESPONSE_SUCCESS, message="")
        ).start()
        sock = rc._join_relay(server.relay_url(), GENESIS, OUR_ADDR, timeout=5)
        try:
            server.join()
            assert server.error is None, server.error
            assert isinstance(server.received, wire.JoinRelayRequest)
            assert server.received.token == ""
        finally:
            sock.close()

    def test_join_refused(self):
        server = _FakeRelayServer(
            reply_msg=wire.Response(code=wire.RESPONSE_WRONG_TOKEN, message="nope")
        ).start()
        try:
            rc._join_relay(server.relay_url(), GENESIS, OUR_ADDR, timeout=5)
            assert False, "should have raised RelayError"
        except rc.RelayError as e:
            assert "nope" in str(e) or "refused" in str(e)
        server.join()

    def test_relay_full(self):
        server = _FakeRelayServer(reply_msg=wire.RelayFull()).start()
        try:
            rc._join_relay(server.relay_url(), GENESIS, OUR_ADDR, timeout=5)
            assert False, "should have raised RelayError"
        except rc.RelayError as e:
            assert "full" in str(e).lower()
        server.join()

    def test_token_from_url_is_sent(self):
        server = _FakeRelayServer(
            reply_msg=wire.Response(code=wire.RESPONSE_SUCCESS, message="")
        ).start()
        url = f"relay://{server.host}:{server.port}/?id=fake&token=secret123"
        sock = rc._join_relay(url, GENESIS, OUR_ADDR, timeout=5)
        try:
            server.join()
            assert server.received.token == "secret123"
        finally:
            sock.close()


class TestParseRelayUrl:
    def test_basic(self):
        host, port, token = rc._parse_relay_url("relay://1.2.3.4:22067/?id=X")
        assert host == "1.2.3.4"
        assert port == 22067
        assert token == ""

    def test_with_token(self):
        host, port, token = rc._parse_relay_url("relay://1.2.3.4:22067/?id=X&token=abc")
        assert token == "abc"

    def test_rejects_non_relay_scheme(self):
        try:
            rc._parse_relay_url("http://1.2.3.4:22067/")
            assert False, "should have raised"
        except rc.RelayError:
            pass

    def test_rejects_missing_port(self):
        try:
            rc._parse_relay_url("relay://1.2.3.4/?id=X")
            assert False, "should have raised"
        except rc.RelayError:
            pass
