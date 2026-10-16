"""A node's public https address (--public-url): what is accepted, and how a
node behind a reverse proxy tells clients apart."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
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


class TestBehindAReverseProxy:
    """With TLS terminated by a proxy on the same machine, every request
    arrives from 127.0.0.1. Rate limits are per client, so the client's
    address has to come from the proxy, and only from the proxy."""

    def _client(self):
        from tests.wallet_app import _FullNode
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
