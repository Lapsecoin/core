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

ADDR = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"


class TestStartWithoutLibtorrent:
    def test_start_returns_none_session_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(discovery_dht_mod, "lt", None)
        dht = discovery_dht_mod.DHTDiscovery(
            enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333, node_address=ADDR)

        ses, my_offset = dht.start()

        assert ses is None
        assert my_offset == 0

    def test_start_logs_why_instead_of_failing_silently(self, monkeypatch, caplog):
        monkeypatch.setattr(discovery_dht_mod, "lt", None)
        dht = discovery_dht_mod.DHTDiscovery(
            enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333, node_address=ADDR)

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
                dht_notification=1, status_notification=2,
                dht_operation_notification=4)),
        )
        import unittest.mock as mock
        with mock.patch.object(discovery_dht_mod, "lt", fake_lt):
            dht = discovery_dht_mod.DHTDiscovery(
                enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333, node_address=ADDR)
            with mock.patch("os.path.exists", return_value=False):
                ses, my_offset = dht.start()

        assert ses is fake_session


class TestOwnSlotCheck:
    """Our slot only counts as published when it holds our own address."""

    def _dht(self):
        dht = discovery_dht_mod.DHTDiscovery(
            enqueue_fn=lambda addr: None, genesis_hash="a" * 64, port=8333, node_address=ADDR)
        dht._pk_to_slot = {}
        return dht

    def _alert(self, dht, slot, seq):
        import types
        _, pk = dht._slot_keypair(slot)
        dht._pk_to_slot[pk] = slot
        return types.SimpleNamespace(authoritative=True, key=pk, seq=seq)

    def _put(self, dht, addr, acked_ago):
        import time
        slot = dht.slot_for(addr)
        dht._own_slot, dht._own_addr, dht._own_visible = slot, addr, False
        dht._put_acked_at = time.monotonic() - acked_ago
        dht._round_total = 1
        return slot

    def test_our_own_value_in_our_slot_is_visible(self):
        dht = self._dht()
        slot = self._put(dht, "1.2.3.4:8333", acked_ago=60)
        dht._note_item(self._alert(dht, slot, 3), "1.2.3.4:8333")
        assert dht._own_visible and not dht.slot_taken

    def test_somebody_elses_value_in_our_slot_marks_it_taken(self):
        dht = self._dht()
        slot = self._put(dht, "1.2.3.4:8333", acked_ago=60)
        dht._note_item(self._alert(dht, slot, 9), "9.9.9.9:8333")
        assert dht.slot_taken and not dht._own_visible
        assert dht.needs_reput()

    def test_a_value_read_right_after_the_put_is_not_yet_a_conflict(self):
        dht = self._dht()
        slot = self._put(dht, "1.2.3.4:8333", acked_ago=5)
        dht._note_item(self._alert(dht, slot, 9), "9.9.9.9:8333")
        assert not dht.slot_taken

    def test_moving_gives_a_different_slot_and_stops_after_the_limit(self):
        dht = self._dht()
        first = dht.slot_for("1.2.3.4:8333")
        seen = {first}
        for _ in range(discovery_dht_mod.MAX_SLOT_MOVES):
            dht.slot_taken = True
            assert dht.move_slot_if_taken()
            seen.add(dht.slot_for("1.2.3.4:8333"))
        assert len(seen) > 1
        dht.slot_taken = True
        assert not dht.move_slot_if_taken()
