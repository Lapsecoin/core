"""Deterministic FALCON-512 key generation from a 48-byte seed.

liboqs only draws its own randomness for key generation, so this loads a
small library built from the same PQClean Falcon-512 sources (native/
falcon_seeded/) with the seed passed in. The keys come out in the identical
format, so crypto.sign and crypto.verify (liboqs) work on them unchanged.

On first load the library's SHAKE256 is checked against hashlib, so a bad
build fails loudly instead of deriving keys nobody can reproduce."""

import ctypes
import hashlib
import os
import sys
import threading

import resources

SEED_BYTES = 48
SECRET_KEY_BYTES = 1281
PUBLIC_KEY_BYTES = 897

_lib = None
_lock = threading.Lock()


def _candidates():
    names = {"win32": ["falcon_seeded.dll"], "darwin": ["libfalcon_seeded.dylib"]}
    names = names.get(sys.platform, ["libfalcon_seeded.so"])
    root = resources.bundle_dir()
    for name in names:
        yield os.path.join(root, "native", "falcon_seeded", name)
        yield os.path.join(root, name)


def _load():
    global _lib
    with _lock:
        if _lib is not None:
            return _lib
        last = None
        for path in _candidates():
            if not os.path.exists(path):
                continue
            try:
                lib = ctypes.CDLL(path)
            except OSError as e:
                last = e
                continue
            _self_test(lib)
            _lib = lib
            return lib
    raise RuntimeError(
        "the seeded FALCON library is missing; build it with "
        "'make -C native/falcon_seeded'" + (f" ({last})" if last else ""))


def _self_test(lib):
    for data in (b"", b"lapsecoin", bytes(range(200))):
        out = ctypes.create_string_buffer(64)
        lib.falcon_seeded_shake256(out, 64, data, len(data))
        if out.raw != hashlib.shake_256(data).digest(64):
            raise RuntimeError("the seeded FALCON library's SHAKE256 is wrong")


def available():
    try:
        _load()
        return True
    except RuntimeError:
        return False


def keypair_from_seed(seed):
    """(secret_key_bytes, public_key_bytes) for a 48-byte seed, in the same
    order and format as crypto.generate_keypair."""
    if len(seed) != SEED_BYTES:
        raise ValueError(f"the seed must be {SEED_BYTES} bytes")
    lib = _load()
    pk = ctypes.create_string_buffer(PUBLIC_KEY_BYTES)
    sk = ctypes.create_string_buffer(SECRET_KEY_BYTES)
    try:
        if lib.falcon512_keypair_from_seed(pk, sk, bytes(seed)) != 0:
            raise RuntimeError("seeded FALCON key generation failed")
        return sk.raw, pk.raw
    finally:
        ctypes.memset(sk, 0, SECRET_KEY_BYTES)
