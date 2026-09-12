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


class _Overlay:
    """A writable view over a State that copies nothing.

    Reads fall through to the underlying state unless this view has
    changed that address; writes land here and never touch it. That makes
    creating one free and reading one about twice the cost of reading a
    dict, which is the right trade for something created once per inbound
    transaction, written to three times and discarded.

    Implements the surface tx.validate and apply_tx actually use
    (get_balance, get_nonce, credit, debit, set_nonce, apply_tx) and
    nothing else, on purpose: anything that needs a real State, above all
    anything that could become a committed one, should not be handed one
    of these by accident.

    Zero is stored rather than deleted here, unlike State.debit, and means
    the same thing: a spent-out address. The view has to record that the
    balance is now zero, since forgetting it would read the underlying
    state's older, larger balance straight back.
    """

    __slots__ = ("_base", "_balances", "_nonces")

    def __init__(self, base):
        self._base     = base
        self._balances = {}
        self._nonces   = {}

    def get_balance(self, addr):
        v = self._balances.get(addr)
        return self._base.get_balance(addr) if v is None else v

    def get_nonce(self, addr):
        v = self._nonces.get(addr)
        return self._base.get_nonce(addr) if v is None else v

    def credit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")
        self._balances[addr] = self.get_balance(addr) + amount

    def debit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"debit amount must be positive, got {amount}")
        bal = self.get_balance(addr)
        if bal < amount:
            raise ValueError(f"debit would make balance negative: {bal} - {amount}")
        self._balances[addr] = bal - amount

    def set_nonce(self, addr, nonce):
        self._nonces[addr] = nonce

    def apply_tx(self, tx_dict):
        """Same rule as State.apply_tx, driven through this view."""
        sender    = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        self.debit(sender, total_out + tx_dict["fee"])
        for out in tx_dict["outputs"]:
            self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])


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
        """A read-through view of `state` with addr's already-pending
        mempool txs applied, in nonce order. Validating a newly submitted
        tx against this (instead of the raw confirmed state) is what lets a
        wallet queue a second send before the first confirms: both the
        nonce and the balance it's checked against already account for the
        first one.

        An overlay rather than state.snapshot(). This runs once per inbound
        transaction and the result is read a handful of times and thrown
        away, so copying the entire ledger for it was the most expensive
        thing about accepting a transaction and it scaled with the number
        of holders, not with anything about the transaction. Measured at
        1.8ms per probe against a hundred thousand holders, against a rate
        limiter that permits thousands of transactions a second, which made
        it the cheapest way to make a node stop keeping up.

        Deliberately not the committed state's own representation. This
        view is never promoted to a committed state (nothing here returns
        it to ChainState), so it can be a cheap read-through without the
        question of how overlays get flattened ever arising.
        """
        probe = _Overlay(state)
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
