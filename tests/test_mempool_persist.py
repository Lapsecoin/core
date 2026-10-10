"""Mempool persistence, the 24 hour TTL and re-flooding of pending txs.

The pool itself stays I/O free (mempool.py); mempool_store.py writes and reads
the snapshot, and Node wires it to startup, a timer, shutdown and gossip.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mempool as mempool_mod
import mempool_store
import node as node_mod
import state as state_mod
from node import Node
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_tx, seed_balance
from tests.test_node import node_env  # noqa: F401  (pytest fixture)


def tx_from(sender):
    s = state_mod.State()
    seed_balance(s, sender, 100.0)
    return make_tx(sender, 9, TICKS_PER_LAPSE, s)


def fund(node, *senders):
    for i in senders:
        node.cs.state.credit(address(i), 100 * TICKS_PER_LAPSE)


def reopen(node, keyfile):
    return Node(keyfile=keyfile, public_key=node.pk, gossip=node.gossip,
                syncer=node.syncer, pool=node.pool, net_in_q=node.net_in_q,
                db_path=node.storage.path)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

class TestPool:
    def test_ttl_is_24_hours(self):
        assert mempool_mod.MEMPOOL_TTL_SECONDS == 24 * 60 * 60

    def test_a_restored_tx_keeps_its_original_entry_time(self):
        mp = mempool_mod.Mempool()
        mp.add(tx_from(3), entered=1234.5)
        assert [e for _, e in mp.records()] == [1234.5]

    def test_records_are_oldest_first(self):
        mp = mempool_mod.Mempool()
        mp.add(tx_from(3), entered=300.0)
        mp.add(tx_from(4), entered=100.0)
        mp.add(tx_from(5), entered=200.0)
        assert [e for _, e in mp.records()] == [100.0, 200.0, 300.0]

    def test_version_moves_on_every_change_and_only_then(self):
        mp = mempool_mod.Mempool()
        v0 = mp.version
        t = tx_from(3)
        ok, h = mp.add(t)
        assert mp.version > v0
        v1 = mp.version
        mp.add(t)                       # duplicate: nothing changed
        assert mp.version == v1
        mp.remove("nope")               # absent: nothing changed
        assert mp.version == v1
        mp.remove(h)
        assert mp.version > v1

    def test_prune_uses_wall_clock_age(self, monkeypatch):
        mp = mempool_mod.Mempool()
        s = state_mod.State()
        mp.add(tx_from(3))
        now = time.time()
        monkeypatch.setattr(mempool_mod.time, "time", lambda: now + 23 * 3600)
        assert mp.prune_stale(s) == []
        monkeypatch.setattr(mempool_mod.time, "time", lambda: now + 25 * 3600)
        assert len(mp.prune_stale(s)) == 1


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

class TestStore:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "m.json")
        recs = [(tx_from(3), 100.0), (tx_from(4), 50.0)]
        assert mempool_store.save(path, recs) == 2
        back = mempool_store.load(path)
        assert [e for _, e in back] == [50.0, 100.0]          # oldest first
        assert {t["from"] for t, _ in back} == {address(3), address(4)}

    def test_missing_file_is_an_empty_pool(self, tmp_path):
        assert mempool_store.load(str(tmp_path / "none.json")) == []

    @pytest.mark.parametrize("junk", ["not json", "{}", '{"version":99,"txs":[]}',
                                      '{"version":1,"txs":[{"tx":5,"entered":1}]}'])
    def test_a_damaged_file_is_set_aside_not_fatal(self, tmp_path, junk):
        path = tmp_path / "m.json"
        path.write_text(junk)
        assert mempool_store.load(str(path)) == []
        assert not path.exists() and (tmp_path / "m.json.bad").exists()

    def test_save_leaves_no_temp_file_and_replaces_the_old_one(self, tmp_path):
        path = str(tmp_path / "m.json")
        mempool_store.save(path, [(tx_from(3), 1.0)])
        mempool_store.save(path, [])
        assert mempool_store.load(path) == []
        assert os.listdir(tmp_path) == ["m.json"]

    def test_lives_next_to_the_chain_database(self, tmp_path):
        p = mempool_store.path_for(str(tmp_path / "chain.db"))
        assert os.path.dirname(p) == str(tmp_path) and p.endswith("_mempool.json")


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class TestNodePersistence:
    def test_pending_txs_come_back_after_a_restart(self, node_env):
        node, keyfile, *_ = node_env
        fund(node, 3, 4)
        a, b = tx_from(3), tx_from(4)
        node.mempool.add(a, entered=time.time() - 600)
        node.mempool.add(b)
        node._save_mempool()

        again = reopen(node, keyfile)
        fund(again, 3, 4)
        again._load_mempool()
        assert again.mempool.size() == 2
        # The age travelled with it: 10 minutes, not "just now".
        ages = sorted(time.time() - e for _, e in again.mempool.records())
        assert ages[-1] > 590

    def test_a_tx_the_chain_has_moved_past_is_dropped_on_load(self, node_env):
        node, keyfile, *_ = node_env
        fund(node, 3)
        node.mempool.add(tx_from(3))
        node._save_mempool()

        again = reopen(node, keyfile)
        fund(again, 3)
        again.cs.state.set_nonce(address(3), 5)      # that nonce is spent now
        again._load_mempool()
        assert again.mempool.size() == 0

    def test_a_tx_past_the_ttl_is_dropped_on_load(self, node_env):
        node, keyfile, *_ = node_env
        fund(node, 3)
        old = time.time() - mempool_mod.MEMPOOL_TTL_SECONDS - 60
        node.mempool.add(tx_from(3), entered=old)
        node._save_mempool()

        again = reopen(node, keyfile)
        fund(again, 3)
        again._load_mempool()
        assert again.mempool.size() == 0

    def test_a_chain_of_nonces_from_one_sender_restores_in_order(self, node_env):
        node, keyfile, *_ = node_env
        fund(node, 3)
        s = node.cs.state
        first = make_tx(3, 9, TICKS_PER_LAPSE, s)
        node.mempool.add(first)
        probe = node.mempool.probe_state_for(address(3), s)
        second = make_tx(3, 9, TICKS_PER_LAPSE, probe)
        node.mempool.add(second)
        assert second["nonce"] == first["nonce"] + 1
        node._save_mempool()

        again = reopen(node, keyfile)
        fund(again, 3)
        again._load_mempool()
        assert again.mempool.size() == 2

    def test_an_unchanged_pool_is_not_rewritten(self, node_env, monkeypatch):
        node, *_ = node_env
        writes = []
        real = mempool_store.save
        monkeypatch.setattr(mempool_store, "save",
                            lambda *a, **k: writes.append(1) or real(*a, **k))
        node.mempool.add(tx_from(3))
        node._save_mempool()
        node._save_mempool()
        assert len(writes) == 1
        node.mempool.add(tx_from(4))
        node._save_mempool()
        assert len(writes) == 2

    def test_stop_saves(self, node_env):
        node, *_ = node_env
        node.mempool.add(tx_from(3))
        node.stop()
        assert len(mempool_store.load(node._mempool_file)) == 1

    def test_an_unwritable_location_does_not_raise(self, node_env):
        node, *_ = node_env
        node._mempool_file = os.path.join(os.path.dirname(node._mempool_file),
                                          "missing_dir", "m.json")
        node.mempool.add(tx_from(3))
        node._save_mempool()          # logs a warning, node carries on


class TestRebroadcast:
    def setup_pool(self, node, n=3):
        for i in range(n):
            node.mempool.add(tx_from(3 + i))
        node._rebroadcast_last = time.monotonic() - 10 * 3600

    def test_floods_the_pool_once_per_interval(self, node_env):
        node, _, _, gossip, *_ = node_env
        self.setup_pool(node)
        node._rebroadcast_mempool()
        assert gossip.force_fluff.call_count == 3
        node._rebroadcast_mempool()                  # too soon
        assert gossip.force_fluff.call_count == 3

    def test_a_batch_cap_rotates_to_the_ones_not_yet_sent(self, node_env, monkeypatch):
        node, _, _, gossip, *_ = node_env
        monkeypatch.setattr(node_mod, "MEMPOOL_REBROADCAST_BATCH", 2)
        self.setup_pool(node, 3)
        node._rebroadcast_mempool()
        first = {c.args[2] for c in gossip.force_fluff.call_args_list}
        assert len(first) == 2
        gossip.force_fluff.reset_mock()
        node._rebroadcast_last = time.monotonic() - 10 * 3600
        node._rebroadcast_mempool()
        second = {c.args[2] for c in gossip.force_fluff.call_args_list}
        assert len(second) == 2 and len(first | second) == 3   # the third goes first

    def test_nothing_is_sent_with_no_peers(self, node_env):
        node, _, _, gossip, _, pool, _ = node_env
        pool.count.return_value = 0
        self.setup_pool(node)
        node._rebroadcast_mempool()
        gossip.force_fluff.assert_not_called()

    def test_txs_still_awaiting_their_first_echo_are_left_to_the_retry(self, node_env):
        node, _, _, gossip, *_ = node_env
        self.setup_pool(node, 2)
        h = node.mempool.hashes()[0]
        node._unconfirmed_spreads[h] = (node.mempool.get(h), "tx", time.monotonic(), None)
        node._rebroadcast_mempool()
        sent = {c.args[2] for c in gossip.force_fluff.call_args_list}
        assert h not in sent and len(sent) == 1
