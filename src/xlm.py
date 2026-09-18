"""Stellar (XLM) side of the P2P swap.

Why Stellar rather than a Bitcoin fork: a swap is split into increments, so
the counter-asset's per-transaction cost is multiplied by the increment
count. At Litecoin's average fee a four-increment trade on two dollars
loses nine percent to fees, and a fee spike can exceed the trade value
outright. Stellar's fee is fixed at 100 stroops (0.00001 XLM) regardless
of load, which makes the increment count a safety dial rather than a cost.

The second reason matters more for correctness: Stellar finality is
deterministic. A transaction is final on inclusion, roughly five seconds,
with no reorg. "Is this increment settled?" is a boolean here, not a
confidence level, so only the LapseCoin side needs a confirmation depth.

Idempotency
-----------
Every payment carries a sequence number that is part of the signed
payload, so one signed envelope can apply at most once no matter how many
times it is submitted. That is what makes a crash mid-send safe: the
envelope is persisted before it is ever put on the wire, and recovery
re-submits that same envelope rather than building a new one. Either it
already applied, and Stellar rejects the duplicate, or it applies now.
Neither path can pay twice. See submit_envelope.
"""

import base64
import decimal
import json
import logging
import os
import time

import nacl.pwhash
import nacl.secret
import nacl.utils
import requests
from stellar_sdk import (
    Account, Asset, Keypair, Network, TransactionBuilder, TransactionEnvelope,
)
from stellar_sdk.exceptions import (
    BadRequestError, BadResponseError, NotFoundError, ConnectionError as SdkConnectionError,
)

log = logging.getLogger("ec.xlm")

HORIZON_URL = "https://horizon.stellar.org"
NETWORK_PASSPHRASE = Network.PUBLIC_NETWORK_PASSPHRASE

# Stellar amounts carry exactly 7 decimal places. Everything internal is an
# integer count of these (stroops) so no float ever touches a balance.
STROOPS_PER_XLM = 10_000_000

# Fixed by the protocol, not a market: 100 stroops per operation.
BASE_FEE_STROOPS = 100

# Two base reserves to bring an account into existence. Held by the
# account, not spent, and recoverable by merging the account away. A
# brand-new seller never has to fund this themselves: see
# build_sponsored_create_account.
BASE_RESERVE_STROOPS = 5_000_000          # 0.5 XLM
ACCOUNT_MIN_BALANCE_STROOPS = 2 * BASE_RESERVE_STROOPS

# How long a built envelope stays submittable. Past this the network
# rejects it outright, which is the property recovery leans on: an
# envelope is either still valid and safe to re-submit, or provably dead
# and safe to rebuild. Without a bound both states look identical forever.
TX_TIMEOUT_SECONDS = 180

# Horizon is a public endpoint with rate limits, so every read goes
# through one session with a short timeout rather than an unbounded wait
# on a node loop.
HTTP_TIMEOUT = 15

_session = requests.Session()


class XLMError(Exception):
    """Any Stellar-side failure that the caller has to decide about."""


class XLMUnreachable(XLMError):
    """Horizon could not be reached. Distinct from a rejection: nothing is
    known about the transaction's fate, so the caller must retry rather
    than treat it as failed."""


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

def stroops_to_str(stroops):
    """Stroops as the decimal string the Stellar API expects.

    Via Decimal rather than a float divide: 0.1 + 0.2 arithmetic on a
    balance is how a payment ends up one stroop short of what was agreed,
    and the amount here is compared against what a counterparty expects.

    Formatted to a fixed seven places rather than str()'d. Decimal renders
    small values in scientific notation ("1E-7"), which Stellar rejects as
    a malformed amount, so a one-stroop payment would fail on a format
    detail rather than anything about the payment.
    """
    return f"{decimal.Decimal(stroops) / decimal.Decimal(STROOPS_PER_XLM):.7f}"


