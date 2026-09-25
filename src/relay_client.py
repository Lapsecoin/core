"""Relay session establishment: the part relay_wire.py and
relay_identity.py exist to support. NOT validated end-to-end against a
live relay, there's no way to dial one from this development environment
to confirm it. Everything below follows the spec as sourced in
relay_wire.py's docstring; treat a first real run against
relays.syncthing.net as the actual test, not this module's existence.
Left uncalled from discovery.py (SAME_NAT_RELAY_FALLBACK stays False)
until that's happened.

Rendezvous strategy
--------------------
Syncthing's own client has a fixed caller/callee split: one side joins
and waits, the other dials in naming a target it already knows the device
ID of from a prior pairing. Our two peers are symmetric, neither is
designated in advance, so both sides run *both* roles at once:
  1. Join the relay (register, then wait for a pushed SessionInvitation).
  2. Concurrently, send our own ConnectRequest naming the other side's
     device ID (predicted from their addr, see relay_identity.py), retrying
     on ResponseNotFound for the same reason: the other side may not have
     finished joining yet.
Whichever half completes first (we get invited, or our own connect
succeeds) wins; the other is cancelled. This is the same "attempt both
directions at once" trick simultaneous-open NAT punching already uses
elsewhere in this codebase (see discovery.py's punch flow), applied at
the relay-rendezvous layer instead.

Session mode
------------
Once a SessionInvitation is in hand (pushed to the joined side, or
returned directly to the connecting side, per the spec text: "a session
key, address, and port for both parties to establish a plain-text session
connection"), each side opens a *plain* TCP connection to that
address:port and sends JoinSessionRequest(key=invitation.key) to bind
into that specific session. From that point the socket is a raw,
opaque byte pipe between the two peers; nothing about its framing is this
module's concern anymore, it hands back a bare socket for the UDP-over-
relay bridge layer (not yet built) to use.
"""

import logging
import socket
import ssl
import tempfile
import threading
import time
import os
from urllib.parse import urlparse, parse_qs

import relay_identity as identity
import relay_wire as wire

log = logging.getLogger("ec.relayclient")

CONTROL_TIMEOUT = 10   # seconds for each TLS control-channel round trip
SESSION_TIMEOUT = 15   # seconds to wait for a session to materialize overall
CONNECT_RETRY_INTERVAL = 1.5  # seconds between our own ConnectRequest retries


class RelayError(Exception):
    pass


def _parse_relay_url(relay_url: str):
    """"relay://host:port/?id=...&token=..." -> (host, port, token).
    The id query param is the relay's OWN device ID, not used here (we
    don't verify the relay's identity beyond the TCP endpoint answering
    correctly, same trust level as the rest of this project's peer
    discovery: reachability is the signal, not a certificate chain)."""
    parsed = urlparse(relay_url)
    if parsed.scheme != "relay":
        raise RelayError(f"not a relay:// URL: {relay_url!r}")
    if not parsed.hostname or not parsed.port:
        raise RelayError(f"missing host or port: {relay_url!r}")
    token = parse_qs(parsed.query).get("token", [""])[0]
    return parsed.hostname, parsed.port, token


