"""Tests rendezvous()'s own race/cleanup logic in isolation, by
substituting the four network-facing calls it makes (_join_relay,
_recv_message, _attempt_connect, _join_session) with deterministic
fakes. This is the logic you actually asked to have verified: which side
wins when both the listener and the dialer could succeed, and that a
loss on one path doesn't leave the other hanging or produce a wrong
result. The network mechanics those four calls perform are covered
separately (test_relay_client_loopback.py, test_relay_wire.py); this file
assumes they work and checks the orchestration built on top of them.
"""

import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import relay_client as rc  # noqa: E402
import relay_wire as wire  # noqa: E402

GENESIS = "deadbeef" * 8
US   = "10.0.0.1:9000"
THEM = "10.0.0.2:9001"
RELAY_URL = "relay://198.51.100.1:22067/?id=fake"


class _FakeJoinSocket:
    """Stand-in for the TLS socket _join_relay would normally hand back:
    settimeout is a no-op, close is a no-op, nothing else is touched
    because _recv_message itself is monkeypatched per test."""
    def settimeout(self, *a, **k): pass
    def close(self): pass


def _patch_common(monkeypatch):
    monkeypatch.setattr(rc, "_join_relay",
                         lambda *a, **k: _FakeJoinSocket())


FAKE_SESSION_SOCKET = object()


def _patch_join_session(monkeypatch, expected_invitation):
    def _fake_join_session(invitation, host, timeout=None):
        assert invitation is expected_invitation
        return FAKE_SESSION_SOCKET
    monkeypatch.setattr(rc, "_join_session", _fake_join_session)


class TestRendezvousRace:
    def test_listener_wins_when_invited_first(self, monkeypatch):
        """We get invited (someone else's ConnectRequest matched us)
        before our own outgoing attempt would succeed."""
        _patch_common(monkeypatch)
        invitation = wire.SessionInvitation(
            from_=b"x", key=b"sesskey", address=b"198.51.100.1",
            port=12345, server_socket=True)

        def fake_recv_message(sock, timeout=None):
            return invitation
        monkeypatch.setattr(rc, "_recv_message", fake_recv_message)

        def slow_attempt_connect(*a, **k):
            time.sleep(2)  # loses the race
            return None
        monkeypatch.setattr(rc, "_attempt_connect", slow_attempt_connect)
        _patch_join_session(monkeypatch, invitation)

        result = rc.rendezvous(RELAY_URL, GENESIS, US, THEM, timeout=3)
        assert result is FAKE_SESSION_SOCKET

    def test_dialer_wins_when_our_connect_succeeds_first(self, monkeypatch):
        """Our own ConnectRequest matches before anything is pushed to us."""
        _patch_common(monkeypatch)

        def hanging_recv_message(sock, timeout=None):
            time.sleep(2)
            raise socket.timeout()
        monkeypatch.setattr(rc, "_recv_message", hanging_recv_message)

        invitation = wire.SessionInvitation(
            from_=b"y", key=b"sesskey2", address=b"198.51.100.1",
            port=54321, server_socket=False)

        def fast_attempt_connect(*a, **k):
            return invitation
        monkeypatch.setattr(rc, "_attempt_connect", fast_attempt_connect)
        _patch_join_session(monkeypatch, invitation)

        result = rc.rendezvous(RELAY_URL, GENESIS, US, THEM, timeout=3)
        assert result is FAKE_SESSION_SOCKET

    def test_raises_relay_error_when_neither_side_succeeds(self, monkeypatch):
        """Both paths exhaust their attempts without a session: this must
        surface as a clean, catchable failure (the last-resort caller in
        discovery.py just falls through to 'still unreachable'), not hang
        or raise something unexpected."""
        _patch_common(monkeypatch)

        def hanging_recv_message(sock, timeout=None):
            time.sleep(2)
            raise socket.timeout()
        monkeypatch.setattr(rc, "_recv_message", hanging_recv_message)
        monkeypatch.setattr(rc, "_attempt_connect", lambda *a, **k: None)

        try:
            rc.rendezvous(RELAY_URL, GENESIS, US, THEM, timeout=1)
            assert False, "should have raised RelayError"
        except rc.RelayError as e:
            assert "no session" in str(e)

    def test_error_message_never_leaks_full_ids(self, monkeypatch):
        """Device IDs are truncated in the failure message (see
        rendezvous's f-string), not printed in full -- these end up in
        logs, and there's no reason to hand out the whole 32-byte
        identifier when 8 hex chars is enough to correlate a retry."""
        _patch_common(monkeypatch)
        monkeypatch.setattr(rc, "_recv_message",
                             lambda *a, **k: (_ for _ in ()).throw(socket.timeout()))
        monkeypatch.setattr(rc, "_attempt_connect", lambda *a, **k: None)
        try:
            rc.rendezvous(RELAY_URL, GENESIS, US, THEM, timeout=1)
            assert False
        except rc.RelayError as e:
            msg = str(e)
            full_id = rc.identity.device_id_for(GENESIS, US).hex()
            assert full_id not in msg
