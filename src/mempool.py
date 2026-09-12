"""Pending tx storage. No I/O."""

import time

import tx as tx_mod

MEMPOOL_TTL_SECONDS = 30 * 60

# Hard cap on total pending tx bytes (fee-basis size, signature excluded,
# same measure block.assemble() prioritizes by). ~10x BLOCK_SIZE_LIMIT: room
# for several blocks' worth of backlog without letting the mempool grow
# unbounded under spam. Once full, a new tx is admitted only by outbidding
# and evicting enough of the lowest fee-per-byte txs currently held to fit,
# same eviction policy as Bitcoin Core's mempool.
MEMPOOL_MAX_BYTES = 100_000_000


class _Entry:
    """One pending transaction plus the two facts about it that never
    change and used to be recomputed constantly."""

    __slots__ = ("tx", "entered", "size", "fee_rate")

    def __init__(self, tx_dict):
        self.tx       = tx_dict
        self.entered  = time.monotonic()
        self.size     = tx_mod.tx_size(tx_dict)
        self.fee_rate = tx_dict.get("fee", 0) / max(self.size, 1)


class Mempool:
    """
    The node loop is the only writer. Flask threads are read-only.
    CPython GIL makes dict reads and single-key writes atomic, so no
    lock is needed. Flask threads must never call add, remove, or remove_many.
    """

    def __init__(self):
        # tx_hash -> _Entry. Size and fee rate are stored alongside the
        # transaction rather than derived on demand: both are a full
        # canonical serialization of it, and both were being recomputed on
        # every eviction sort (twice per candidate), every removal and
        # every prune, for values that cannot change once a transaction
        # exists.
        self._pool: dict = {}
        self._total_bytes = 0

    def add(self, tx_dict) -> tuple:
        h = tx_mod.tx_hash(tx_dict)
        if h in self._pool:
            return False, "duplicate"

        entry = _Entry(tx_dict)
        overflow = self._total_bytes + entry.size - MEMPOOL_MAX_BYTES
        to_evict = []
        if overflow > 0:
            by_worst_first = sorted(self._pool.items(),
                                    key=lambda item: item[1].fee_rate)
            freed = 0
            for h2, other in by_worst_first:
                if freed >= overflow:
                    break
                if other.fee_rate >= entry.fee_rate:
                    break
                to_evict.append(h2)
                freed += other.size
            if freed < overflow:
                return False, "mempool full: fee too low to replace pending txs"

        self.remove_many(to_evict)
        self._pool[h] = entry
        self._total_bytes += entry.size
        return True, h

    def remove(self, tx_hash):
        entry = self._pool.pop(tx_hash, None)
        if entry:
            self._total_bytes -= entry.size

    def remove_many(self, tx_hashes):
        for h in tx_hashes:
            self.remove(h)

    def get(self, tx_hash):
        entry = self._pool.get(tx_hash)
        return entry.tx if entry else None

    def get_txs_by_hashes(self, tx_hashes):
        return [self._pool[h].tx for h in tx_hashes if h in self._pool]

    def size(self):
        return len(self._pool)

    def all_txs(self):
        return [e.tx for e in self._pool.values()]

    def pending_nonce(self, addr):
        """Highest nonce in the mempool for addr, or 0 if none. Lets a
        wallet queue up several sends in a row without waiting for each
        one to confirm first."""
        nonces = [e.tx["nonce"] for e in self._pool.values()
                  if e.tx.get("from") == addr]
        return max(nonces) if nonces else 0

    def probe_state_for(self, addr, state):
        """State snapshot with addr's already-pending mempool txs applied,
        in nonce order. Validating a newly submitted tx against this
        (instead of the raw confirmed state) is what lets a wallet queue a
        second send before the first confirms: both the nonce and the
        balance it's checked against already account for the first one."""
        probe = state.snapshot()
        pending = sorted(
            (e.tx for e in self._pool.values() if e.tx.get("from") == addr),
            key=lambda t: t["nonce"],
        )
        for t in pending:
            probe.apply_tx(t)
        return probe

    def pending_hashes(self):
        return frozenset(self._pool.keys())

    def prune_stale(self, state, ttl_seconds=MEMPOOL_TTL_SECONDS):
        """Evict txs that can never become valid: a nonce already superseded
        on chain, or simply too old. Returns list of pruned hashes."""
        now = time.monotonic()
        pruned = [h for h, e in self._pool.items()
                  if e.tx["nonce"] <= state.get_nonce(e.tx["from"])
                  or now - e.entered > ttl_seconds]
        self.remove_many(pruned)
        return pruned