def _tls_context_for(genesis_hash: str, our_addr: str) -> ssl.SSLContext:
    """A client TLS context presenting our deterministic identity. Verifies
    nothing about the relay's own certificate (CERT_NONE): the relay pool
    is public, unauthenticated infrastructure by design (see
    relay_pool.py), and actual peer trust happens one layer up at the
    genesis-hash PING/PONG handshake once bytes start flowing, same as a
    direct connection. Pinning the relay's cert would need a bundled trust
    list this project doesn't otherwise maintain, for a hop that never
    sees plaintext traffic anyway once the peer-level handshake covers it."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    key_pem, cert_pem = identity.identity_pem_for(genesis_hash, our_addr)
    key_fd, key_path = tempfile.mkstemp(suffix=".pem")
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    try:
        os.write(key_fd, key_pem)
        os.write(cert_fd, cert_pem)
        os.close(key_fd)
        os.close(cert_fd)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        os.unlink(key_path)
        os.unlink(cert_path)
    return ctx


def _send(sock, msg):
    sock.sendall(wire.encode_message(msg))


def _recv_message(sock, timeout=CONTROL_TIMEOUT):
    sock.settimeout(timeout)
    header = _recv_exact(sock, wire.HEADER_LEN)
    msg_type, msg_len = wire.decode_header(header)
    payload = _recv_exact(sock, msg_len) if msg_len else b""
    return wire.decode_payload(msg_type, payload)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RelayError("connection closed mid-message")
        buf += chunk
    return buf


def _join_relay(relay_url: str, genesis_hash: str, our_addr: str,
                 timeout=CONTROL_TIMEOUT) -> ssl.SSLSocket:
    """Dial relay_url, present our identity, JoinRelayRequest. Returns the
    open TLS socket (kept open, this connection is what the relay pushes
    a SessionInvitation to later) on success. Raises RelayError on
    RelayFull or an explicit failure Response."""
    host, port, token = _parse_relay_url(relay_url)
    ctx = _tls_context_for(genesis_hash, our_addr)
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    _send(sock, wire.JoinRelayRequest(token=token))
    reply = _recv_message(sock, timeout=timeout)
    if isinstance(reply, wire.RelayFull):
        sock.close()
        raise RelayError(f"relay full: {relay_url}")
    if isinstance(reply, wire.Response) and reply.code != wire.RESPONSE_SUCCESS:
        sock.close()
        raise RelayError(f"join refused ({reply.code}): {reply.message}")
    if not isinstance(reply, wire.Response):
        sock.close()
        raise RelayError(f"unexpected reply to JoinRelayRequest: {reply!r}")
    return sock


def _attempt_connect(relay_url: str, genesis_hash: str, our_addr: str,
                      target_id: bytes, deadline: float):
    """Repeatedly dial relay_url in 'temporary' mode and ConnectRequest
    target_id until it succeeds, the deadline passes, or a non-retryable
    error occurs. Returns a SessionInvitation on success, None if the
    deadline passed without one (not an error: the other side may simply
    have won by inviting us first)."""
    host, port, token = _parse_relay_url(relay_url)
    while time.monotonic() < deadline:
        try:
            ctx = _tls_context_for(genesis_hash, our_addr)
            raw = socket.create_connection(
                (host, port), timeout=max(0.5, deadline - time.monotonic()))
            sock = ctx.wrap_socket(raw, server_hostname=host)
            try:
                if token:
                    # Some relays require joining before connect is
                    # honored; harmless no-op send if not required.
                    pass
                _send(sock, wire.ConnectRequest(id_=target_id))
                reply = _recv_message(
                    sock, timeout=max(0.5, deadline - time.monotonic()))
                if isinstance(reply, wire.SessionInvitation):
                    return reply
                if isinstance(reply, wire.Response):
                    if reply.code == wire.RESPONSE_NOT_FOUND:
                        # Other side hasn't joined yet; expected during the
                        # race, retry after a short wait rather than fail.
                        pass
                    else:
                        log.debug("[relay] connect refused (%d): %s",
                                  reply.code, reply.message)
                        return None
            finally:
                sock.close()
        except (OSError, RelayError, ssl.SSLError):
            log.debug("[relay] connect attempt to %s failed", relay_url, exc_info=True)
        time.sleep(CONNECT_RETRY_INTERVAL)
    return None


def _join_session(invitation: wire.SessionInvitation, host_fallback: str,
                   timeout=CONTROL_TIMEOUT) -> socket.socket:
    """Open the plain (non-TLS) session-mode socket named by invitation
    and bind into it with JoinSessionRequest. Returns the raw socket,
    ready to carry opaque bytes to the other peer, relay in the middle."""
    addr_str = invitation.address.decode("utf-8", errors="ignore") or host_fallback
    sock = socket.create_connection((addr_str, invitation.port), timeout=timeout)
    sock.sendall(wire.encode_message(wire.JoinSessionRequest(key=invitation.key)))
    return sock


def rendezvous(relay_url: str, genesis_hash: str, our_addr: str, peer_addr: str,
               timeout=SESSION_TIMEOUT):
    """Symmetric relay rendezvous. Both sides call this identically, each
    with their own addr as our_addr and the other's as peer_addr.

    Returns an open, raw socket to the peer (relayed) on success.
    Raises RelayError/OSError/TimeoutError on failure; callers already
    treat this as a last-resort fallback (see discovery.py), so a failure
    here just means falling through to "still unreachable this round",
    not a crash.
    """
    deadline = time.monotonic() + timeout
    my_id = identity.device_id_for(genesis_hash, our_addr)
    target_id = identity.device_id_for(genesis_hash, peer_addr)

    result = {"invitation": None, "host": None}
    result_lock = threading.Lock()

    def _mark(invitation, host):
        with result_lock:
            if result["invitation"] is None:
                result["invitation"] = invitation
                result["host"] = host

    join_sock = _join_relay(relay_url, genesis_hash, our_addr, timeout=CONTROL_TIMEOUT)

    def _listen_for_invite():
        try:
            join_sock.settimeout(max(0.1, deadline - time.monotonic()))
            while time.monotonic() < deadline:
                try:
                    msg = _recv_message(join_sock, timeout=max(0.1, deadline - time.monotonic()))
                except socket.timeout:
                    continue
                if isinstance(msg, wire.SessionInvitation):
                    _mark(msg, _parse_relay_url(relay_url)[0])
                    return
        except (OSError, RelayError, ssl.SSLError):
            log.debug("[relay] join-side listen failed", exc_info=True)

    def _attempt_outgoing():
        inv = _attempt_connect(relay_url, genesis_hash, our_addr, target_id, deadline)
        if inv is not None:
            _mark(inv, _parse_relay_url(relay_url)[0])

    listener = threading.Thread(target=_listen_for_invite, daemon=True)
    dialer = threading.Thread(target=_attempt_outgoing, daemon=True)
    listener.start()
    dialer.start()
    listener.join(timeout=max(0.0, deadline - time.monotonic()) + 0.5)
    dialer.join(timeout=max(0.0, deadline - time.monotonic()) + 0.5)

    try:
        join_sock.close()
    except OSError:
        pass

    with result_lock:
        invitation, host = result["invitation"], result["host"]

    if invitation is None:
        raise RelayError(f"no session established via {relay_url} within {timeout}s "
                          f"(my_id={my_id.hex()[:8]}... target_id={target_id.hex()[:8]}...)")

    return _join_session(invitation, host, timeout=CONTROL_TIMEOUT)
