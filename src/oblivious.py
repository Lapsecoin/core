"""One-hop oblivious requests: ask a node something without the node that
carries the question being able to read it, or the node that reads it
knowing who asked.

A light client has to name its wallet address to learn a balance or a nonce,
and to submit a transaction, and whoever it asks sees its IP at the same
moment. Dandelion keeps a transaction's origin from the network at large,
but it cannot help with the first node, which sees the sender directly. This
is the same idea applied to that hop: the client seals the request to a
second node (the target), and sends the sealed bytes through a first node
(the relay) that cannot open them. The relay sees an IP and an opaque blob of
one of a few sizes. The target sees the request, and that it came from the
relay. Neither alone links the address to the IP. They would have to be the
same operator, or compare notes.

  client --sealed to T--> relay R --sealed to T--> target T
  client <--sealed to client's one-time key-- R <-- T

Sealed boxes (X25519 and XSalsa20-Poly1305, from PyNaCl, already a
dependency): the request is sealed to the target's public key, which it
advertises in /api/info, and it carries a one-time public key of the
client's for the answer to be sealed to. Both directions are padded to a
fixed set of sizes so a blob does not say whether it is a balance lookup or a
transaction.

Light-safe: imports nothing beyond PyNaCl and the standard library.
"""

import base64
import json
import struct

import nacl.exceptions
from nacl.public import PrivateKey, PublicKey, SealedBox

# The sizes a padded message is rounded up to. A transaction, which carries
# a public key and a signature, is the largest thing sent; the rest fit in
# the smallest.
BUCKETS = (1024, 2048, 4096, 8192)

# What a node will accept or pass on, sealing overhead included.
MAX_BLOB = BUCKETS[-1] + 48

META_KEY = "oblivious_secret"


def pad(data):
    """data with its length in front, zero-filled up to the next bucket."""
    need = len(data) + 4
    size = next((b for b in BUCKETS if b >= need), None)
    if size is None:
        raise ValueError("too large to send")
    return struct.pack(">I", len(data)) + data + b"\x00" * (size - need)


def unpad(padded):
    if len(padded) < 4 or len(padded) not in BUCKETS:
        raise ValueError("bad padding")
    (n,) = struct.unpack(">I", padded[:4])
    if n > len(padded) - 4:
        raise ValueError("bad padding")
    return padded[4:4 + n]


def parse_key(text):
    """The raw 32-byte public key a node advertises as base64, or ValueError."""
    if not isinstance(text, str):
        raise ValueError("a key is text")
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        raise ValueError("not base64")
    if len(raw) != 32:
        raise ValueError("not a 32-byte key")
    return raw


class ObliviousService:
    """A node's side: its key, and opening a sealed request for it."""

    def __init__(self, meta):
        """meta: anything with get_meta(key, default) and set_meta(key, value),
        which is how the node already keeps small values (see storage.py)."""
        stored = meta.get_meta(META_KEY)
        try:
            self._key = PrivateKey(base64.b64decode(stored, validate=True))
        except (TypeError, ValueError, nacl.exceptions.TypeError):
            self._key = PrivateKey.generate()
            meta.set_meta(META_KEY, base64.b64encode(bytes(self._key)).decode())
        self.public_b64 = base64.b64encode(bytes(self._key.public_key)).decode()

    def open_request(self, blob):
        """(request dict, client's one-time public key) for a sealed request,
        or ValueError. Nothing about why it failed is worth telling a caller
        who cannot be trusted to have sent a real one."""
        try:
            plain = unpad(SealedBox(self._key).decrypt(blob))
            req = json.loads(plain)
            client = PublicKey(parse_key(req["k"]))
            if not isinstance(req["m"], str) or not isinstance(req["p"], str):
                raise ValueError("malformed")
        except (nacl.exceptions.CryptoError, ValueError, KeyError, TypeError):
            raise ValueError("cannot open")
        return req, client

    @staticmethod
    def seal_response(client_key, status, body):
        """The answer, sealed to the one-time key the request carried."""
        plain = json.dumps({"s": status, "b": body}, separators=(",", ":")).encode()
        return SealedBox(client_key).encrypt(pad(plain))


def seal_request(target_key_b64, method, path, body=None):
    """(blob, one-time private key) for a request to the target whose public
    key is target_key_b64. Keep the private key to open the answer."""
    one_time = PrivateKey.generate()
    req = {"m": method, "p": path, "b": body,
           "k": base64.b64encode(bytes(one_time.public_key)).decode()}
    plain = json.dumps(req, separators=(",", ":")).encode()
    return SealedBox(PublicKey(parse_key(target_key_b64))).encrypt(pad(plain)), one_time


def open_response(one_time, blob):
    """(status, body) from a sealed answer, or ValueError."""
    try:
        reply = json.loads(unpad(SealedBox(one_time).decrypt(blob)))
        return int(reply["s"]), reply["b"]
    except (nacl.exceptions.CryptoError, ValueError, KeyError, TypeError):
        raise ValueError("cannot open the answer")
