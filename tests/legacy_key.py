"""Writes an old single-key file, the format the node still reads (to move it
to a recovery phrase) and never writes any more. Only the tests need to make
one, so the writer lives here and not in crypto.py."""

import base64
import json
import os

import nacl.pwhash
import nacl.secret
import nacl.utils

import crypto


def save_key(path, secret_key_bytes, public_key_bytes, passphrase):
    """Encrypt a keypair to `path` the way the old node did. 0o600."""
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    key = nacl.pwhash.argon2id.kdf(crypto._KLEN, passphrase.encode(), salt,
                                   opslimit=crypto._OPS, memlimit=crypto._MEM)
    ciphertext = nacl.secret.SecretBox(key).encrypt(secret_key_bytes)
    data = {
        "public_key": public_key_bytes.hex(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "salt":       base64.b64encode(salt).decode(),
        "ops": crypto._OPS, "mem": crypto._MEM,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)
