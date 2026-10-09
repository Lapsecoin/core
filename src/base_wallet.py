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

log = logging.getLogger("ec.base_wallet")

KEY_FILE_NAME = "base_gas.key"


def key_path_for(node_keyfile: str) -> str:
    """The gas wallet lives next to the node key."""
    return os.path.join(os.path.dirname(os.path.abspath(node_keyfile)), KEY_FILE_NAME)


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
    with open(tmp, "w") as f:
        json.dump({"type": "base_gas_wallet",
                   "address": address,
                   "ciphertext": base64.b64encode(ciphertext).decode()}, f, indent=2)
    os.chmod(tmp, 0o600)
    # Renamed into place: a crash partway through a direct write leaves a
    # truncated key file and the wallet is gone.
    os.replace(tmp, path)
    return address


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
