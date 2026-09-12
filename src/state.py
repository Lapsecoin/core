"""Balance ledger, nonce tracking, and emission accounting. No disk I/O."""

from params import EMISSION_DECAY_NUMERATOR, EMISSION_DECAY_DENOMINATOR, SUPPLY_CAP


def compute_can_mint(total_minted: int) -> int:
    """Ticks still mintable: SUPPLY_CAP - total_minted, floored at 0.

    Single source of truth for the mintable pool.
    """
    return max(0, SUPPLY_CAP - total_minted)


def compute_reward(total_minted: int) -> int:
    """Single source of truth for block reward. Used by State and NodeView stats.

    Pure integer arithmetic. No floating point, so every node computes
    the exact same reward regardless of platform. See params.py for how
    the decay ratio was derived.
    """
    return (compute_can_mint(total_minted) * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR


class State:
    def __init__(self):
        self._balances    = {}  # addr -> int (ticks), never 0, see debit()
        self._nonces      = {}  # addr -> int (last used nonce, 0 = never transacted)
        self.total_minted = 0   # ticks minted via block rewards since genesis
        # Addresses whose balance or nonce has moved since the last time
        # this state was written to disk. See dirty_addresses().
        self._dirty       = set()

    # ------------------------------------------------------------------
    # Balance and nonce access
    # ------------------------------------------------------------------

    def get_balance(self, addr):
        return self._balances.get(addr, 0)

    def get_nonce(self, addr):
        return self._nonces.get(addr, 0)

    def get_all_balances(self):
        """Every holder's balance (ticks), unsorted. Zero balances are never
        stored, so every entry is a real holder."""
        return list(self._balances.values())

    def credit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")
        self._balances[addr] = self.get_balance(addr) + amount
        self._dirty.add(addr)

    def debit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"debit amount must be positive, got {amount}")
        bal = self.get_balance(addr)
        if bal < amount:
            raise ValueError(f"debit would make balance negative: {bal} - {amount}")
        remaining = bal - amount
        if remaining:
            self._balances[addr] = remaining
        else:
            # Dropped, not stored as 0. get_all_balances' own docstring has
            # always promised that every entry is a real holder, and the
            # wealth histogram, the holder count and the distribution pages
            # all read it that way, but nothing ever removed an address
            # that spent its last tick. Every such address counted as a
            # holder forever and sat in the smallest bucket. get_balance
            # answers 0 for a missing key, so nothing else changes; the
            # nonce is deliberately left alone, since it is what stops a
            # spent-out address replaying its old transactions.
            self._balances.pop(addr, None)
        self._dirty.add(addr)

    def set_nonce(self, addr, nonce):
        self._nonces[addr] = nonce
        self._dirty.add(addr)

    # ------------------------------------------------------------------
    # Change tracking for persistence
    # ------------------------------------------------------------------

    def dirty_addresses(self):
        """Addresses touched since the last mark_persisted().

        What this exists for: the state table used to be deleted in full
        and reinserted in full on every single block commit, so the cost of
        storing one block's worth of change was the size of the whole
        ledger. Measured at 16ms for a thousand addresses, 180ms for ten
        thousand, 1.2s for fifty thousand, growing forever and paid inside
        the commit path every two minutes. A block moves a handful of
        addresses, so this is the set that actually needs writing.

        An address in here may have been removed from _balances entirely
        (see debit), so a writer has to treat "touched" as "look it up
        again", not as "upsert a row that certainly exists".
        """
        return frozenset(self._dirty)

    def mark_persisted(self):
        """Called by storage once the dirty set has been written."""
        self._dirty.clear()

    # ------------------------------------------------------------------
    # Transaction application
    # ------------------------------------------------------------------

    def apply_tx(self, tx_dict):
        """Apply a validated transaction. Debits sender (outputs + fee),
        credits recipients, advances nonce. The fee is not credited to
        anyone here: it is collected by the block builder as part of
        the block reward distribution (see chainstate._apply_builder_reward)."""
        sender    = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        fee       = tx_dict["fee"]

        self.debit(sender, total_out + fee)
        for out in tx_dict["outputs"]:
            self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def compute_can_mint(self) -> int:
        """Ticks still available to mint."""
        return compute_can_mint(self.total_minted)

    def compute_block_reward(self) -> int:
        """Compute the reward for the next accepted block."""
        return compute_reward(self.total_minted)

    def apply_reward_distribution(self, distribution):
        """Credit a pre-computed reward distribution.

        distribution: list of (address, amount) pairs.
        Each amount is credited independently. total_minted is incremented
        by the sum actually distributed (may be slightly less than the full
        reward due to integer rounding; remainder stays in can_mint).
        """
        for addr, amount in distribution:
            if amount >= 1:
                self.credit(addr, amount)
                self.total_minted += amount

    # ------------------------------------------------------------------
    # Construction from persisted data
    # ------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, balances: dict, nonces: dict,
                      total_minted: int) -> "State":
        """Restore a State from persisted data. Replaces direct field assignment."""
        s = cls()
        s._balances    = balances
        s._nonces      = nonces
        s.total_minted = total_minted
        return s

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self):
        """Return a copy for use as a rollback probe. Safe because keys are
        interned strings and values are ints, both immutable.

        The dirty set copies across too: a probe that goes on to become the
        committed state (validate_and_apply hands its probe straight to
        _apply_block_with_state) has to carry everything still unwritten
        from before it was taken, or those rows would never reach disk.
        """
        s = State()
        s._balances    = self._balances.copy()
        s._nonces      = self._nonces.copy()
        s.total_minted = self.total_minted
        s._dirty       = set(self._dirty)
        return s

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def all_balances(self):
        return dict(self._balances)

    def all_nonces(self):
        return dict(self._nonces)