def str_to_stroops(amount_str):
    """Parse a Stellar decimal amount into whole stroops."""
    quantized = (decimal.Decimal(str(amount_str))
                 * decimal.Decimal(STROOPS_PER_XLM)).to_integral_value()
    return int(quantized)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def generate_keypair():
    """A real Ed25519 Stellar keypair. Returns (secret_seed, public_key)."""
    kp = Keypair.random()
    return kp.secret, kp.public_key


def is_valid_address(address):
    try:
        Keypair.from_public_key(address)
        return True
    except Exception:
        return False


def save_key(path, secret_seed, public_key, passphrase=None, kek=None):
    """Encrypt the seed to disk. Supply exactly one of passphrase or kek.

    Same scheme crypto.py uses for the LapseCoin wallet: NaCl secretbox
    under a key-encryption key, file mode 0600, and the seed never touches
    disk in the clear.

    The kek form is what the node actually uses, and the reason is the
    swap worker. A node keeps its key-encryption key while it runs and
    throws the passphrase away at startup, on purpose. If this seed were
    sealed under a passphrase of its own there would be nothing in memory
    able to open it, so every step of every trade would need somebody
    present to type it, and an unattended node could never finish a trade
    it had already started paying into. Sealing it under the same kek
    means one passphrase for the user and a wallet the running node can
    actually use.
    """
    # Emptiness, not just absence: an empty passphrase is a missing one,
    # and testing `is None` alone would let "" through and seal a wallet
    # behind nothing.
    if bool(passphrase) == bool(kek):
        raise ValueError("supply exactly one of a non-empty passphrase or a kek")
    ops = nacl.pwhash.argon2id.OPSLIMIT_MODERATE
    mem = nacl.pwhash.argon2id.MEMLIMIT_MODERATE
    if kek is not None:
        salt = b""
        key = kek
    else:
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        key = nacl.pwhash.argon2id.kdf(nacl.secret.SecretBox.KEY_SIZE,
                                       passphrase.encode(), salt,
                                       opslimit=ops, memlimit=mem)
    ciphertext = nacl.secret.SecretBox(key).encrypt(secret_seed.encode())
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"type": "xlm_trading_wallet",
                   "public_key": public_key,
                   "ciphertext": base64.b64encode(ciphertext).decode(),
                   "salt": base64.b64encode(salt).decode(),
                   "sealed_with": "kek" if kek is not None else "passphrase",
                   "ops": ops, "mem": mem}, f, indent=2)
    os.chmod(tmp, 0o600)
    # Renamed into place rather than written over: a crash partway through
    # a direct write leaves a truncated key file and the wallet is gone.
    os.replace(tmp, path)


