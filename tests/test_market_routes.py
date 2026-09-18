"""The Trades page's worker-status view.

A stalled trade must not look the same whether the wallet is locked,
Horizon is unreachable, or the counterparty genuinely vanished, so this
checks that market_routes._worker_view tells those apart correctly from
whatever swap_worker.status() reports.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import market_routes


class FakeWorker:
    def __init__(self, **overrides):
        self._status = {
            "running": True, "enabled": True, "unlocked": True,
            "passes": 3, "paused_until": 0.0, "last_error": "",
        }
        self._status.update(overrides)

    def status(self):
        return self._status


class FakeNode:
    def __init__(self, worker=None):
        if worker is not None:
            self.swap_worker = worker


class TestWorkerView:
    def test_no_worker_attribute_reads_as_off(self):
        assert market_routes._worker_view(FakeNode())["state"] == "off"

    def test_disabled_reads_as_off(self):
        node = FakeNode(FakeWorker(enabled=False))
        assert market_routes._worker_view(node)["state"] == "off"

    def test_not_running_is_reported(self):
        node = FakeNode(FakeWorker(running=False))
        view = market_routes._worker_view(node)
        assert view["state"] == "stopped"
        assert "not running" in view["message"]

    def test_locked_wallet_is_reported(self):
        node = FakeNode(FakeWorker(unlocked=False))
        view = market_routes._worker_view(node)
        assert view["state"] == "locked"
        assert "locked" in view["message"]

    def test_paused_after_outage_is_reported(self):
        node = FakeNode(FakeWorker(paused_until=time.time() + 60,
                                   last_error="horizon unreachable: timeout"))
        view = market_routes._worker_view(node)
        assert view["state"] == "paused"
        assert "horizon unreachable" in view["message"]

    def test_expired_pause_reads_as_ok(self):
        node = FakeNode(FakeWorker(paused_until=time.time() - 60))
        assert market_routes._worker_view(node)["state"] == "ok"

    def test_running_normally(self):
        node = FakeNode(FakeWorker())
        view = market_routes._worker_view(node)
        assert view["state"] == "ok"
        assert view["passes"] == 3

    def test_locked_takes_priority_over_a_stale_pause(self):
        """Unlocking is the fix a user can act on; showing 'paused' instead
        would send them to wait on Horizon rather than unlock the node."""
        node = FakeNode(FakeWorker(unlocked=False,
                                   paused_until=time.time() + 60))
        assert market_routes._worker_view(node)["state"] == "locked"
