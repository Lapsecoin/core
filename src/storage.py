"""
Chain and state persistence via SQLite + peewee ORM.

Schema:
  Block      full block JSON, indexed by height
  State      per-address balance and nonce
  Emission   total_minted singleton
  TxIndex    tx_hash -> block_height lookup
  AddrIndex  addr -> (tx_hash, block_height) lookup

Blocks are the source of truth. State is rebuilt from blocks on open
if the stored state is missing or the chain was extended offline.
The node calls storage after every block is validated and applied.
"""

import json
import logging
import zlib

import tx as tx_mod
from peewee import (
    SqliteDatabase, Model,
    IntegerField, TextField, BlobField, CompositeKey,
)

log = logging.getLogger("ec.storage")

db = SqliteDatabase(None, pragmas={"journal_mode": "wal", "synchronous": "normal"},
                    check_same_thread=False)


class _Base(Model):
    class Meta:
        database = db


# Bumped when the on-disk layout changes in a way that needs a one-shot
# migration. Stored in Meta, so a database carries its own version and the
# migration runs once rather than being re-decided on every read.
SCHEMA_VERSION = 2

# Blocks are written once and read on every start, so this trades compress
# time for read time in the direction that suits: level 6 over level 1 buys
# a couple of points of ratio for a few milliseconds a block, paid once.
BLOCK_COMPRESS_LEVEL = 6


class Block(_Base):
    height = IntegerField(primary_key=True)
    hash   = TextField()
    # `data` is the pre-v2 uncompressed JSON, kept only so an old database
    # is still readable while _migrate_blocks works through it. Emptied as
    # each row moves across; nothing reads it after that.
    data   = TextField()
    dataz  = BlobField(null=True)   # zlib of the same JSON


class State(_Base):
    addr    = TextField(primary_key=True)
    balance = IntegerField(default=0)
    nonce   = IntegerField(default=0)


class Emission(_Base):
    key   = TextField(primary_key=True)
    value = IntegerField(default=0)


class Meta(_Base):
    key   = TextField(primary_key=True)
    value = TextField()


class TxIndex(_Base):
    tx_hash      = TextField(primary_key=True)
    block_height = IntegerField()


class AddrIndex(_Base):
    addr         = TextField()
    tx_hash      = TextField()
    block_height = IntegerField()

    class Meta:
        primary_key = CompositeKey("addr", "tx_hash")
        indexes = ((("addr",), False),)


_TABLES = [Block, State, Emission, Meta, TxIndex, AddrIndex]


def _pack_block(blk):
    """The bytes a block is stored as. Compression is applied strictly
    after the block is already what it is, so nothing that gets hashed
    ever passes through here: block_hash and every tx_hash are computed
    over the dict, and this only decides how that dict is written down."""
    return zlib.compress(json.dumps(blk).encode(), BLOCK_COMPRESS_LEVEL)


def _unpack_block(row):
    return json.loads(zlib.decompress(row.dataz))


