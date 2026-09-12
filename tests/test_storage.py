"""
Unit tests for storage.py

Covers: save_block, load_block, load_all_blocks, chain_height,
save_state, load_state, state_exists, save_block_and_state,
replace_chain_and_state, TxIndex, AddrIndex, get_meta, set_meta.

Uses an in-memory SQLite database via tmp_path fixture.
No network. No real VDF.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state as state_mod
import tx as tx_mod
import storage as storage_mod
from storage import Storage
from tests.fixtures import (
    address, genesis, make_block, make_tx, seed_balance,
)
from params import TICKS_PER_LAPSE


# ---------------------------------------------------------------------------
# Fixture: fresh Storage pointing at a temp file
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "test_chain.db")
    s = Storage(path)
    yield s
    s.close()


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. save_block / load_block / load_all_blocks / chain_height
# ---------------------------------------------------------------------------

class TestBlockPersistence:
    def test_save_and_load_genesis(self, store):
        g = genesis()
        store.save_block(g)
        loaded = store.load_block(0)
        assert loaded["hash"] == g["hash"]

    def test_load_nonexistent_block_returns_none(self, store):
        assert store.load_block(999) is None

    def test_chain_height_empty(self, store):
        assert store.chain_height() == -1

    def test_chain_height_after_genesis(self, store):
        store.save_block(genesis())
        assert store.chain_height() == 0

    def test_chain_height_after_two_blocks(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        store.save_block(g)
        store.save_block(b1)
        assert store.chain_height() == 1

    def test_load_all_blocks_empty(self, store):
        assert store.load_all_blocks() == []

    def test_load_all_blocks_ordered_by_height(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        store.save_block(b1)
        store.save_block(g)
        blocks = store.load_all_blocks()
        assert [b["height"] for b in blocks] == [0, 1]

    def test_save_block_is_idempotent(self, store):
        g = genesis()
        store.save_block(g)
        store.save_block(g)  # should not raise
        assert store.chain_height() == 0


# ---------------------------------------------------------------------------
# 2. State persistence: save_state / load_state / state_exists
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_exists_false_initially(self, store):
        assert store.state_exists() is False

    def test_state_exists_true_after_save(self, store):
        s = fresh_state()
        s.credit(address(0), 1000)
        store.save_state(s)
        assert store.state_exists() is True

    def test_load_state_restores_balances(self, store):
        s = fresh_state()
        s.credit(address(0), 5000)
        store.save_state(s)
        balances, nonces, minted = store.load_state()
        assert balances[address(0)] == 5000

    def test_load_state_restores_nonces(self, store):
        s = fresh_state()
        s.set_nonce(address(0), 7)
        store.save_state(s)
        _, nonces, _ = store.load_state()
        assert nonces[address(0)] == 7

    def test_load_state_restores_emission(self, store):
        s = fresh_state()
        s.total_minted = 12345
        store.save_state(s)
        _, _, minted = store.load_state()
        assert minted == 12345

    def test_save_state_replaces_previous(self, store):
        s1 = fresh_state()
        s1.credit(address(0), 1000)
        store.save_state(s1)
        s2 = fresh_state()
        s2.credit(address(0), 9999)
        store.save_state(s2)
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 9999


# ---------------------------------------------------------------------------
# 3. TxIndex and AddrIndex
# ---------------------------------------------------------------------------

class TestTxAndAddrIndex:
    def test_tx_not_indexed_before_block_save(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h = tx_mod.tx_hash(t)
        assert store.get_tx_height(h) is None

    def test_tx_indexed_after_block_save(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h = tx_mod.tx_hash(t)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        assert store.get_tx_height(h) == 1

    def test_addr_index_for_sender(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        rows = store.get_tx_heights_for_addr(address(0))
        assert len(rows) == 1
        assert rows[0][0] == 1  # block height

    def test_addr_index_for_recipient(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        rows = store.get_tx_heights_for_addr(address(1))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 4. replace_chain_and_state (reorg path)
# ---------------------------------------------------------------------------

class TestReplaceChain:
    def test_replace_chain_removes_old_blocks(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        store.save_block(g)
        store.save_block(b1)
        store.save_block(b2)
        # Reorg: replace from height 1 with a new block
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        store.replace_chain_and_state(fork_point=1, blocks=[b1_new], state=fresh_state())
        assert store.chain_height() == 1
        assert store.load_block(2) is None

    def test_replace_chain_adds_new_blocks(self, store):
        g = genesis()
        store.save_block(g)
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        store.replace_chain_and_state(fork_point=1, blocks=[b1_new], state=fresh_state())
        loaded = store.load_block(1)
        assert loaded["builder"] == address(1)


# ---------------------------------------------------------------------------
# 5. save_block_and_state (atomic)
# ---------------------------------------------------------------------------

class TestSaveBlockAndState:
    def test_block_and_state_saved_together(self, store):
        g = genesis()
        store.save_block(g)
        b1 = make_block(1, g["hash"], [])
        s = fresh_state()
        s.credit(address(0), 42_000)
        store.save_block_and_state(b1, s)
        assert store.chain_height() == 1
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 42_000


# ---------------------------------------------------------------------------
# 6. replace_chain_and_state (atomic)
# ---------------------------------------------------------------------------

class TestReplaceChainAndState:
    def test_replace_chain_and_state_atomic(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        s1 = fresh_state()
        s1.credit(address(0), 1000)
        store.save_block(g)
        store.save_block_and_state(b1, s1)

        # Reorg with new block and new state
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        s2 = fresh_state()
        s2.credit(address(0), 9999)
        store.replace_chain_and_state(fork_point=1, blocks=[b1_new], state=s2)

        assert store.chain_height() == 1
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 9999


# ---------------------------------------------------------------------------
# 7. Meta key-value store
# ---------------------------------------------------------------------------

class TestMeta:
    def test_get_meta_missing_returns_default(self, store):
        assert store.get_meta("no_key") is None
        assert store.get_meta("no_key", default="fallback") == "fallback"

    def test_set_and_get_meta(self, store):
        store.set_meta("version", "1.2.3")
        assert store.get_meta("version") == "1.2.3"

    def test_set_meta_overwrites(self, store):
        store.set_meta("key", "old")
        store.set_meta("key", "new")
        assert store.get_meta("key") == "new"


# ---------------------------------------------------------------------------
# 8. Migrating a pre-v2 (uncompressed) database
# ---------------------------------------------------------------------------

def _write_old_format_db(path, blocks):
    """Write a database exactly as the pre-v2 code did: block JSON in a
    plain text column, no dataz, no schema_version."""
    from peewee import SqliteDatabase, Model, IntegerField, TextField
    import json as _json

    olddb = SqliteDatabase(str(path), pragmas={"journal_mode": "wal"})

    class OldBlock(Model):
        height = IntegerField(primary_key=True)
        hash   = TextField()
        data   = TextField()

        class Meta:
            database = olddb
            table_name = "block"

    olddb.connect()
    olddb.create_tables([OldBlock])
    for b in blocks:
        OldBlock.insert(height=b["height"], hash=b["hash"],
                        data=_json.dumps(b)).execute()
    olddb.close()


class TestSchemaMigration:
    def _blocks(self, n=25):
        out = []
        for h in range(n):
            b = make_block(h, f"{h:064x}", [])
            b["height"] = h
            out.append(b)
        return out

    def test_old_blocks_survive_byte_for_byte(self, tmp_path):
        """The migration rewrites how a block is stored, never what it is.
        Anything else would change its hash and with it the chain."""
        path = tmp_path / "old.db"
        blocks = self._blocks()
        _write_old_format_db(path, blocks)

        store = Storage(str(path))
        assert store.load_all_blocks() == blocks
        assert store.load_block(7) == blocks[7]
        store.close()

    def test_migration_records_its_version_and_does_not_repeat(self, tmp_path):
        path = tmp_path / "old.db"
        blocks = self._blocks()
        _write_old_format_db(path, blocks)

        store = Storage(str(path))
        assert int(store.get_meta("schema_version")) == storage_mod.SCHEMA_VERSION
        store.close()

        again = Storage(str(path))
        assert again.load_all_blocks() == blocks
        again.close()

    def test_an_interrupted_migration_is_finished_by_the_next_open(self, tmp_path):
        """A crash halfway leaves rows in both layouts. That database is
        valid, and the next start must complete it rather than trip on it."""
        path = tmp_path / "old.db"
        blocks = self._blocks()
        _write_old_format_db(path, blocks)

        store = Storage(str(path))          # migrates fully
        store.close()

        # put some rows back the way they were, and clear the version, as
        # an interrupted run would have left them
        import json as _json
        import zlib as _zlib
        reopened = Storage(str(path))
        for h in (3, 4, 5):
            storage_mod.Block.update(
                data=_json.dumps(blocks[h]), dataz=None
            ).where(storage_mod.Block.height == h).execute()
        reopened.set_meta("schema_version", 1)
        reopened.close()

        finished = Storage(str(path))
        assert finished.load_all_blocks() == blocks
        assert int(finished.get_meta("schema_version")) == storage_mod.SCHEMA_VERSION
        finished.close()

    def test_a_fresh_database_needs_no_migration(self, tmp_path):
        store = Storage(str(tmp_path / "new.db"))
        blk = make_block(0, "00" * 32, [])
        store.save_block(blk)
        assert store.load_block(0) == blk
        store.close()


class TestIncrementalStateWrites:
    """save_block_and_state writes only the addresses that moved.

    The state table used to be deleted in full and reinserted in full on
    every block commit, so storing one block's worth of change cost the
    size of the whole ledger, every two minutes, forever.
    """

    def test_only_touched_rows_are_written(self, store):
        st = state_mod.State()
        for i in range(50):
            st.credit(f"addr{i}", 100)
        store.save_state(st)
        assert st.dirty_addresses() == frozenset()

        st.credit("addr0", 5)
        blk = make_block(1, genesis()["hash"], [], 0)
        store.save_block_and_state(blk, st)

        balances, _nonces, _tm = store.load_state()
        assert balances["addr0"] == 105
        assert balances["addr7"] == 100      # untouched rows survive
        assert len(balances) == 50

    def test_an_emptied_address_loses_its_row_when_it_has_no_nonce(self, store):
        st = state_mod.State()
        st.credit("alice", 100)
        st.credit("bob", 100)
        store.save_state(st)

        st.debit("alice", 100)
        blk = make_block(1, genesis()["hash"], [], 0)
        store.save_block_and_state(blk, st)

        balances, _nonces, _tm = store.load_state()
        assert "alice" not in balances
        assert balances["bob"] == 100

    def test_an_emptied_address_keeps_its_row_while_it_has_a_nonce(self, store):
        st = state_mod.State()
        st.credit("alice", 100)
        st.set_nonce("alice", 4)
        store.save_state(st)

        st.debit("alice", 100)
        blk = make_block(1, genesis()["hash"], [], 0)
        store.save_block_and_state(blk, st)

        balances, nonces, _tm = store.load_state()
        assert balances["alice"] == 0
        assert nonces["alice"] == 4

    def test_that_kept_row_does_not_come_back_as_a_holder(self, store):
        # The row above is right, and reading it straight back into
        # _balances would not be: a spent-out address would count as a
        # holder again after a restart, and only after a restart.
        st = state_mod.State()
        st.credit("alice", 100)
        st.set_nonce("alice", 4)
        st.credit("bob", 100)
        store.save_state(st)
        st.debit("alice", 100)
        store.save_block_and_state(make_block(1, genesis()["hash"], [], 0), st)

        restored = state_mod.State.from_snapshot(*store.load_state())
        assert restored.get_all_balances() == [100]
        assert restored.get_nonce("alice") == 4

    def test_a_reorg_rewrites_the_whole_table(self, store):
        # replace_chain_and_state takes a state that may have been resumed
        # from a cached snapshot at some other height, so what differs from
        # the table is not something that state knows. It rewrites rather
        # than trusting the dirty set.
        st = state_mod.State()
        st.credit("alice", 100)
        st.credit("bob", 100)
        store.save_state(st)

        replacement = state_mod.State()
        replacement.credit("carol", 7)
        replacement.mark_persisted()        # nothing looks dirty at all
        blk = make_block(1, genesis()["hash"], [], 0)
        store.replace_chain_and_state(1, [blk], replacement)

        balances, _nonces, _tm = store.load_state()
        assert balances == {"carol": 7}

    def test_total_minted_is_written_even_with_nothing_touched(self, store):
        st = state_mod.State()
        st.apply_reward_distribution([("alice", 500)])
        store.save_state(st)
        st.mark_persisted()

        blk = make_block(1, genesis()["hash"], [], 0)
        store.save_block_and_state(blk, st)
        _balances, _nonces, total_minted = store.load_state()
        assert total_minted == 500
