"""lapsecoin_wallet.json: the recovery phrase, sealed under the passphrase,
and the list of accounts derived from it.

One file, like any multi-account wallet. The phrase is sealed with the same
Argon2id + NaCl secretbox scheme the key file always used, so the node keeps
a key-encryption key (never the passphrase) in memory and opens the phrase
per operation. The account list (index, public key, address, label) is
public and is only there so a restart does not have to rediscover it; it is
checked against the phrase every time the wallet is unlocked.

Accounts are derived on demand (hdwallet.py, falcon_seeded.py). Nothing
derived is ever written to disk except by export_account_key, which writes
only that account's sealed seed as a short lc1- text."""

import base64
import json
import os
import tempfile

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils

import crypto
import falcon_seeded
import hdwallet
import shortkey

WALLET_FILE = "lapsecoin_wallet.json"
VERSION = 1


class WalletFileError(ValueError):
    pass


def _atomic_write(path, data):
    """Write the JSON beside the file, flush it to disk, then rename over the
    original, so a crash leaves the old file or the new one, never half."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".wallet-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _account_entry(index, seed):
    sk, pk = falcon_seeded.keypair_from_seed(seed)
    del sk
    return {"index": index, "public_key": pk.hex(),
            "address": crypto.public_key_to_address(pk), "label": ""}


class WalletFile:

    def __init__(self, path, data):
        self.path = path
        self._data = data

    # -- creating and loading -------------------------------------------

    @classmethod
    def create(cls, path, phrase, passphrase, phrase_shown=True, scan_pending=False):
        """Seal `phrase` under `passphrase` and write the wallet with account
        0. Refuses to overwrite an existing file. `scan_pending` marks a
        restore: the node looks for the phrase's other accounts in the chain
        history once it is running."""
        if os.path.exists(path):
            raise FileExistsError(path)
        if not passphrase:
            raise ValueError("passphrase is mandatory")
        phrase = hdwallet.validate_phrase(phrase)
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        kek = nacl.pwhash.argon2id.kdf(
            crypto._KLEN, passphrase.encode(), salt,
            opslimit=crypto._OPS, memlimit=crypto._MEM)
        sealed = nacl.secret.SecretBox(kek).encrypt(phrase.encode())
        data = {
            "version": VERSION, "kind": "wallet",
            "salt": base64.b64encode(salt).decode(),
            "ops": crypto._OPS, "mem": crypto._MEM,
            "ciphertext": base64.b64encode(sealed).decode(),
            "accounts": [_account_entry(0, hdwallet.falcon_seed_for_account(phrase, "", 0))],
            "phrase_shown": bool(phrase_shown),
            "scan_pending": bool(scan_pending),
        }
        _atomic_write(path, data)
        return cls(path, data)

    @classmethod
    def load(cls, path):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise WalletFileError(f"cannot read the wallet file: {e}")
        if not isinstance(data, dict) or data.get("kind") != "wallet":
            raise WalletFileError("this is not a LapseCoin wallet file")
        if data.get("version") != VERSION:
            raise WalletFileError("this wallet file is from a newer LapseCoin; update the node")
        for field in ("salt", "ciphertext", "accounts"):
            if field not in data:
                raise WalletFileError(f"the wallet file is missing '{field}'")
        return cls(path, data)

    @staticmethod
    def is_wallet_file(path):
        try:
            with open(path) as f:
                return json.load(f).get("kind") == "wallet"
        except (OSError, ValueError, AttributeError):
            return False

    # -- the public side (no passphrase needed) --------------------------

    @property
    def accounts(self):
        return [dict(a) for a in self._data["accounts"]]

    @property
    def phrase_shown(self):
        return bool(self._data.get("phrase_shown", True))

    @property
    def scan_pending(self):
        return bool(self._data.get("scan_pending", False))

    def account(self, index):
        for a in self._data["accounts"]:
            if a["index"] == index:
                return dict(a)
        raise KeyError(index)

    def address(self, index=0):
        return self.account(index)["address"]

    def public_key(self, index=0):
        return bytes.fromhex(self.account(index)["public_key"])

    # -- unlocking -------------------------------------------------------

    def derive_kek(self, passphrase):
        if not passphrase:
            raise ValueError("passphrase is mandatory")
        return nacl.pwhash.argon2id.kdf(
            crypto._KLEN, passphrase.encode(),
            base64.b64decode(self._data["salt"]),
            opslimit=self._data.get("ops", crypto._OPS),
            memlimit=self._data.get("mem", crypto._MEM))

    def phrase(self, kek):
        """The recovery phrase. ValueError for a wrong passphrase. The caller
        drops it as soon as it has used it."""
        try:
            box = nacl.secret.SecretBox(kek)
            return bytes(box.decrypt(base64.b64decode(self._data["ciphertext"]))).decode()
        except nacl.exceptions.CryptoError:
            raise ValueError("wrong passphrase or corrupted wallet file")

    def verify(self, kek):
        """Open the phrase and check every listed account against it, so a
        file whose account list was edited is refused instead of trusted."""
        phrase = self.phrase(kek)
        try:
            for a in self._data["accounts"]:
                want = _account_entry(a["index"], hdwallet.falcon_seed_for_account(
                    phrase, "", a["index"]))
                if want["public_key"] != a["public_key"] or want["address"] != a["address"]:
                    raise WalletFileError(
                        f"account {a['index']} in the wallet file does not match the "
                        "recovery phrase; the file has been altered")
        finally:
            del phrase

    # -- using accounts --------------------------------------------------

    def secret_key(self, kek, index=0):
        """The FALCON secret key for an account, derived now. The caller
        signs with it and drops it."""
        self.account(index)          # must be a listed account
        phrase = self.phrase(kek)
        try:
            seed = hdwallet.falcon_seed_for_account(phrase, "", index)
        finally:
            del phrase
        sk, _pk = falcon_seeded.keypair_from_seed(seed)
        del seed
        return sk

    def new_account(self, kek):
        """Derive the next unused account, list it and save. Returns it."""
        index = max(a["index"] for a in self._data["accounts"]) + 1
        phrase = self.phrase(kek)
        try:
            entry = _account_entry(index, hdwallet.falcon_seed_for_account(phrase, "", index))
        finally:
            del phrase
        self._data["accounts"].append(entry)
        self._save()
        return dict(entry)

    def finish_scan(self, kek, indexes):
        """List the accounts a history scan found and clear the restore flag."""
        self._data["scan_pending"] = False
        self.add_discovered(kek, indexes)

    def add_discovered(self, kek, indexes):
        """List accounts found by scanning history after a restore."""
        have = {a["index"] for a in self._data["accounts"]}
        phrase = self.phrase(kek)
        try:
            for index in sorted(set(indexes) - have):
                self._data["accounts"].append(_account_entry(
                    index, hdwallet.falcon_seed_for_account(phrase, "", index)))
        finally:
            del phrase
        self._data["accounts"].sort(key=lambda a: a["index"])
        self._save()

    def set_label(self, index, label):
        for a in self._data["accounts"]:
            if a["index"] == index:
                a["label"] = str(label)[:60]
                self._save()
                return
        raise KeyError(index)

    def mark_phrase_shown(self):
        self._data["phrase_shown"] = True
        self._save()

    def export_account_key(self, kek, index, new_passphrase, wallet_passphrase=None):
        """The short lc1- text for one account, sealed under a new passphrase.
        It holds that account's seed only, never the phrase."""
        if not new_passphrase:
            raise ValueError("a new passphrase is required")
        if wallet_passphrase is not None and new_passphrase == wallet_passphrase:
            raise ValueError("choose a different passphrase for the exported key")
        self.account(index)
        phrase = self.phrase(kek)
        try:
            seed = hdwallet.falcon_seed_for_account(phrase, "", index)
        finally:
            del phrase
        try:
            return shortkey.encode(seed, new_passphrase)
        finally:
            del seed

    # -- saving ----------------------------------------------------------

    def _save(self):
        _atomic_write(self.path, self._data)