def load_public_key(path):
    """The address, readable without the passphrase."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)["public_key"]
    except (OSError, ValueError, KeyError):
        log.warning("[xlm] key file unreadable: %s", path)
        return None


def decrypt_seed(path, passphrase=None, kek=None):
    """Decrypt and return the secret seed. Callers drop it immediately.

    Supply whichever the file was sealed with. The file records which, so
    a caller offering the wrong one gets told that rather than a generic
    corruption error.
    """
    with open(path) as f:
        data = json.load(f)
    sealed_with = data.get("sealed_with", "passphrase")
    if sealed_with == "kek":
        if kek is None:
            raise ValueError("this wallet is sealed with the node key; "
                             "unlock the node rather than supplying a passphrase")
        key = kek
    else:
        if not passphrase:
            raise ValueError("this wallet needs its passphrase")
        key = nacl.pwhash.argon2id.kdf(
            nacl.secret.SecretBox.KEY_SIZE, passphrase.encode(),
            base64.b64decode(data["salt"]),
            opslimit=data.get("ops", nacl.pwhash.argon2id.OPSLIMIT_MODERATE),
            memlimit=data.get("mem", nacl.pwhash.argon2id.MEMLIMIT_MODERATE))
    try:
        plaintext = nacl.secret.SecretBox(key).decrypt(
            base64.b64decode(data["ciphertext"]))
    except nacl.exceptions.CryptoError:
        raise ValueError("wrong key or corrupted key file")
    return plaintext.decode()


# ---------------------------------------------------------------------------
# Horizon reads
# ---------------------------------------------------------------------------

def _get(path, params=None):
    """One Horizon GET. Raises XLMUnreachable on transport failure so the
    caller can tell "the network is down" from "the answer is no"; those
    need opposite handling and collapsing them is how a stalled trade gets
    misread as an abandoned one."""
    try:
        resp = _session.get(f"{HORIZON_URL}{path}", params=params,
                            timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise XLMUnreachable(f"horizon unreachable: {e}") from e
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        raise XLMUnreachable("horizon rate limited")
    if resp.status_code >= 500:
        raise XLMUnreachable(f"horizon error {resp.status_code}")
    if resp.status_code >= 400:
        raise XLMError(f"horizon rejected request: {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise XLMError(f"horizon returned non-JSON: {e}") from e


def account_exists(address):
    """Whether the account is on the ledger. An address that has never been
    funded is a valid address with no account behind it, and cannot receive
    a plain payment; it needs a create-account operation instead."""
    return _get(f"/accounts/{address}") is not None


def get_balance_stroops(address):
    """Native XLM balance in stroops. 0 for an account that does not exist
    yet, which is the truth rather than an error: nothing is there."""
    data = _get(f"/accounts/{address}")
    if data is None:
        return 0
    for bal in data.get("balances", []):
        if bal.get("asset_type") == "native":
            return str_to_stroops(bal["balance"])
    return 0


def get_spendable_stroops(address):
    """Balance minus the locked minimum, which is what can actually be sent.

    Reported separately from get_balance_stroops because a wallet showing
    its full balance as available is how a user builds a payment the
    network then refuses: the reserve is held, not spent, but it cannot be
    paid away.
    """
    data = _get(f"/accounts/{address}")
    if data is None:
        return 0
    subentries = data.get("subentry_count", 0)
    # Sponsored entries are paid for by somebody else, so they do not raise
    # this account's own floor. Horizon reports how many are sponsored.
    sponsored = data.get("num_sponsored", 0)
    owned = max(subentries - sponsored, 0)
    reserve = (2 + owned) * BASE_RESERVE_STROOPS
    if data.get("sponsor"):
        # The base reserve itself is carried by a sponsor.
        reserve = owned * BASE_RESERVE_STROOPS
    balance = 0
    for bal in data.get("balances", []):
        if bal.get("asset_type") == "native":
            balance = str_to_stroops(bal["balance"])
    return max(balance - reserve, 0)


def get_sequence(address):
    """Current sequence number, which the next transaction increments."""
    data = _get(f"/accounts/{address}")
    if data is None:
        raise XLMError(f"account does not exist: {address}")
    return int(data["sequence"])


def get_transaction(tx_hash):
    """A transaction by hash, or None if the network has never seen it.

    None genuinely means absent: Horizon 404s an unknown hash, and a
    transport failure raises instead, so a caller can treat None as
    "did not land" without it silently also meaning "could not ask".
    """
    return _get(f"/transactions/{tx_hash}")


def transaction_succeeded(tx_hash):
    """True only if the transaction is on the ledger and succeeded.

    Both halves matter. Stellar records failed transactions on the ledger
    too (the fee is still charged), so presence alone is not settlement.
    """
    data = get_transaction(tx_hash)
    return bool(data and data.get("successful") is True)


def find_payment(to_address, memo, min_stroops, from_address=None, limit=200):
    """Look for a settled incoming payment matching what was agreed.

    This is the check that actually decides an increment, so it verifies
    every term rather than trusting the counterparty's word that they
    paid: the destination, the memo tying it to this session and
    increment, the sending account, and an amount at or above what was
    agreed. Over-payment passes, under-payment does not.

    Returns the transaction hash, or None if no such payment has settled.
    """
    data = _get(f"/accounts/{to_address}/payments",
                {"limit": min(limit, 200), "order": "desc"})
    if data is None:
        return None
    for record in data.get("_embedded", {}).get("records", []):
        if record.get("type") not in ("payment", "create_account"):
            continue
        if record.get("transaction_successful") is False:
            continue
        if record.get("type") == "payment":
            if record.get("asset_type") != "native":
                continue
            if record.get("to") != to_address:
                continue
            paid = str_to_stroops(record.get("amount", "0"))
            sender = record.get("from")
        else:
            if record.get("account") != to_address:
                continue
            paid = str_to_stroops(record.get("starting_balance", "0"))
            sender = record.get("funder")
        if paid < min_stroops:
            continue
        if from_address and sender != from_address:
            continue
        tx_hash = record.get("transaction_hash")
        if tx_hash and _memo_matches(tx_hash, memo):
            return tx_hash
    return None


def _memo_matches(tx_hash, memo):
    tx = get_transaction(tx_hash)
    if not tx:
        return False
    return tx.get("memo_type") == "text" and tx.get("memo") == memo


# ---------------------------------------------------------------------------
# Building transactions
# ---------------------------------------------------------------------------

def _builder(source_public, sequence):
    # Horizon reports the account's current sequence and the SDK increments
    # it, so the caller passes what it read rather than this re-reading and
    # risking a different answer than the one already persisted.
    return TransactionBuilder(
        source_account=Account(source_public, sequence),
        network_passphrase=NETWORK_PASSPHRASE,
        base_fee=BASE_FEE_STROOPS)


def build_payment(secret_seed, destination, stroops, memo, sequence):
    """A signed payment carrying the session tag. Returns (xdr, tx_hash).

    Nothing is submitted here. The envelope comes back so the caller can
    persist it first and submit second, which is the whole basis of
    recovery: what is on disk before the send is exactly what is re-sent
    after a crash, sequence number and all.
    """
    kp = Keypair.from_secret(secret_seed)
    tx = (_builder(kp.public_key, sequence)
          .add_text_memo(memo)
          .append_payment_op(destination=destination,
                             asset=Asset.native(),
                             amount=stroops_to_str(stroops))
          .set_timeout(TX_TIMEOUT_SECONDS)
          .build())
    tx.sign(kp)
    return tx.to_xdr(), tx.hash_hex()


def build_create_account(secret_seed, destination, stroops, memo, sequence):
    """Fund a not-yet-existing account into being, carrying the session tag.

    A plain payment to an address with no account behind it fails, so the
    first delivery to a brand-new counterparty has to be this instead. The
    starting balance must clear the minimum for the account to exist at
    all.
    """
    if stroops < ACCOUNT_MIN_BALANCE_STROOPS:
        raise XLMError(
            f"starting balance {stroops_to_str(stroops)} XLM is below the "
            f"{stroops_to_str(ACCOUNT_MIN_BALANCE_STROOPS)} XLM minimum an "
            f"account needs to exist")
    kp = Keypair.from_secret(secret_seed)
    tx = (_builder(kp.public_key, sequence)
          .add_text_memo(memo)
          .append_create_account_op(destination=destination,
                                    starting_balance=stroops_to_str(stroops))
          .set_timeout(TX_TIMEOUT_SECONDS)
          .build())
    tx.sign(kp)
    return tx.to_xdr(), tx.hash_hex()


def build_sponsored_create_account(sponsor_seed, destination_seed, memo, sequence):
    """Bring an account into existence holding nothing, with the sponsor
    carrying its reserve.

    This is what lets somebody holding only LAPSE sell it for XLM. Their
    address cannot receive a payment until an account exists behind it,
    and creating one normally costs the minimum balance they do not have
    yet. Under CAP-33 the sponsor carries that reserve instead, so the
    new account exists at a zero balance and the seller funds nothing.

    Both sponsorship operations must sit in one transaction and both
    accounts must sign it, so neither side can do this to the other
    unilaterally. The sponsor's own minimum balance rises while the
    sponsorship stands, and it can be handed back once the account has
    funds of its own.
    """
    sponsor = Keypair.from_secret(sponsor_seed)
    new_account = Keypair.from_secret(destination_seed)
    tx = (_builder(sponsor.public_key, sequence)
          .add_text_memo(memo)
          .append_begin_sponsoring_future_reserves_op(
              sponsored_id=new_account.public_key)
          .append_create_account_op(destination=new_account.public_key,
                                    starting_balance="0")
          .append_end_sponsoring_future_reserves_op(
              source=new_account.public_key)
          .set_timeout(TX_TIMEOUT_SECONDS)
          .build())
    tx.sign(sponsor)
    tx.sign(new_account)
    return tx.to_xdr(), tx.hash_hex()


def envelope_hash(xdr):
    """The hash of an already-built envelope, without re-signing it."""
    return TransactionEnvelope.from_xdr(xdr, NETWORK_PASSPHRASE).hash_hex()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def submit_envelope(xdr):
    """Submit a signed envelope. Safe to call repeatedly with the same one.

    Returns (ok, tx_hash, detail).

    Re-submission is the recovery path, not an error case, so the two ways
    a repeat can come back are both treated as success: the transaction
    may already be on the ledger, or its sequence number may now be
    consumed, and in both the payment this envelope represents has
    happened exactly once. Building a *fresh* envelope after a crash is
    what would pay twice, which is why the caller persists this one first.

    A transport failure raises rather than returning False. Nothing is
    known about the transaction's fate in that case, and recording it as
    failed would strand a payment that is about to settle.
    """
    tx_hash = envelope_hash(xdr)
    try:
        resp = _session.post(f"{HORIZON_URL}/transactions",
                             data={"tx": xdr}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise XLMUnreachable(f"submit failed, fate unknown: {e}") from e

    if resp.status_code == 200:
        body = resp.json()
        if body.get("successful") is True:
            return True, tx_hash, "submitted"
        return False, tx_hash, _result_code(body)

    if resp.status_code in (400, 409):
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        code = _result_code(body)
        # Already applied, under either name Horizon gives it.
        if code in ("tx_bad_seq", "duplicate"):
            if transaction_succeeded(tx_hash):
                return True, tx_hash, "already applied"
            # The sequence is spent but not by this envelope, so this one
            # can never apply. Distinct from a duplicate and the caller
            # has to rebuild rather than retry.
            return False, tx_hash, "sequence consumed by another transaction"
        return False, tx_hash, code

    if resp.status_code == 429 or resp.status_code >= 500:
        raise XLMUnreachable(f"horizon unavailable: {resp.status_code}")

    return False, tx_hash, f"http {resp.status_code}"


def _result_code(body):
    extras = body.get("extras", {}) or {}
    result = extras.get("result_codes", {}) or {}
    return result.get("transaction") or body.get("title") or "unknown"


# ---------------------------------------------------------------------------
# Price reference (advisory only, never gates a trade)
# ---------------------------------------------------------------------------

_price_cache = {"usd": 0.0, "at": 0.0}
PRICE_TTL_SECONDS = 300


def get_xlm_usd():
    """XLM in USD for display. 0.0 when unavailable.

    Advisory only: it labels a quote so a user can sanity-check it, and
    never blocks posting or taking an order. A trade's terms are the two
    amounts the parties agreed, which this never touches.
    """
    if time.time() - _price_cache["at"] < PRICE_TTL_SECONDS:
        return _price_cache["usd"]
    try:
        resp = _session.get("https://api.coingecko.com/api/v3/simple/price",
                            params={"ids": "stellar", "vs_currencies": "usd"},
                            timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            usd = float(resp.json().get("stellar", {}).get("usd", 0.0))
            if usd > 0:
                _price_cache.update(usd=usd, at=time.time())
                return usd
    except (requests.RequestException, ValueError, TypeError):
        log.debug("[xlm] price lookup failed", exc_info=True)
    return _price_cache["usd"]
