"""
Keys, signing, verification, address derivation, key storage.

Signing: FALCON-512 (NIST PQC standard) via liboqs-python (Open Quantum Safe).
Key storage: Argon2id + NaCl secretbox. Passphrase mandatory.
             Secret key decrypted per signing call, reference dropped immediately.
"""

import re
import base64
import hashlib
import json
import os
import threading

import nacl.pwhash
import nacl.secret
import nacl.utils
import orjson
import oqs

from params import ADDRESS_WORD_COUNT, WORD_BITS

def _load_wordlist():
    """Load BIP39 wordlist from disk. Called once at module import."""
    import resources
    base = (resources.bundle_dir() if resources.is_frozen()
            else os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "bip39_english.txt")
    with open(path) as f:
        words = [w.strip() for w in f if w.strip()]
    assert len(words) == 2048, f"BIP39 wordlist must have 2048 words, got {len(words)}"
    return words

# Immutable module-level constant. No global mutation after import.
_WORDLIST: list[str] = _load_wordlist()
_WORDLIST_SET: frozenset[str] = frozenset(_WORDLIST)


# ---------------------------------------------------------------------------
# Key generation and signing (FALCON-512)
# ---------------------------------------------------------------------------

def generate_keypair():
    """Returns (secret_key_bytes, public_key_bytes). FALCON-512 via liboqs."""
    sig = oqs.Signature("Falcon-512")
    pk  = sig.generate_keypair()
    sk  = sig.export_secret_key()
    return sk, pk


def sign(message_bytes, secret_key_bytes):
    """Sign with FALCON-512. Returns signature bytes (variable length, max ~752 bytes)."""
    return oqs.Signature("Falcon-512", secret_key_bytes).sign(message_bytes)


_verifiers = threading.local()


def _verifier():
    """A FALCON-512 verifier for this thread.

    One per thread rather than one per call: constructing an oqs.Signature
    allocates and frees a C-side object, which measured about 8% of the
    cost of a verification, paid once per signature in every block. Kept
    thread-local rather than shared because a liboqs signature object
    carries its own state and nothing here promises it is safe to use from
    two threads at once, and both Flask workers and the node loop verify.
    """
    v = getattr(_verifiers, "falcon", None)
    if v is None:
        v = oqs.Signature("Falcon-512")
        _verifiers.falcon = v
    return v


def verify(message_bytes, signature_bytes, public_key_bytes):
    """Verify FALCON-512 signature. Returns True/False."""
    if isinstance(signature_bytes, str):
        signature_bytes = bytes.fromhex(signature_bytes)
    try:
        return _verifier().verify(message_bytes, signature_bytes, public_key_bytes)
    except Exception:
        return False


