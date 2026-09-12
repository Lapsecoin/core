"""Unit tests for discovery_dht.py's optional-dependency handling.

libtorrent is a C++ extension with no bundled fallback, and PyPI wheel
gaps for it on Windows are a real, documented, upstream problem (new
Python releases regularly go months without a published wheel there).
discovery_dht.py already imported it inside a try/except for exactly
this reason, but nothing downstream ever checked the result: DHTDiscovery
.start() called straight into lt.default_settings() regardless, so a
missing libtorrent didn't degrade DHT discovery, it crashed the entire
discovery thread (a bare daemon thread in main.py, so the crash is
silent) and took LAN broadcast, the peer cache, and peer-exchange down
with it: every discovery mechanism, not just the DHT-specific one.

These tests hold discovery_dht.lt at None (simulating the import having
failed) and check start() degrades instead of raising. No test file for
this module existed before this bug was found.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discovery_dht as discovery_dht_mod


class TestStartWithoutLibtorrent:
    def test_start_returns_none_session_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(discovery_dht_mod, "lt", None)
        dht = discovery_dht_mod.DHTDiscovery(
            enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333)

        ses, my_slot, my_offset = dht.start()

        assert ses is None
        assert my_slot == 0
        assert my_offset == 0

    def test_start_logs_why_instead_of_failing_silently(self, monkeypatch, caplog):
        monkeypatch.setattr(discovery_dht_mod, "lt", None)
        dht = discovery_dht_mod.DHTDiscovery(
            enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333)

        with caplog.at_level("WARNING", logger="ec.discovery.dht"):
            dht.start()

        assert any("libtorrent not installed" in r.message for r in caplog.records)

    def test_start_still_builds_a_real_session_when_libtorrent_is_present(self):
        """The degradation path must not accidentally become the only
        path: with the real (or a stand-in) lt module present, start()
        still returns a working session, not None."""
        import types
        fake_session = object()
        fake_lt = types.SimpleNamespace(
            default_settings=lambda: {},
            session=lambda settings: fake_session,
            alert=types.SimpleNamespace(category_t=types.SimpleNamespace(
                dht_notification=1, status_notification=2)),
        )
        import unittest.mock as mock
        with mock.patch.object(discovery_dht_mod, "lt", fake_lt):
            dht = discovery_dht_mod.DHTDiscovery(
                enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333)
            with mock.patch("os.path.exists", return_value=False):
                ses, my_slot, my_offset = dht.start()

        assert ses is fake_session
