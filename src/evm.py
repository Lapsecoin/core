"""EVM primitives for the node's gas wallet.

Just enough of Ethereum to hold one key, read a balance, sign a plain
EIP-1559 transaction and prove ownership of an address by signature.
No contract ABI, no wallet framework: the only contract call this node ever
makes is the deposit Relay hands back in a quote, which arrives as ready-made
calldata.

Amounts are integers in wei everywhere. A float never touches a balance.
"""

import json
import logging
import re
import secrets

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

log = logging.getLogger("ec.evm")

WEI_PER_ETH = 10 ** 18
HTTP_TIMEOUT = 15

# Base is the one chain the node holds funds on. Public endpoint by default;
# an operator with their own provider sets it in the node settings.
BASE_CHAIN_ID = 8453
DEFAULT_BASE_RPC = "https://mainnet.base.org"

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class EVMError(Exception):
    """Any chain-side failure the caller has to decide about."""


class EVMUnreachable(EVMError):
    """The RPC could not be reached. Nothing is known about the request's
    fate, so the caller retries rather than treating it as rejected."""


# ---------------------------------------------------------------------------
# Hashing, amounts, addresses
# ---------------------------------------------------------------------------

_M64 = (1 << 64) - 1
_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008)
_KECCAK_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
               (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _keccak_f(a):
    for rc in _KECCAK_RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _KECCAK_ROT[x][y])
        a = [b[x + 5 * y] ^ (~b[(x + 1) % 5 + 5 * y] & b[(x + 2) % 5 + 5 * y])
             for y in range(5) for x in range(5)]
        a[0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256 (the original padding, not SHA3-256). In pure
    Python on purpose: it hashes a few dozen bytes at a time here, and one
    less compiled dependency is one less thing that fails to install."""
    rate = 136
    msg = bytearray(data)
    msg.append(0x01)
    msg.extend(b"\x00" * (-len(msg) % rate))
    msg[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(msg[off + 8 * i: off + 8 * i + 8], "little")
        state = _keccak_f(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


# ---------------------------------------------------------------------------
# secp256k1: signing goes through OpenSSL (the secret key never touches
# Python arithmetic); recovering a signer from a signature uses only public
# data, so it is plain integer arithmetic here.
# ---------------------------------------------------------------------------

_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _j_double(p):
    x, y, z = p
    if y == 0:
        return (0, 1, 0)
    s = 4 * x * y * y % _P
    m = 3 * x * x % _P
    x3 = (m * m - 2 * s) % _P
    return (x3, (m * (s - x3) - 8 * y ** 4) % _P, 2 * y * z % _P)


def _j_add(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    u1, u2 = x1 * z2 * z2 % _P, x2 * z1 * z1 % _P
    s1, s2 = y1 * z2 ** 3 % _P, y2 * z1 ** 3 % _P
    if u1 == u2:
        return _j_double(p) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % _P, (s2 - s1) % _P
    h2 = h * h % _P
    h3 = h * h2 % _P
    x3 = (r * r - h3 - 2 * u1 * h2) % _P
    return (x3, (r * (u1 * h2 - x3) - s1 * h3) % _P, h * z1 * z2 % _P)


def _mul(k, point):
    acc, addend = (0, 1, 0), (point[0], point[1], 1)
    while k:
        if k & 1:
            acc = _j_add(acc, addend)
        addend = _j_double(addend)
        k >>= 1
    return acc


def _affine(p):
    if p[2] == 0:
        return None
    zi = pow(p[2], -1, _P)
    return (p[0] * zi * zi % _P, p[1] * zi ** 3 % _P)


def _recover(digest: bytes, r: int, s: int, v: int):
    """The (x, y) public key that signed `digest` with (r, s) and recovery
    bit v, or None."""
    if not (1 <= r < _N and 1 <= s < _N) or v not in (0, 1):
        return None
    y2 = (pow(r, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y * y % _P != y2:
        return None
    if (y & 1) != v:
        y = _P - y
    rinv = pow(r, -1, _N)
    e = int.from_bytes(digest, "big")
    q = _j_add(_mul((-e * rinv) % _N, _G), _mul(s * rinv % _N, (r, y)))
    return _affine(q)


def _private(secret: bytes):
    return ec.derive_private_key(int.from_bytes(secret, "big"), ec.SECP256K1())


def _public_xy(secret: bytes):
    n = _private(secret).public_key().public_numbers()
    return n.x, n.y


def _sign_digest(secret: bytes, digest: bytes):
    """(r, s, recovery bit) for a 32-byte digest, with the low-s form
    Ethereum requires."""
    der = _private(secret).sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = utils.decode_dss_signature(der)
    if s > _N // 2:
        s = _N - s
    want = _public_xy(secret)
    for v in (0, 1):
        if _recover(digest, r, s, v) == want:
            return r, s, v
    raise EVMError("could not sign")


def wei_to_str(wei: int, places: int = 6) -> str:
    """Wei as a decimal ETH string, truncated (never rounded up) to `places`."""
    whole, frac = divmod(int(wei), WEI_PER_ETH)
    frac_s = f"{frac:018d}"[:places]
    return f"{whole}.{frac_s}" if places else str(whole)


def str_to_wei(amount: str) -> int:
    """Parse a decimal ETH amount into wei. Raises ValueError on anything
    that is not a plain non-negative decimal with at most 18 places."""
    s = str(amount).strip()
    if not re.fullmatch(r"\d+(\.\d{1,18})?", s):
        raise ValueError("Enter an amount like 0.01")
    whole, _, frac = s.partition(".")
    return int(whole) * WEI_PER_ETH + int((frac + "0" * 18)[:18] or 0)


def _checksum(addr_hex40: str) -> str:
    """EIP-55 mixed-case form of a 40-hex-digit address (no 0x)."""
    lower = addr_hex40.lower()
    digest = keccak256(lower.encode()).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c
                          for i, c in enumerate(lower))


def is_valid_address(addr) -> bool:
    """Shape check, plus the EIP-55 checksum when the address is mixed case
    (all-lower or all-upper carries no checksum and is accepted)."""
    if not isinstance(addr, str) or not _ADDR_RE.match(addr):
        return False
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True
    return _checksum(body) == addr


def to_checksum_address(addr: str) -> str:
    if not isinstance(addr, str) or not _ADDR_RE.match(addr):
        raise ValueError("not an EVM address")
    return _checksum(addr[2:])


def address_from_public_key(pub_uncompressed: bytes) -> str:
    """pub_uncompressed is the 65-byte 0x04 form."""
    return _checksum(keccak256(pub_uncompressed[1:])[-20:].hex())


def _address_of_xy(xy) -> str:
    return address_from_public_key(b"\x04" + xy[0].to_bytes(32, "big") + xy[1].to_bytes(32, "big"))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def generate_keypair():
    """A fresh secp256k1 key. Returns (secret_bytes, checksum_address)."""
    while True:
        secret = secrets.token_bytes(32)
        if 1 <= int.from_bytes(secret, "big") < _N:
            return secret, address_from_secret(secret)


def address_from_secret(secret: bytes) -> str:
    return _address_of_xy(_public_xy(secret))


# ---------------------------------------------------------------------------
# Message signing (EIP-191 personal_sign)
# ---------------------------------------------------------------------------

def _personal_hash(message: bytes) -> bytes:
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message)


def sign_message(message: bytes, secret: bytes) -> str:
    """personal_sign: 0x + r(32) s(32) v(1, 27 or 28), what every EVM wallet
    produces and verifies."""
    r, s, v = _sign_digest(secret, _personal_hash(message))
    return "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v + 27])).hex()


def recover_message_signer(message: bytes, signature: str):
    """The checksum address that signed `message`, or None when the
    signature is malformed or recovers nothing."""
    try:
        raw = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
        if len(raw) != 65:
            return None
        v = raw[64]
        v = v - 27 if v >= 27 else v
        if v not in (0, 1):
            return None
        xy = _recover(_personal_hash(message), int.from_bytes(raw[:32], "big"),
                      int.from_bytes(raw[32:64], "big"), v)
        return None if xy is None else _address_of_xy(xy)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# RLP and EIP-1559 transactions
# ---------------------------------------------------------------------------

def _int_bytes(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


def rlp_encode(item) -> bytes:
    if isinstance(item, int):
        item = _int_bytes(item)
    if isinstance(item, (bytes, bytearray)):
        b = bytes(item)
        if len(b) == 1 and b[0] < 0x80:
            return b
        return _rlp_len(len(b), 0x80) + b
    if isinstance(item, (list, tuple)):
        body = b"".join(rlp_encode(x) for x in item)
        return _rlp_len(len(body), 0xC0) + body
    raise TypeError(f"cannot RLP-encode {type(item).__name__}")


def _rlp_len(n: int, offset: int) -> bytes:
    if n < 56:
        return bytes([offset + n])
    nb = _int_bytes(n)
    return bytes([offset + 55 + len(nb)]) + nb


def _addr_bytes(addr: str) -> bytes:
    if not is_valid_address(addr):
        raise ValueError("not an EVM address")
    return bytes.fromhex(addr[2:])


def sign_transaction(secret: bytes, *, chain_id: int, nonce: int, to: str,
                     value: int, data: bytes = b"", gas: int,
                     max_fee_per_gas: int, max_priority_fee_per_gas: int) -> bytes:
    """A signed type-2 (EIP-1559) transaction, ready for eth_sendRawTransaction."""
    fields = [chain_id, nonce, max_priority_fee_per_gas, max_fee_per_gas, gas,
              _addr_bytes(to), value, bytes(data), []]
    digest = keccak256(b"\x02" + rlp_encode(fields))
    r, s, v = _sign_digest(secret, digest)
    return b"\x02" + rlp_encode(fields + [v, r, s])


def transaction_hash(raw: bytes) -> str:
    return "0x" + keccak256(raw).hex()


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

_session = requests.Session()


def rpc(url: str, method: str, params=None):
    """One JSON-RPC call. EVMUnreachable when the endpoint cannot be
    reached or answers garbage, EVMError when the node refuses the call."""
    try:
        resp = _session.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or []}, timeout=HTTP_TIMEOUT)
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise EVMUnreachable(f"{method}: {e}") from e
    if not isinstance(body, dict):
        raise EVMUnreachable(f"{method}: unexpected reply")
    if "error" in body:
        err = body["error"]
        raise EVMError(f"{method}: {err.get('message') if isinstance(err, dict) else err}")
    return body.get("result")


def _hex_int(value) -> int:
    try:
        return int(value, 16)
    except (TypeError, ValueError) as e:
        raise EVMUnreachable(f"unexpected number in reply: {value!r}") from e


def get_balance_wei(url: str, addr: str) -> int:
    return _hex_int(rpc(url, "eth_getBalance", [addr, "latest"]))


def get_nonce(url: str, addr: str) -> int:
    return _hex_int(rpc(url, "eth_getTransactionCount", [addr, "pending"]))


def get_fee_params(url: str):
    """(max_fee_per_gas, max_priority_fee_per_gas) in wei: twice the current
    base fee as headroom, plus the node's suggested tip."""
    block = rpc(url, "eth_getBlockByNumber", ["latest", False])
    base = _hex_int((block or {}).get("baseFeePerGas"))
    tip = _hex_int(rpc(url, "eth_maxPriorityFeePerGas"))
    return 2 * base + tip, tip


def estimate_gas(url: str, tx: dict) -> int:
    return _hex_int(rpc(url, "eth_estimateGas", [tx]))


def gas_price_wei(url: str) -> int:
    """Current gas price, for sizing a request. The base fee plus the tip."""
    max_fee, tip = get_fee_params(url)
    return (max_fee - tip) // 2 + tip


def send_raw_transaction(url: str, raw: bytes) -> str:
    return rpc(url, "eth_sendRawTransaction", ["0x" + raw.hex()])


def get_receipt(url: str, tx_hash: str):
    """The receipt dict, or None while the transaction is still pending."""
    return rpc(url, "eth_getTransactionReceipt", [tx_hash])


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)
