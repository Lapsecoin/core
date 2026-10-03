"""A node's advertised https address: what is accepted, how peers carry it,
and what a light client does with it."""

import os
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import http_probe
import peerpool as peerpool_mod
from public_url import parse_public_url


class TestParse:
    @pytest.mark.parametrize("given,expected", [
        ("https://node.example.org", "https://node.example.org"),
        ("https://node.example.org/", "https://node.example.org"),
        ("HTTPS://Node.Example.ORG", "https://node.example.org"),
        ("https://node.example.org:8443", "https://node.example.org:8443"),
        ("https://node.example.org:443", "https://node.example.org"),
        ("  https://a.b.co  ", "https://a.b.co"),
    ])
    def test_accepts_a_public_https_name(self, given, expected):
        assert parse_public_url(given) == expected

    @pytest.mark.parametrize("bad", [
        "http://node.example.org",             # not encrypted
        "node.example.org",                    # no scheme
        "https://203.0.113.7",                 # an IP: no certificate names it
        "https://203.0.113.7:8443",
        "https://[2001:db8::1]",
        "https://localhost",                   # this machine
        "https://localhost:8333",
        "https://printer.local",               # this network
        "https://box.lan",
        "https://intranet",                    # no dot: not a public name
        "https://user:pw@node.example.org",    # a login
        "https://node.example.org/api",        # a path
        "https://node.example.org?x=1",
        "https://node.example.org#frag",
        "https://",
        "https://node.example.org:notaport",
        7, ["https://node.example.org"], {"u": 1}, b"https://node.example.org",
        "ftp://node.example.org",
        "", None,
    ])
    def test_refuses_the_rest(self, bad):
        with pytest.raises(ValueError):
            parse_public_url(bad)


