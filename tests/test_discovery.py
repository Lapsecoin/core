"""Unit tests for discovery.py's self-address guard.

_is_own_addr is the one check both add_bootstrap_peer (the --peer CLI
flag) and _flush_candidates (peer-exchange/DHT/LAN-hint candidates) go
through before ever pinging an address. Before this, add_bootstrap_peer
had no such check at all: a --peer pointed at this node's own address
(a copy-paste mistake, or a config templated the same for every node)
could ping itself and, on a router that hairpins its own public IP back
to the sender, actually get a PONG and self-admit. PeerPool.add has no
notion of which address is "us", so nothing downstream would have
caught it either.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discovery as discovery_mod


def _make_discovery(our_external_addr=None, local_ips=frozenset()):
    udp = MagicMock()
    udp.our_external_addr = our_external_addr
    udp._local_ips = local_ips
    pool = MagicMock()
    return discovery_mod.Discovery(udp, pool, genesis_hash="a" * 64, port=8333), udp


class TestIsOwnAddr:
    def test_matches_confirmed_external_address(self):
        d, _ = _make_discovery(our_external_addr="9.9.9.9:8333")
        assert d._is_own_addr("9.9.9.9:8333") is True

    def test_matches_a_local_interface_ip_regardless_of_port(self):
        d, _ = _make_discovery(local_ips={"192.168.1.5"})
        assert d._is_own_addr("192.168.1.5:9001") is True

    def test_ordinary_peer_address_is_not_own(self):
        d, _ = _make_discovery(our_external_addr="9.9.9.9:8333",
                                local_ips={"192.168.1.5"})
        assert d._is_own_addr("1.2.3.4:8333") is False

    def test_external_addr_unknown_does_not_false_positive(self):
        """our_external_addr is None until the first PONG confirms it;
        that must not make every address look like a match against ""."""
        d, _ = _make_discovery(our_external_addr=None)
        assert d._is_own_addr("1.2.3.4:8333") is False


class TestAddBootstrapPeerRefusesSelf:
    def test_own_external_address_is_refused_without_pinging(self):
        d, udp = _make_discovery(our_external_addr="9.9.9.9:8333")
        d.add_bootstrap_peer("9.9.9.9:8333")
        udp.ping.assert_not_called()

    def test_own_local_ip_is_refused_without_pinging(self):
        d, udp = _make_discovery(local_ips={"192.168.1.5"})
        d.add_bootstrap_peer("192.168.1.5:8333")
        udp.ping.assert_not_called()

    def test_a_real_peer_is_still_pinged(self):
        d, udp = _make_discovery(our_external_addr="9.9.9.9:8333")
        udp.ping.return_value = None  # unreachable; irrelevant to this test
        d.pool.get_all.return_value = []
        d.add_bootstrap_peer("1.2.3.4:8333")
        udp.ping.assert_called_once_with("1.2.3.4:8333")
