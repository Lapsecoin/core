"""Moving an old single-key node to a recovery-phrase wallet.

At start-up wallet.py creates the phrase wallet (same passphrase, new phrase,
shown once) and the node runs as its account 0 from then on. What is left is
the old key's funds: one ordinary transaction from the old address to the new
one, signed with the old key. No protocol change, and nothing for the chain
to know about.

The sweep is signed once, the signed transaction is stored in the wallet file,
and only then is the old key file deleted. After that the old key is gone and
the stored transaction is all that is needed: it is rebroadcast until the old
address's nonce shows it confirmed, and after a reorg it is simply
broadcast again, because an account-model transaction stays valid at its
nonce.

The sweep waits for the node to be synced before it is signed, so the balance
it moves is the network's and not a stale local one. If the process stops
first, both files are still there and the next start picks it up again."""

import logging
import os
import threading
import time

import params
import tx as tx_mod

log = logging.getLogger("ec.migration")

# The sweep pays this many times the minimum relay fee rate: still a tiny
# amount, but safely above the floor whatever the exact size comes to.
FEE_MARGIN = 10
CHECK_SECONDS = 10
RESEND_SECONDS = 300


class Migration:

    def __init__(self, walletfile, legacy=None, legacy_kek=None, legacy_path=None):
        self.wf = walletfile
        self.legacy = legacy              # the old Wallet, while its key file exists
        self.legacy_kek = legacy_kek
        self.legacy_path = legacy_path
        self.account0 = walletfile.address(0)
        self._last_send = 0.0
        self._warned_wait = 0.0

    @property
    def done(self):
        return self.legacy is None and self.wf.pending_sweep is None

    # One pass
    def step(self, node, now=None):
        """Do whatever is due. Returns a short word for what happened."""
        now = time.monotonic() if now is None else now
        sweep = self.wf.pending_sweep
        if sweep is None:
            if self.legacy is None:
                return "done"
            return self._sign_and_store(node, now)
        return self._follow(node, sweep, now)

    def _synced(self, node):
        info = node.get_info()
        return info["sync_percent"] >= 100 and info["peer_count"] > 0

    def _sign_and_store(self, node, now):
        if not self._synced(node):
            if now - self._warned_wait > 600:
                self._warned_wait = now
                log.info("[migrate] waiting for the node to sync before moving the "
                         "old address's funds to %s", self.account0)
            return "waiting"
        old = self.legacy.addr
        state = node.view.state
        balance = state.get_balance(old)
        nonce = max(state.get_nonce(old), node.mempool.pending_nonce(old)) + 1
        draft = {"from": old, "pubkey": self.legacy.pk_hex,
                 "outputs": [{"to": self.account0, "amount": balance}],
                 "nonce": nonce, "fee": balance}
        fee = tx_mod.tx_size(draft) * params.MIN_RELAY_FEE_RATE * FEE_MARGIN
        if balance <= fee:
            log.info("[migrate] the old address %s holds %s, not enough to move; "
                     "retiring its key", old, balance)
            self._retire_key()
            return "empty"
        signed, _fee = self.legacy.sign_tx(
            [{"to": self.account0, "amount": balance - fee}], nonce, fee,
            kek=self.legacy_kek)
        # The signed move is stored before the key goes, in one atomic write.
        self.wf.set_pending_sweep(signed)
        self._retire_key()
        log.info("[migrate] moving %s from the old address to %s (fee %s)",
                 balance - fee, self.account0, fee)
        return "signed"

    def _follow(self, node, sweep, now):
        old = sweep["from"]
        state = node.view.state
        if state.get_nonce(old) >= sweep["nonce"]:
            self.wf.clear_pending_sweep()
            left = state.get_balance(old)
            if left and self.legacy is not None:
                # A start that was cut short left the old key in place: what has
                # arrived since is moved by the next pass, so nothing is stranded.
                log.info("[migrate] the first move is confirmed; moving the %s that "
                         "arrived at the old address since", left)
            elif left:
                log.warning("[migrate] done, but %s arrived at the old address %s after "
                            "it was moved and cannot be reached any more", left, old)
            else:
                log.info("[migrate] the old address's funds are now at %s", self.account0)
            return "confirmed"
        if now - self._last_send >= RESEND_SECONDS or self._last_send == 0.0:
            self._last_send = now
            ok, why = node.submit_tx_from_api(sweep)
            log.info("[migrate] sent the move to the network%s",
                     "" if ok else f" (not accepted yet: {why})")
        return "sent"

    def _retire_key(self):
        """The old key is deleted. Nothing is left of it but the stored move."""
        self.legacy = None
        self.legacy_kek = None
        if self.legacy_path and os.path.exists(self.legacy_path):
            os.remove(self.legacy_path)

    # The thread
    def run(self, node):
        while node.running and not self.done:
            try:
                self.step(node)
            except Exception:
                log.exception("[migrate] pass failed, will try again")
            time.sleep(CHECK_SECONDS)

    def start(self, node):
        t = threading.Thread(target=self.run, args=(node,), daemon=True, name="migration")
        t.start()
        return t


class Discovery:
    """After a restore from a phrase: list the other accounts the phrase has
    used, found by walking account 0, 1, 2, ... in order and stopping once a
    few in a row have no history (as the wallet convention on the site says;
    a short run past the first gap costs nothing and catches skipped ones)."""

    GAP = 5

    def __init__(self, walletfile, kek):
        self.wf = walletfile
        self.kek = kek

    def step(self, node):
        if not self.wf.scan_pending:
            return "done"
        info = node.get_info()
        if info["sync_percent"] < 100 or info["peer_count"] == 0:
            return "waiting"
        import falcon_seeded
        import hdwallet
        import crypto
        state = node.view.state
        phrase = self.wf.phrase(self.kek)
        try:
            found, index, empty = [], 1, 0
            while empty < self.GAP:
                seed = hdwallet.falcon_seed_for_account(phrase, "", index)
                _sk, pk = falcon_seeded.keypair_from_seed(seed)
                addr = crypto.public_key_to_address(pk)
                if state.get_nonce(addr) or state.get_balance(addr):
                    found.append(index)
                    empty = 0
                else:
                    empty += 1
                index += 1
        finally:
            del phrase
        self.wf.finish_scan(self.kek, found)
        if found:
            log.info("[wallet] found %d more address%s in this phrase's history",
                     len(found), "" if len(found) == 1 else "es")
        return "scanned"

    def run(self, node):
        while node.running and self.wf.scan_pending:
            try:
                self.step(node)
            except Exception:
                log.exception("[wallet] address scan failed, will try again")
            time.sleep(CHECK_SECONDS)

    def start(self, node):
        t = threading.Thread(target=self.run, args=(node,), daemon=True, name="wallet-scan")
        t.start()
        return t