class TestProbeLearnsIt:
    def _answer(self, body, status=200):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return mock.patch("urllib.request.urlopen", return_value=resp)

    def test_a_peers_https_address_is_read_from_the_probe_it_already_makes(self):
        with self._answer(b'{"iid": "x", "public_url": "https://peer.example.org"}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, "https://peer.example.org", None)

    def test_an_unacceptable_one_is_dropped_but_the_peer_still_answers(self):
        for bad in ("http://peer.example.org", "https://10.0.0.1", "https://localhost", 7, None):
            body = ('{"public_url": %s}' % ("null" if bad is None else
                                            f'"{bad}"' if isinstance(bad, str) else bad)).encode()
            with self._answer(body):
                assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, None, None), bad

    def test_a_peers_relay_key_is_read_too(self):
        from oblivious import ObliviousService
        from tests.test_api import _MemoryMeta
        key = ObliviousService(_MemoryMeta()).public_b64
        with self._answer(('{"oblivious_key": "%s"}' % key).encode()):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, None, key)

    def test_a_bad_key_is_dropped_but_the_peer_still_answers(self):
        for bad in ('"nope"', '"AAAA"', "7", "null", '["x"]', '""'):
            with self._answer(('{"oblivious_key": %s}' % bad).encode()):
                assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, None, None), bad

    def test_a_peer_with_none_still_answers(self):
        with self._answer(b'{"height": 5}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, None, None)

    def test_a_reply_that_is_not_json_still_counts_as_answering(self):
        with self._answer(b"<html>"):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (True, None, None)

    def test_our_own_instance_is_still_recognised(self):
        with self._answer(b'{"iid": "me", "public_url": "https://me.example.org"}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1, own_iid="me") == (None, None, None)

    def test_an_error_is_not_reachable(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (False, None, None)

    def test_a_bad_status_is_not_reachable(self):
        with self._answer(b"{}", status=503):
            assert http_probe._probe_one("1.2.3.4:8333", 1) == (False, None, None)


class TestPoolHoldsIt:
    def test_recorded_and_listed(self):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_public_url("8.8.8.8:8333", "https://a.example.org")
        assert pool.public_urls() == {"8.8.8.8:8333": "https://a.example.org"}

    def test_cleared_with_none(self):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_public_url("8.8.8.8:8333", "https://a.example.org")
        pool.set_public_url("8.8.8.8:8333", None)
        assert pool.public_urls() == {}

    def test_ignored_for_a_peer_not_held(self):
        pool = peerpool_mod.PeerPool()
        pool.set_public_url("8.8.8.8:8333", "https://a.example.org")
        assert pool.public_urls() == {}

    def test_forgotten_with_the_peer(self):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_public_url("8.8.8.8:8333", "https://a.example.org")
        pool.remove("8.8.8.8:8333")
        assert pool.public_urls() == {}


class TestPoolHoldsRelayKeys:
    KEY = "A" * 43 + "="

    def test_recorded_and_listed(self):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_oblivious_key("8.8.8.8:8333", self.KEY)
        assert pool.oblivious_keys() == {"8.8.8.8:8333": self.KEY}

    def test_cleared_with_none(self):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_oblivious_key("8.8.8.8:8333", self.KEY)
        pool.set_oblivious_key("8.8.8.8:8333", None)
        assert pool.oblivious_keys() == {}

    def test_ignored_for_a_peer_not_held_and_forgotten_with_it(self):
        pool = peerpool_mod.PeerPool()
        pool.set_oblivious_key("8.8.8.8:8333", self.KEY)
        assert pool.oblivious_keys() == {}
        pool.add("8.8.8.8:8333")
        pool.set_oblivious_key("8.8.8.8:8333", self.KEY)
        pool.remove("8.8.8.8:8333")
        assert pool.oblivious_keys() == {}


class TestNodeAdvertisesIt:
    def _app(self, public_url):
        from tests.test_api import _InfoNode
        from chainstate import ChainState
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.add("8.8.4.4:8333")
        pool.set_http_reachable("8.8.8.8:8333", True)
        pool.set_http_reachable("8.8.4.4:8333", True)
        pool.set_public_url("8.8.8.8:8333", "https://peer.example.org")
        return api.create_app(_InfoNode(ChainState.from_genesis()), pool,
                              public_url=public_url).test_client()

    def test_info_carries_it(self):
        assert self._app("https://me.example.org").get("/api/info").get_json()["public_url"] \
            == "https://me.example.org"

    def test_info_carries_none_when_there_is_none(self):
        assert self._app(None).get("/api/info").get_json()["public_url"] is None

    def test_the_http_list_names_its_own_first_then_its_peers(self):
        data = self._app("https://me.example.org").get("/api/peers/http").get_json()
        assert data["https"] == ["https://me.example.org", "https://peer.example.org"]
        assert "8.8.8.8:8333" in data["nodes"]

    def test_with_none_of_its_own_it_still_passes_on_its_peers(self):
        assert self._app(None).get("/api/peers/http").get_json()["https"] == [
            "https://peer.example.org"]

    def test_a_peer_it_lists_twice_is_listed_once(self):
        from tests.test_light import _FullNode
        from chainstate import ChainState
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.set_http_reachable("8.8.8.8:8333", True)
        pool.set_public_url("8.8.8.8:8333", "https://me.example.org")
        client = api.create_app(_FullNode(ChainState.from_genesis()), pool,
                                public_url="https://me.example.org").test_client()
        assert client.get("/api/peers/http").get_json()["https"] == ["https://me.example.org"]

    def test_the_private_app_says_it_too(self):
        from tests.test_api import _InfoNode
        from chainstate import ChainState
        app = api.create_private_app(_InfoNode(ChainState.from_genesis()),
                                     peerpool_mod.PeerPool(), public_url="https://me.example.org")
        assert app.test_client().get("/api/info").get_json()["public_url"] == "https://me.example.org"


class TestBehindAReverseProxy:
    """With TLS terminated by a proxy on the same machine, every request
    arrives from 127.0.0.1. Rate limits are per client, so the client's
    address has to come from the proxy, and only from the proxy."""

    def _client(self):
        from tests.test_light import _FullNode
        from chainstate import ChainState
        from flask import request
        app = api.create_app(_FullNode(ChainState.from_genesis()), peerpool_mod.PeerPool())
        app.add_url_rule("/_ip", "ip", lambda: request.remote_addr)
        return app.test_client()

    def test_the_proxy_is_believed_when_it_is_the_proxy(self):
        r = self._client().get("/_ip", headers={"X-Forwarded-For": "198.51.100.9"},
                               environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.get_data(as_text=True) == "198.51.100.9"

    def test_the_last_entry_is_the_one_the_proxy_added(self):
        """Earlier entries were written by the client and mean nothing."""
        r = self._client().get("/_ip", headers={"X-Forwarded-For": "1.1.1.1, 198.51.100.9"},
                               environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.get_data(as_text=True) == "198.51.100.9"

    def test_a_stranger_cannot_choose_their_own_address(self):
        r = self._client().get("/_ip", headers={"X-Forwarded-For": "1.1.1.1"},
                               environ_overrides={"REMOTE_ADDR": "203.0.113.5"})
        assert r.get_data(as_text=True) == "203.0.113.5"

    def test_the_ipv6_loopback_counts_as_the_proxy(self):
        r = self._client().get("/_ip", headers={"X-Forwarded-For": "198.51.100.9"},
                               environ_overrides={"REMOTE_ADDR": "::1"})
        assert r.get_data(as_text=True) == "198.51.100.9"

    def test_no_header_means_no_change(self):
        r = self._client().get("/_ip", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.get_data(as_text=True) == "127.0.0.1"


class TestFlag:
    def test_a_node_can_decline_to_relay(self):
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "main.py"),
                              "--help"], capture_output=True, text=True, timeout=120)
        assert "--no-relay" in out.stdout

    def _run(self, *args):
        main = os.path.join(os.path.dirname(__file__), "..", "main.py")
        return subprocess.run([sys.executable, main, "--no-gui", *args], capture_output=True,
                              text=True, timeout=120)

    @pytest.mark.parametrize("bad", ["http://node.example.org", "https://203.0.113.7",
                                     "https://localhost"])
    def test_a_bad_public_url_stops_the_node_at_once_and_says_why(self, bad):
        out = self._run("--public-url", bad)
        assert out.returncode != 0
        assert "--public-url" in out.stderr