class Storage:
    def __init__(self, path):
        self.path = path
        db.init(path)
        db.connect(reuse_if_open=True)
        db.create_tables(_TABLES, safe=True)
        self._migrate()

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    def _migrate(self):
        """Bring an older database up to SCHEMA_VERSION, once.

        A one-shot migration rather than reading both layouts forever:
        load_all_blocks touches every block on every start, so a
        per-row format check would run for the life of the node to serve
        a case that exists for one of them, and a branch that only fires
        on machines upgrading from an old release is a branch nobody
        exercises. Keeping the old layout's knowledge inside here means
        it can be deleted outright once nothing upgrades from that far
        back.

        Nothing here touches what gets hashed, so it cannot change the
        chain: it rewrites how each block is written down, not what the
        block is.
        """
        version = int(self.get_meta("schema_version", 0) or 0)
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            self._migrate_blocks_to_compressed()
        self.set_meta("schema_version", SCHEMA_VERSION)

    def _migrate_blocks_to_compressed(self):
        """Move pre-v2 rows from the plain `data` column into `dataz`.

        Safe to interrupt and re-run: rows are selected by still having
        uncompressed data, so a crash halfway leaves a database that is
        valid either way and that the next start simply finishes. The
        version is only recorded once every row is across.
        """
        cols = {r[1] for r in db.execute_sql("PRAGMA table_info(block)").fetchall()}
        if "dataz" not in cols:
            # create_tables(safe=True) adds tables, never columns to one
            # that already exists, so an old database needs this by hand.
            db.execute_sql("ALTER TABLE block ADD COLUMN dataz BLOB")

        pending = Block.select().where((Block.dataz.is_null()) & (Block.data != ""))
        moved = 0
        with db.atomic():
            for row in pending:
                Block.update(dataz=zlib.compress(row.data.encode(), BLOCK_COMPRESS_LEVEL),
                             data="").where(Block.height == row.height).execute()
                moved += 1
        if not moved:
            return
        log.info("[storage] compressed %d stored blocks", moved)
        try:
            # Only this reclaims the freed pages; SQLite keeps them
            # otherwise. Needs scratch space up to the size of the
            # database, so on a full disk it fails, and a database that is
            # merely uncompacted is a fine place to stop.
            db.execute_sql("VACUUM")
        except Exception:
            log.warning("[storage] could not compact after migrating "
                        "(the database is correct either way)", exc_info=True)

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    def _index_block(self, blk):
        """Insert TxIndex and AddrIndex rows for all transactions in blk."""
        height = blk["height"]
        txs = blk.get("transactions", [])
        tx_rows, addr_rows = [], []
        for t in txs:
            h = tx_mod.tx_hash(t)
            tx_rows.append({"tx_hash": h, "block_height": height})
            addrs = {t["from"]} | {o["to"] for o in t.get("outputs", [])}
            for addr in addrs:
                addr_rows.append({"addr": addr, "tx_hash": h, "block_height": height})
        if tx_rows:
            TxIndex.insert_many(tx_rows).on_conflict_ignore().execute()
        if addr_rows:
            AddrIndex.insert_many(addr_rows).on_conflict_ignore().execute()

    def save_block(self, blk):
        with db.atomic():
            Block.insert(height=blk["height"], hash=blk["hash"],
                         data="", dataz=_pack_block(blk)).on_conflict_replace().execute()
            self._index_block(blk)

    def get_tx_height(self, tx_hash):
        row = TxIndex.get_or_none(TxIndex.tx_hash == tx_hash)
        return row.block_height if row else None

    def get_tx_heights_for_addr(self, addr):
        rows = (AddrIndex
                .select(AddrIndex.block_height, AddrIndex.tx_hash)
                .where(AddrIndex.addr == addr)
                .order_by(AddrIndex.block_height.desc()))
        return [(r.block_height, r.tx_hash) for r in rows]

    def load_block(self, height):
        row = Block.get_or_none(Block.height == height)
        return _unpack_block(row) if row else None

    def load_all_blocks(self):
        """Load and return the full chain as a list. The entire chain is kept
        in memory by design; this is called once at startup."""
        return [_unpack_block(r) for r in Block.select().order_by(Block.height)]

    def chain_height(self):
        row = Block.select(Block.height).order_by(Block.height.desc()).first()
        return row.height if row else -1

    # ------------------------------------------------------------------
    # State snapshots
    # ------------------------------------------------------------------

    def _rewrite_state_inner(self, state):
        """Replace every state row. Must be called inside a db.atomic().

        The unconditional form, for the paths where the dirty set cannot be
        trusted to describe the difference from what is on disk: a reorg
        swaps in a state that may have been resumed from a cached snapshot
        taken at some other height, so what changed relative to the table
        is not something that state knows. Reorgs are rare, a full rewrite
        is always correct, and doing it here is what lets the common path
        below be incremental without having to reason about provenance.
        """
        balances = state.all_balances()
        nonces   = state.all_nonces()
        rows = [
            {"addr": addr, "balance": balances.get(addr, 0), "nonce": nonces.get(addr, 0)}
            for addr in balances.keys() | nonces.keys()
        ]
        State.delete().execute()
        if rows:
            State.insert_many(rows).execute()
        Emission.insert(key="total_minted", value=state.total_minted).on_conflict_replace().execute()
        state.mark_persisted()

    def _save_state_delta_inner(self, state):
        """Write only the addresses that moved. Must be called inside a
        db.atomic().

        The per-block path. See state.dirty_addresses for why: the cost of
        committing one block used to be the size of the entire ledger,
        every two minutes, forever.

        An address is looked up rather than assumed present, because a
        balance that reached zero is removed from the ledger entirely (see
        State.debit) while its nonce stays. Only an address with neither
        loses its row.
        """
        balances = state.all_balances()
        nonces   = state.all_nonces()
        touched  = state.dirty_addresses()
        if not touched:
            Emission.insert(key="total_minted",
                            value=state.total_minted).on_conflict_replace().execute()
            return

        rows, gone = [], []
        for addr in touched:
            if addr in balances or addr in nonces:
                rows.append({"addr": addr,
                             "balance": balances.get(addr, 0),
                             "nonce": nonces.get(addr, 0)})
            else:
                gone.append(addr)
        if gone:
            State.delete().where(State.addr.in_(gone)).execute()
        if rows:
            State.insert_many(rows).on_conflict_replace().execute()
        Emission.insert(key="total_minted", value=state.total_minted).on_conflict_replace().execute()
        state.mark_persisted()

    def save_state(self, state):
        with db.atomic():
            self._rewrite_state_inner(state)

    def load_state(self):
        rows     = State.select()
        balances = {r.addr: r.balance for r in rows}
        nonces   = {r.addr: r.nonce   for r in rows}
        em       = {r.key: r.value for r in Emission.select()}
        return balances, nonces, em.get("total_minted", 0)

    def state_exists(self):
        return State.select().exists()

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def save_block_and_state(self, blk, state):
        """Save block and state in one atomic transaction.
        Calls inner logic directly to avoid nested db.atomic() savepoints.
        Exceptions from peewee propagate to the caller (node._commit),
        which is wrapped in the cycle's unhandled-error handler.
        """
        log.debug("[storage] save block+state  height=%d  hash=%s",
                  blk["height"], blk.get("hash", "?")[:12])
        with db.atomic():
            Block.insert(height=blk["height"], hash=blk["hash"],
                         data="", dataz=_pack_block(blk)).on_conflict_replace().execute()
            self._index_block(blk)
            self._save_state_delta_inner(state)

    def replace_chain_and_state(self, fork_point, blocks, state):
        """Replace chain tail and state in one atomic transaction."""
        log.debug("[storage] replace chain  fork_point=%d  new_blocks=%d",
                  fork_point, len(blocks))
        with db.atomic():
            Block.delete().where(Block.height >= fork_point).execute()
            TxIndex.delete().where(TxIndex.block_height >= fork_point).execute()
            AddrIndex.delete().where(AddrIndex.block_height >= fork_point).execute()
            Block.insert_many([
                {"height": b["height"], "hash": b["hash"],
                 "data": "", "dataz": _pack_block(b)}
                for b in blocks
            ]).on_conflict_replace().execute()
            for blk in blocks:
                self._index_block(blk)
            # Full rewrite, not the delta: see _rewrite_state_inner.
            self._rewrite_state_inner(state)

    def get_meta(self, key, default=None):
        row = Meta.get_or_none(Meta.key == key)
        return row.value if row else default

    def set_meta(self, key, value):
        Meta.insert(key=key, value=str(value)).on_conflict_replace().execute()

    def close(self):
        db.close()
