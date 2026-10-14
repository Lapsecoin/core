"""
Unit tests for upnp.py's port mapping.

Regression test for a bug where only TCP was mapped, leaving the actual
P2P transport (UDP, same port number) unreachable via UPnP even when a
router successfully mapped it, TCP only serves the web UI.

miniupnpc is optional and not installed in this environment (see upnp.py's
own docstring for why it's deliberately excluded from requirements.txt),
so it's injected into sys.modules as a fake for these tests.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _install_fake_miniupnpc(discover_result=1):
    fake_module = MagicMock()
    fake_upnp_instance = MagicMock()
    fake_upnp_instance.discover.return_value = discover_result
    fake_upnp_instance.lanaddr = "192.168.1.50"
    fake_module.UPnP.return_value = fake_upnp_instance
    sys.modules["miniupnpc"] = fake_module
    return fake_upnp_instance


def test_map_port_maps_both_udp_and_tcp():
    """The actual bug: UDP is the P2P transport on this port, TCP only the
    web UI. Both must be requested, not TCP alone."""
    fake = _install_fake_miniupnpc()
    try:
        import upnp
        upnp._map_port(8333, "LapseCoin")
        protocols = {call.args[1] for call in fake.addportmapping.call_args_list}
        assert protocols == {"UDP", "TCP"}
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_maps_with_correct_port_and_lan_addr():
    fake = _install_fake_miniupnpc()
    try:
        import upnp
        upnp._map_port(8333, "LapseCoin")
        for call in fake.addportmapping.call_args_list:
            args = call.args
            assert args[0] == 8333
            assert args[2] == "192.168.1.50"
            assert args[3] == 8333
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_one_protocol_failing_does_not_block_the_other():
    fake = _install_fake_miniupnpc()
    try:
        import upnp

        def side_effect(port, proto, *a, **kw):
            if proto == "UDP":
                raise RuntimeError("router rejected UDP mapping")

        fake.addportmapping.side_effect = side_effect
        assert upnp._map_port(8333, "LapseCoin") is None  # must not raise
        protocols = {call.args[1] for call in fake.addportmapping.call_args_list}
        assert protocols == {"UDP", "TCP"}
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_conflict_logs_warning(caplog):
    """A ConflictInMappingEntry-style failure means another device on this
    network already holds this port, a second, broadcast-independent
    signal for the same problem probe_lan_ports is meant to catch. Must
    surface at WARNING, not get buried at debug with ordinary failures."""
    fake = _install_fake_miniupnpc()
    try:
        import upnp
        import logging

        def side_effect(port, proto, *a, **kw):
            raise RuntimeError("718 ConflictInMappingEntry")

        fake.addportmapping.side_effect = side_effect
        with caplog.at_level(logging.WARNING, logger="ec.upnp"):
            upnp._map_port(8333, "LapseCoin")
        assert any("already mapped" in r.message for r in caplog.records)
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_ordinary_failure_stays_at_debug(caplog):
    fake = _install_fake_miniupnpc()
    try:
        import upnp
        import logging

        def side_effect(port, proto, *a, **kw):
            raise RuntimeError("router does not support this action")

        fake.addportmapping.side_effect = side_effect
        with caplog.at_level(logging.WARNING, logger="ec.upnp"):
            upnp._map_port(8333, "LapseCoin")
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_no_router_found_skips_mapping():
    fake = _install_fake_miniupnpc(discover_result=0)
    try:
        import upnp
        upnp._map_port(8333, "LapseCoin")
        fake.addportmapping.assert_not_called()
    finally:
        del sys.modules["miniupnpc"]


def test_map_port_missing_miniupnpc_does_not_raise():
    sys.modules.pop("miniupnpc", None)
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "miniupnpc":
            raise ImportError("no module named miniupnpc")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        import upnp
        upnp._map_port(8333, "LapseCoin")  # must not raise
    finally:
        builtins.__import__ = real_import
