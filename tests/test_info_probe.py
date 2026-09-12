"""Unit tests for info_probe: the peers page's height/version feed."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import info_probe
import peerpool as peerpool_mod


class _FakeUDP:
    """Answers get_info from a canned per-address map. Anything absent
    answers None, which is what an unreachable peer looks like."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get_info(self, addr, timeout=None):
        self.asked.append(addr)
        answer = self.answers.get(addr)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _pool_with(*addrs):
    pool = peerpool_mod.PeerPool()
    for addr in addrs:
        pool.add(addr)
    return pool


def _row(pool, addr):
    return next(r for r in pool.snapshot() if r[0] == addr)


class TestProbeRound:
    def test_records_what_each_peer_answers(self):
        pool = _pool_with("5.6.7.8:8333", "9.10.11.12:8333")
        udp = _FakeUDP({
            "5.6.7.8:8333":    {"height": 42, "version": "0.5.1"},
            "9.10.11.12:8333": {"height": 41, "version": "0.5.0"},
        })

        assert info_probe.probe_round(pool, udp) == 2
        assert sorted(udp.asked) == ["5.6.7.8:8333", "9.10.11.12:8333"]

        addr, _last_seen, _active, height, version, _http = _row(
            pool, "5.6.7.8:8333")

    def test_every_peer_is_asked_in_one_round(self):
        """The point of the module: one round covers the whole table, rather
        than a single random peer per block interval, which left a given
        peer's row stale for peer-count block intervals at a time."""
        # Distinct /24s: PeerPool caps how many peers it admits from one
        # subnet, so same-prefix addresses would not all be tracked.
        addrs = [f"5.6.{i}.1:8333" for i in range(1, 9)]
        pool = _pool_with(*addrs)
        udp = _FakeUDP({a: {"height": 7, "version": "v"}
                        for a in addrs})

        info_probe.probe_round(pool, udp)
        assert sorted(udp.asked) == sorted(addrs)

    def test_a_silent_peer_does_not_clear_what_we_already_knew(self):
        """Otherwise one dropped datagram blanks a row that was correct."""
        pool = _pool_with("5.6.7.8:8333")
        answering = _FakeUDP({"5.6.7.8:8333": {"height": 42, "version": "0.5.1"}})
        info_probe.probe_round(pool, answering)

        info_probe.probe_round(pool, _FakeUDP({}))      # no answer this time

        _addr, _ls, _ac, height, version, _http = _row(pool, "5.6.7.8:8333")
        assert height == 42
        assert version == "0.5.1"

    def test_a_peer_that_raises_does_not_stop_the_round(self):
        pool = _pool_with("5.6.7.8:8333", "9.10.11.12:8333")
        udp = _FakeUDP({
            "5.6.7.8:8333":    OSError("network unreachable"),
            "9.10.11.12:8333": {"height": 41, "version": "0.5.0"},
        })

        assert info_probe.probe_round(pool, udp) == 1
        _a, _ls, _ac, height, _v, _http = _row(pool, "9.10.11.12:8333")
        assert height == 41

    def test_a_junk_answer_is_ignored(self):
        pool = _pool_with("5.6.7.8:8333")
        assert info_probe.probe_round(pool, _FakeUDP({"5.6.7.8:8333": "nope"})) == 0
        _a, _ls, _ac, height, _v, _http = _row(pool, "5.6.7.8:8333")
        assert height is None

    def test_no_peers_is_a_no_op(self):
        pool = peerpool_mod.PeerPool()
        assert info_probe.probe_round(pool, _FakeUDP({})) == 0
