"""How discovery turns candidate addresses into peers, and the peer list it
keeps between runs. The network is replaced by mocks; what is checked is which
addresses get tried, in what order, and that nothing is left marked as being
attempted."""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discovery as discovery_mod  # noqa: E402

GOOD = "8.8.8.8:8333"
OTHER = "1.1.1.1:8333"


def make(known=(), external=None, max_peers=50):
    udp = MagicMock()
    udp.our_external_addr = external
    udp._local_ips = frozenset()
    pool = MagicMock()
    pool.all_addrs.return_value = list(known)
    pool.count.return_value = len(known)
    pool._max_peers = max_peers
    pool.get_all.return_value = []
    return discovery_mod.Discovery(udp, pool, genesis_hash="a" * 64, port=8333, node_address="tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"), udp, pool


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(discovery_mod.time, "sleep", lambda s: None)


class TestFlushCandidates:
    def test_an_empty_queue_does_nothing(self):
        d, udp, pool = make()
        d._flush_candidates()
        pool.mark_attempting.assert_not_called()

    def test_new_addresses_are_tried_with_where_they_were_learned_from(self):
        d, udp, pool = make()
        tried = []
        d._try_candidate = lambda addr, learned_from=None: tried.append((addr, learned_from)) or True
        d.enqueue_candidate(GOOD, learned_from=OTHER)
        d._flush_candidates()
        assert tried == [(GOOD, OTHER)]

    def test_the_queue_is_emptied_so_an_address_is_not_tried_twice(self):
        d, udp, pool = make()
        tried = []
        d._try_candidate = lambda addr, learned_from=None: tried.append(addr) or True
        d.enqueue_candidate(GOOD)
        d._flush_candidates()
        d._flush_candidates()
        assert tried == [GOOD]

    def test_peers_we_already_have_and_our_own_address_are_skipped(self):
        d, udp, pool = make(known=[OTHER], external=GOOD)
        d._try_candidate = lambda *a, **k: pytest.fail("must not try")
        d.enqueue_candidate(OTHER)
        d.enqueue_candidate(GOOD)
        d._flush_candidates()

    def test_addresses_that_cannot_be_peers_never_enter_the_queue(self):
        d, *_ = make()
        for bad in ("not an address", "127.0.0.1:8333", "10.0.0.5:8333", None, 5):
            d.enqueue_candidate(bad)
        assert d._candidates == {}


class TestTryCandidate:
    def test_a_full_pool_tries_nothing(self):
        d, udp, pool = make(known=[OTHER], max_peers=1)
        d._ping_and_admit = lambda *a, **k: pytest.fail("pool is full")
        assert d._try_candidate(GOOD) is False
        pool.mark_attempting.assert_not_called()

    def test_a_peer_that_answers_a_ping_is_admitted_without_punching(self):
        d, udp, pool = make()
        d._ping_and_admit = MagicMock(return_value=True)
        assert d._try_candidate(GOOD, OTHER) is True
        d._ping_and_admit.assert_called_once_with(GOOD, OTHER)
        udp.punch_direct.assert_not_called()
        pool.unmark_attempting.assert_called_once_with(GOOD)

    def test_relays_are_tried_in_order_and_the_first_that_works_wins(self):
        d, udp, pool = make()
        pool.get_all.return_value = ["2.2.2.2:1", "3.3.3.3:1", "4.4.4.4:1"]
        d._ping_and_admit = MagicMock(return_value=False)
        d._punch_and_admit = MagicMock(side_effect=[False, True])
        assert d._try_candidate(GOOD) is True
        assert [c.args[0] for c in d._punch_and_admit.call_args_list] == ["2.2.2.2:1", "3.3.3.3:1"]
        udp.punch_direct.assert_not_called()

    def test_with_no_relay_working_it_punches_directly_and_pings_once_more(self):
        d, udp, pool = make()
        d._ping_and_admit = MagicMock(side_effect=[False, True])
        assert d._try_candidate(GOOD) is True
        udp.punch_direct.assert_called_once_with(GOOD)
        assert d._ping_and_admit.call_count == 2

    def test_an_unreachable_address_is_given_up_on(self):
        d, udp, pool = make()
        d._ping_and_admit = MagicMock(return_value=False)
        assert d._try_candidate(GOOD) is False
        pool.unmark_attempting.assert_called_once_with(GOOD)

    def test_the_attempt_mark_is_cleared_even_when_something_raises(self):
        d, udp, pool = make()
        d._ping_and_admit = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            d._try_candidate(GOOD)
        pool.unmark_attempting.assert_called_once_with(GOOD)


class TestPeerCache:
    @pytest.fixture(autouse=True)
    def cache_file(self, monkeypatch, tmp_path):
        self.path = tmp_path / "peers.json"
        monkeypatch.setattr(discovery_mod, "PEER_CACHE_FILE", str(self.path))

    def test_the_pool_is_saved_as_a_json_list(self):
        d, udp, pool = make(known=[GOOD, OTHER])
        d._save_peer_cache()
        assert json.loads(self.path.read_text()) == [GOOD, OTHER]

    def test_remembered_peers_are_queued_on_the_next_start(self):
        self.path.write_text(json.dumps([GOOD, OTHER]))
        d, *_ = make()
        d._load_peer_cache()
        assert set(d._candidates) == {GOOD, OTHER}

    def test_no_cache_file_is_a_normal_first_start(self):
        d, *_ = make()
        d._load_peer_cache()
        assert d._candidates == {}

    def test_a_damaged_cache_file_is_ignored_not_fatal(self):
        self.path.write_text("{ this is not json")
        d, *_ = make()
        d._load_peer_cache()
        assert d._candidates == {}

    def test_a_cache_that_is_the_wrong_shape_is_ignored_not_fatal(self):
        self.path.write_text(json.dumps({"peers": [GOOD]}))
        d, *_ = make()
        d._load_peer_cache()
        assert d._candidates == {}

    def test_a_place_it_cannot_write_is_not_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(discovery_mod, "PEER_CACHE_FILE", str(tmp_path / "no" / "such" / "dir.json"))
        d, *_ = make(known=[GOOD])
        d._save_peer_cache()
