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

import requests
from Crypto.Hash import keccak
from coincurve import PrivateKey, PublicKey

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

def keccak256(data: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


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


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def generate_keypair():
    """A fresh secp256k1 key. Returns (secret_bytes, checksum_address)."""
    sk = PrivateKey()
    return sk.secret, address_from_public_key(sk.public_key.format(compressed=False))


def address_from_secret(secret: bytes) -> str:
    return address_from_public_key(PrivateKey(secret).public_key.format(compressed=False))


# ---------------------------------------------------------------------------
# Message signing (EIP-191 personal_sign)
# ---------------------------------------------------------------------------

def _personal_hash(message: bytes) -> bytes:
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message)


def sign_message(message: bytes, secret: bytes) -> str:
    """personal_sign: 0x + r(32) s(32) v(1, 27 or 28), what every EVM wallet
    produces and verifies."""
    sig = PrivateKey(secret).sign_recoverable(_personal_hash(message), hasher=None)
    return "0x" + (sig[:64] + bytes([sig[64] + 27])).hex()


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
        pub = PublicKey.from_signature_and_message(
            raw[:64] + bytes([v]), _personal_hash(message), hasher=None)
        return address_from_public_key(pub.format(compressed=False))
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
    sig = PrivateKey(secret).sign_recoverable(digest, hasher=None)
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:64], "big")
    return b"\x02" + rlp_encode(fields + [sig[64], r, s])


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
