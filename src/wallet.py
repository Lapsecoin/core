"""A wallet: one keyfile, its public key and address, and the one place a
transaction gets built and signed.

Deliberately knows nothing about chain state. The nonce is passed in by
whoever can see it (a full node reads it from its own state and mempool,
a light client asks a remote node), so the same signing code serves both
and cannot drift between them.
"""

import getpass
import json
import logging
import os
import sys

import crypto
import tx as tx_mod

# Same logger name main.py logs under, so the startup lines read the same
# whichever program created or loaded the key.
log = logging.getLogger("ec.main")


DEFAULT_KEY = "lapsecoin_key.json"
KEY_ENV = "LAPSECOIN_KEY"


def key_path(value=None):
    """The key file to use. `value` is --key, else $LAPSECOIN_KEY, else the
    default file. Either may be a path to the key file or the text that
    `--export` prints; text is saved to the default file (once) and that
    file is used from then on, so everything downstream still gets a path."""
    value = (value or os.environ.get(KEY_ENV) or DEFAULT_KEY).strip()
    # A library we import prints a line of its own on stdout, so captured
    # --export output can carry it in front of the key. Take the key's line.
    lines = [ln.strip() for ln in value.splitlines() if ln.lstrip().startswith("{")]
    if lines:
        value = lines[-1]
    if not value.startswith("{"):
        return value
    try:
        data = json.loads(value)
        for field in ("public_key", "ciphertext", "salt"):
            if not isinstance(data[field], str):
                raise TypeError(field)
    except (ValueError, KeyError, TypeError):
        raise ValueError("the key is neither a file path nor the output of --export")
    if os.path.exists(DEFAULT_KEY):
        if crypto.load_pubkey(DEFAULT_KEY).hex() != data["public_key"]:
            raise ValueError(f"{DEFAULT_KEY} already holds a different key; "
                             "remove it, or point --key at the key you want")
        return DEFAULT_KEY
    fd = os.open(DEFAULT_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    log.info("[startup] key saved  file=%s", DEFAULT_KEY)
    return DEFAULT_KEY


def export_key(path):
    """The key file as one line of text, for --key or $LAPSECOIN_KEY on
    another machine. Still encrypted: it opens only with the passphrase."""
    with open(path) as f:
        return json.dumps(json.load(f), separators=(",", ":"))


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


def resolve_passphrase(prompt):
    env_pass = os.environ.get("LAPSECOIN_PASSPHRASE")
    if env_pass:
        return env_pass
    return getpass.getpass(prompt)


def load_or_create_key(keyfile):
    """Returns (pk, kek, passphrase). The passphrase is handed back for
    symmetry with the GUI path; main discards it immediately, since nothing
    past startup needs it. There is one key file and one address."""
    if not os.path.exists(keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = resolve_passphrase("New passphrase: ")
        if not os.environ.get("LAPSECOIN_PASSPHRASE"):
            passphrase = prompt_new_passphrase(passphrase)
        sk, pk = crypto.generate_keypair()
        crypto.save_key(keyfile, sk, pk, passphrase)
        kek = crypto.derive_kek(keyfile, passphrase)
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key created  file=%s", keyfile)
        log.info("[startup] address=%s", addr)
        del sk
        return pk, kek, passphrase
    passphrase = resolve_passphrase("Passphrase: ")
    try:
        pk = crypto.load_pubkey(keyfile)
        kek = crypto.derive_kek(keyfile, passphrase)
        sk_test = crypto.decrypt_secret_key(keyfile, kek=kek)
        del sk_test
    except ValueError as e:
        sys.exit(f"Error: {e}")
    log.info("[startup] key loaded  file=%s", keyfile)
    return pk, kek, passphrase


def prompt_new_passphrase(first=None):
    while True:
        p1 = first if first else getpass.getpass("New passphrase: ")
        first = None
        if len(p1) < 8:
            print("Passphrase must be at least 8 characters.")
            continue
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases do not match.")

