"""A wallet: one keyfile, its public key and address, and the one place a
transaction gets built and signed.

Deliberately knows nothing about chain state. The nonce is passed in by
whoever can see it (a full node reads it from its own state and mempool,
a light client asks a remote node), so the same signing code serves both
and cannot drift between them.
"""

import crypto
import tx as tx_mod


class Wallet:

    def __init__(self, keyfile, public_key):
        self.keyfile = keyfile
        self.pk      = public_key
        self.pk_hex  = public_key.hex()
        self.addr    = crypto.public_key_to_address(public_key)

    def sign_tx(self, outputs, nonce, fee, memo="", passphrase=None, kek=None):
        """Build and sign a transaction from this wallet. Supply exactly
        one of passphrase or kek (see crypto.decrypt_secret_key). Returns
        (tx_dict, fee)."""
        sk = crypto.decrypt_secret_key(self.keyfile, passphrase=passphrase, kek=kek)
        try:
            t = tx_mod.create(self.addr, self.pk_hex, outputs, nonce, fee, sk, memo=memo)
        finally:
            del sk
        return t, fee
