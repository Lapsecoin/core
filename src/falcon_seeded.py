"""Deterministic FALCON-512 key generation from a 48-byte seed.

liboqs only draws its own randomness for key generation, so this loads a
small library built from the same PQClean Falcon-512 sources (native/
falcon_seeded/) with the seed passed in. The keys come out in the identical
format, so crypto.sign and crypto.verify (liboqs) work on them unchanged.

On first load the library is checked: its SHAKE256 against hashlib and one
seed against the public key the site publishes for it, so a bad build fails
loudly instead of deriving keys nobody can reproduce."""

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import threading

import resources

SEED_BYTES = 48
SECRET_KEY_BYTES = 1281
PUBLIC_KEY_BYTES = 897

_lib = None
_lock = threading.Lock()


def _lib_names():
    return {"win32": ["falcon_seeded.dll"],
            "darwin": ["libfalcon_seeded.dylib"]}.get(sys.platform, ["libfalcon_seeded.so"])


def _search_dirs():
    """Where the library may be: beside the program (a release build), in the
    source tree, and with liboqs (the prebuilt install scripts put it there)."""
    root = resources.bundle_dir()
    dirs = [os.path.join(root, "native", "falcon_seeded"), root,
            os.path.join(root, "_oqs", "bin"), os.path.join(root, "_oqs", "lib")]
    for base in (os.environ.get("OQS_INSTALL_PATH"), os.path.join(os.path.expanduser("~"), "_oqs")):
        if base:
            dirs += [os.path.join(base, "lib"), os.path.join(base, "bin")]
    dirs.append(_cache_dir())
    dirs += ["/usr/local/lib", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"]
    return dirs


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or base
    return os.path.join(base, "lapsecoin")


def _candidates():
    for d in _search_dirs():
        for name in _lib_names():
            yield os.path.join(d, name)


def _build_from_source():
    """Compile the library into the cache directory with a C compiler, for a
    source checkout or a pip install that no prebuilt copy has reached.
    Returns the path, or None when there is no compiler or no source."""
    src = os.path.join(resources.bundle_dir(), "native", "falcon_seeded")
    sources = ["seeded.c", "keygen.c", "codec.c", "common.c", "fft.c", "fpr.c",
               "rng.c", "vrfy.c", "keccak.c"]
    if not all(os.path.exists(os.path.join(src, f)) for f in sources):
        return None
    cc = next((c for c in (os.environ.get("CC"), "cc", "gcc", "clang") if c and shutil.which(c)), None)
    if cc is None or sys.platform == "win32":
        return None
    out_dir = _cache_dir()
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, _lib_names()[0])
    tmp = f"{out}.{os.getpid()}.tmp"
    cmd = [cc, "-O2", "-fPIC", "-fvisibility=hidden", "-shared", "-o", tmp,
           *[os.path.join(src, f) for f in sources], "-I", src]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        os.replace(tmp, out)
    except (OSError, subprocess.SubprocessError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return out


def _load():
    global _lib
    with _lock:
        if _lib is not None:
            return _lib
        last = None
        tried_build = False
        while True:
            for path in _candidates():
                if not os.path.exists(path):
                    continue
                try:
                    lib = ctypes.CDLL(path)
                    _self_test(lib)
                except (OSError, RuntimeError, AttributeError) as e:
                    last = e
                    continue
                _lib = lib
                return lib
            if tried_build:
                break
            tried_build = True
            if _build_from_source() is None:
                break
    raise RuntimeError(
        "the seeded FALCON library is missing. The install scripts fetch it with liboqs; "
        "from a source checkout run 'make -C native/falcon_seeded' (needs a C compiler)"
        + (f" ({last})" if last else ""))


# The first vector on the site's wallets page: this seed must give this public
# key. A library that does not is not the pinned Falcon key generation, and
# keys from it would not restore anywhere else.
_KAT_SEED = bytes.fromhex(
    "fb49a518faeecb48d54921bd28ad8b9cee7b0e58f856f7968cdbe874f968ba07"
    "e39ac6960e48974e6913c4d353338e95")
_KAT_PUBLIC_KEY_SHA256 = "e7f162aa47073308083eda3238a6817d53092ba55407c8e57f65df5e829ed515"


def _self_test(lib):
    for data in (b"", b"lapsecoin", bytes(range(200))):
        out = ctypes.create_string_buffer(64)
        lib.falcon_seeded_shake256(out, 64, data, len(data))
        if out.raw != hashlib.shake_256(data).digest(64):
            raise RuntimeError("the seeded FALCON library's SHAKE256 is wrong")
    pk = ctypes.create_string_buffer(PUBLIC_KEY_BYTES)
    sk = ctypes.create_string_buffer(SECRET_KEY_BYTES)
    if lib.falcon512_keypair_from_seed(pk, sk, _KAT_SEED) != 0 or \
            hashlib.sha256(pk.raw).hexdigest() != _KAT_PUBLIC_KEY_SHA256:
        raise RuntimeError("the seeded FALCON library does not reproduce the published key")
    ctypes.memset(sk, 0, SECRET_KEY_BYTES)


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