def sha256(data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def public_key_to_address(public_key_bytes):
    """sha256(pubkey) -> 132 bits -> twelve 11-bit BIP39 indices -> dotted words."""
    h = sha256(public_key_bytes)
    bits = int.from_bytes(h[:17], "big") >> (17 * 8 - ADDRESS_WORD_COUNT * WORD_BITS)
    wordlist = _WORDLIST
    words = []
    for _ in range(ADDRESS_WORD_COUNT):
        words.append(wordlist[bits & ((1 << WORD_BITS) - 1)])
        bits >>= WORD_BITS
    words.reverse()
    return ".".join(words)


def burn_address():
    """A fixed, syntactically valid address nobody holds the key to.

    Addresses are sha256(pubkey) folded into words (see
    public_key_to_address above), a one-way hash, so there is no key that
    maps *to* a chosen word sequence, only the reverse. Picking the
    wordlist's own first ADDRESS_WORD_COUNT entries, rather than a real
    address seen on chain, makes that plain on sight and costs nothing to
    verify: the odds of an actual keypair ever hashing to this exact
    sequence are the same 1-in-2^132 as for any other fixed sequence.
    Same idea as Bitcoin's well-known eater addresses or Ethereum's
    0x000...dEaD, just built from this chain's own address format instead
    of borrowed from another one.
    """
    return ".".join(_WORDLIST[:ADDRESS_WORD_COUNT])


def escrow_address():
    """Where a fee request's lock sits until it settles (see gaslock.py).
    Built like burn_address: a fixed word sequence nobody holds the key to,
    taken from further along the wordlist so the two never coincide."""
    return ".".join(_WORDLIST[ADDRESS_WORD_COUNT:2 * ADDRESS_WORD_COUNT])


def normalize_address(text):
    """The dotted address in `text`, however it was typed or pasted: words
    split by dots, spaces, commas or line breaks, in any case. ValueError says
    what is wrong (how many words there are, or which word is not in the
    list), because a person is reading it. It cannot tell a mistyped address
    from another person's: addresses carry no checksum."""
    words = [w for w in re.split(r"[.\s,;]+", str(text).strip().lower()) if w]
    if len(words) != ADDRESS_WORD_COUNT:
        raise ValueError(f"an address is {ADDRESS_WORD_COUNT} words, like "
                         f"tree.mention.pride..., and this has {len(words)}")
    for i, w in enumerate(words, 1):
        if w not in _WORDLIST_SET:
            raise ValueError(f"word {i} of the address, '{w[:20]}', is not in the word list")
    return ".".join(words)


def is_valid_address(addr):
    """True if addr is exactly ADDRESS_WORD_COUNT dot-separated words, all
    drawn from the BIP39 wordlist. Does not (and cannot) verify the address
    was actually derived from a real public key; addresses are one-way
    hashes, so any syntactically valid word sequence passes here. This only
    rejects malformed input (wrong shape, words not in the list), which is
    exactly what's needed to keep non-address strings out of 'to' fields.
    Without it, arbitrary text (including markup) ends up stored
    on-chain and rendered by the block explorer."""
    if not isinstance(addr, str):
        return False
    words = addr.split(".")
    if len(words) != ADDRESS_WORD_COUNT:
        return False
    return all(w in _WORDLIST_SET for w in words)


def canonical_json(obj) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace. ~8x faster than stdlib."""
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)


def serialize_for_signing(tx_dict) -> bytes:
    """Canonical deterministic serialization excluding 'signature'."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return canonical_json(fields)


# ---------------------------------------------------------------------------
# Key storage: Argon2id + NaCl secretbox (whitepaper Section 10)
# ---------------------------------------------------------------------------

_OPS  = nacl.pwhash.argon2id.OPSLIMIT_MODERATE
_MEM  = nacl.pwhash.argon2id.MEMLIMIT_MODERATE
_KLEN = nacl.secret.SecretBox.KEY_SIZE  # 32 bytes


def load_pubkey(path):
    """Load public key from key file (no passphrase needed)."""
    with open(path) as f:
        return bytes.fromhex(json.load(f)["public_key"])


def derive_kek_data(data, passphrase):
    """derive_kek for a key file already parsed into a dict."""
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    salt = base64.b64decode(data["salt"])
    return nacl.pwhash.argon2id.kdf(
        _KLEN, passphrase.encode(), salt,
        opslimit=data.get("ops", _OPS),
        memlimit=data.get("mem", _MEM),
    )


def derive_kek(path, passphrase):
    """Derive and return a key-encryption key (KEK) from the passphrase and
    the salt stored in the key file. The KEK is a 32-byte value suitable for
    use with NaCl secretbox. Store this instead of the passphrase; discard
    the passphrase string immediately after calling this function.

    The KEK has the same cryptographic strength as the Argon2id output and
    cannot be used to recover the original passphrase. An attacker with a
    memory dump gets a key-encryption key, not a reusable secret.
    """
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    with open(path) as f:
        data = json.load(f)
    return derive_kek_data(data, passphrase)


def decrypt_secret_key_data(data, kek):
    """decrypt_secret_key for a key file already parsed into a dict."""
    ciphertext = base64.b64decode(data["ciphertext"])
    try:
        return bytes(nacl.secret.SecretBox(kek).decrypt(ciphertext))
    except nacl.exceptions.CryptoError:
        raise ValueError("wrong passphrase or corrupted key file")


def decrypt_secret_key(path, passphrase=None, kek=None):
    """Decrypt and return secret key bytes. Never store the result.
    Supply exactly one of passphrase or kek."""
    with open(path) as f:
        data = json.load(f)
    if kek is not None:
        derived = kek
    elif passphrase:
        derived = derive_kek_data(data, passphrase)
    else:
        raise ValueError("passphrase or kek is required")
    return decrypt_secret_key_data(data, derived)
