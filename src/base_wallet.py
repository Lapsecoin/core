"""The node's gas wallet: one secp256k1 key holding ETH on Base.

Sealed under the same key-encryption key as the node key. A node keeps its
kek while it runs and drops the passphrase at startup on purpose, so a
wallet sealed behind a passphrase of its own would leave nothing in memory
able to open it, and an unattended node could never pay a request it had
claimed. One passphrase for the operator, and a wallet the running node can
actually use.
"""

import base64
import json
import logging
import os

import nacl.exceptions
import nacl.secret
import nacl.utils

import evm
import relay

log = logging.getLogger("ec.base_wallet")

KEY_FILE_NAME = "base_gas.key"


def default_path() -> str:
    """The gas wallet lives in the working directory, with the node's other
    files (an installed build has already moved there, see datadir.py)."""
    return os.path.abspath(KEY_FILE_NAME)


def create(path: str, kek: bytes) -> str:
    """Generate a key, seal it under `kek`, write it atomically (0600).
    Returns the address. Refuses to overwrite an existing wallet."""
    if os.path.exists(path):
        raise FileExistsError(path)
    secret, address = evm.generate_keypair()
    box = nacl.secret.SecretBox(kek)
    ciphertext = box.encrypt(secret)
    del secret
    tmp = path + ".tmp"
    # Created 0600 from the start, so the file is never readable by others.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"type": "base_gas_wallet",
                   "address": address,
                   "ciphertext": base64.b64encode(ciphertext).decode()}, f, indent=2)
    # Renamed into place: a crash partway through a direct write leaves a
    # truncated key file and the wallet is gone.
    os.replace(tmp, path)
    return address


def reseal(path: str, old_kek: bytes, new_kek: bytes) -> bool:
    """Seal an existing gas wallet under a new key-encryption key. The gas
    wallet opens with the node's kek, so when the node's key changes (an old
    key moving to a recovery phrase) the file would otherwise be locked for
    good. Does nothing when there is no wallet or it already opens with
    `new_kek`. Returns True if it was re-sealed; ValueError if neither kek
    opens it."""
    if not os.path.isfile(path):
        return False
    try:
        decrypt_secret(path, new_kek)
        return False
    except ValueError:
        pass
    secret = decrypt_secret(path, old_kek)
    try:
        with open(path) as f:
            data = json.load(f)
        data["ciphertext"] = base64.b64encode(nacl.secret.SecretBox(new_kek).encrypt(secret)).decode()
    finally:
        del secret
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    log.info("[base_wallet] the gas wallet is now sealed under the new wallet key")
    return True


def ensure(path: str, kek: bytes) -> str:
    """The wallet's address, creating the wallet on first start."""
    address = load_address(path)
    if address:
        return address
    address = create(path, kek)
    log.info("[startup] created a Base gas wallet: %s", address)
    return address


def load_address(path: str):
    """The address, readable without unlocking. None if there is no wallet."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            address = json.load(f)["address"]
    except (OSError, ValueError, KeyError):
        log.warning("[base_wallet] key file unreadable: %s", path)
        return None
    return address if evm.is_valid_address(address) else None


def decrypt_secret(path: str, kek: bytes) -> bytes:
    """The secret key bytes. Callers drop them immediately."""
    with open(path) as f:
        data = json.load(f)
    try:
        secret = bytes(nacl.secret.SecretBox(kek).decrypt(
            base64.b64decode(data["ciphertext"])))
    except (nacl.exceptions.CryptoError, KeyError, ValueError):
        raise ValueError("wrong passphrase or corrupted key file")
    if evm.address_from_secret(secret) != data.get("address"):
        raise ValueError("the key file does not match its address")
    return secret


def send_eth(path: str, kek: bytes, rpc_url: str, to: str, value_wei: int):
    """Send plain ETH on Base from the gas wallet. Returns the tx hash.

    A plain transfer, so the gas limit is the fixed 21000 and the fee is
    known before signing: the balance check below is exact, not a guess.
    """
    if not evm.is_valid_address(to):
        raise ValueError("That is not a valid EVM address.")
    if value_wei <= 0:
        raise ValueError("Enter an amount above zero.")
    secret = decrypt_secret(path, kek)
    try:
        sender = evm.address_from_secret(secret)
        if to.lower() == sender.lower():
            raise ValueError("That is this wallet's own address.")
        gas = 21_000
        max_fee, tip = evm.get_fee_params(rpc_url)
        balance = evm.get_balance_wei(rpc_url, sender)
        if value_wei + gas * max_fee > balance:
            raise ValueError(
                f"That is more than this wallet can send; {evm.wei_to_str(balance)} ETH "
                f"is held and the network fee needs up to {evm.wei_to_str(gas * max_fee, 8)} ETH.")
        raw = evm.sign_transaction(
            secret, chain_id=evm.BASE_CHAIN_ID, nonce=evm.get_nonce(rpc_url, sender),
            to=to, value=value_wei, gas=gas, max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=tip)
        evm.send_raw_transaction(rpc_url, raw)
        return evm.transaction_hash(raw)
    finally:
        del secret


def send_exit(path: str, kek: bytes, rpc_url: str, net, token_address: str,
              to: str, value_wei: int, api_key: str = ""):
    """Spend `value_wei` of the wallet's ETH on Base to deliver `token_address`
    on `net` to `to`, through Relay. Returns the Base transaction hash.
    The quote is checked (relay.check_exit_quote) before anything is signed."""
    if value_wei <= 0:
        raise ValueError("Enter an amount above zero.")
    secret = decrypt_secret(path, kek)
    try:
        sender = evm.address_from_secret(secret)
        max_fee, _tip = evm.get_fee_params(rpc_url)
        balance = evm.get_balance_wei(rpc_url, sender)
        headroom = relay.MAX_DEPOSIT_GAS * 2 * max_fee
        if value_wei + headroom > balance:
            raise ValueError(
                f"That is more than this wallet can send; {evm.wei_to_str(balance)} ETH "
                f"is held and the network fee needs up to {evm.wei_to_str(headroom, 8)} ETH.")
        q = relay.quote_exit(net, token_address, to, value_wei, sender, api_key)
        return relay.deposit(q, secret, rpc_url)
    except relay.RelayError as e:
        raise ValueError(str(e)) from e
    finally:
        del secret
