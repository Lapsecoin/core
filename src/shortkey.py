"""The short text form of one account's key: "lc1-" and 114 characters.

It carries the account's 48-byte FALCON seed, sealed under a passphrase, and
nothing else: the node derives the FALCON key (and so the public key and
address) from the seed when it unlocks. The seed comes out of BIP-85, which
is one way, so the text reveals nothing about the recovery phrase or the
other accounts.

Layout, 85 bytes in base64url without padding:
    version (1)  salt (16)  sealed seed (64)  checksum (4)
The Argon2id cost is implied by the version, and the nonce is fixed at zero:
every text has its own random salt, so each encryption key seals exactly one
message and a fixed nonce cannot repeat under one key. The checksum catches a
mistyped or cut-off paste before the passphrase is even asked for."""

import base64
import hashlib

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils

PREFIX = "lc1-"
VERSION = 1
# Version 1's Argon2id cost, pinned here (not read from the library) so a
# library default changing can never make existing texts unreadable.
_OPS = 3
_MEM = 268435456
_SALT = 16
_NONCE = bytes(nacl.secret.SecretBox.NONCE_SIZE)
SEED_BYTES = 48
_SEALED = SEED_BYTES + nacl.secret.SecretBox.MACBYTES
_BODY = 1 + _SALT + _SEALED


def _checksum(body):
    return hashlib.sha256(b"lapsecoin-key-v1" + body).digest()[:4]


def encode(seed, passphrase):
    """The text for an account seed, sealed under a new passphrase."""
    if len(seed) != SEED_BYTES:
        raise ValueError(f"the seed must be {SEED_BYTES} bytes")
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    salt = nacl.utils.random(_SALT)
    box = nacl.secret.SecretBox(_kdf(passphrase, salt))
    sealed = box.encrypt(bytes(seed), _NONCE).ciphertext
    body = bytes([VERSION]) + salt + sealed
    raw = body + _checksum(body)
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def looks_like(text):
    return text.strip().startswith(PREFIX)


def decode(text):
    """(salt, sealed seed) from the text. ValueError if it is cut off,
    mistyped or from a newer version."""
    text = "".join(text.split())
    if not text.startswith(PREFIX):
        raise ValueError("this is not a LapseCoin key")
    b64 = text[len(PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except ValueError:
        raise ValueError("the key text is incomplete or mistyped")
    if len(raw) != _BODY + 4 or raw[-4:] != _checksum(raw[:-4]):
        raise ValueError("the key text is incomplete or mistyped")
    if raw[0] != VERSION:
        raise ValueError("this key is from a newer LapseCoin; update the node")
    return raw[1:1 + _SALT], raw[1 + _SALT:_BODY]


def _kdf(passphrase, salt):
    return nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE, passphrase.encode(), salt,
        opslimit=_OPS, memlimit=_MEM)


def derive_kek(parts, passphrase):
    """The key-encryption key for a decoded text, from its own salt."""
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    return _kdf(passphrase, parts[0])


def open_seed(parts, kek):
    """The 48-byte seed. ValueError for a wrong passphrase."""
    try:
        return bytes(nacl.secret.SecretBox(kek).decrypt(parts[1], _NONCE))
    except nacl.exceptions.CryptoError:
        raise ValueError("wrong passphrase")
