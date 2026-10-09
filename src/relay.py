"""Relay (relay.link) client: pays a fee request from the node's ETH on Base.

One exact-output quote per payout: the recipient receives exactly the native
coin amount asked for on their chain and the node absorbs Relay's overhead.
The quote hands back a ready-made deposit transaction on Base. The node signs
whatever the API returns, so nothing here signs a quote before checking it says
what was asked: the sender, the destination chain and recipient, the amount, the
cost ceiling and the transaction's own value must all agree.
"""

import logging

import requests

import evm
import gas

log = logging.getLogger("ec.relay")

API = "https://api.relay.link"
HTTP_TIMEOUT = 20
# A deposit is a plain call into Relay's depository; it needs tens of
# thousands of gas, never hundreds of thousands. A quote asking for more is
# not the transaction the node thinks it is signing.
MAX_DEPOSIT_GAS = 300_000

_session = requests.Session()


class RelayError(Exception):
    """Relay refused, or its answer was not what was asked for."""


class RelayUnreachable(RelayError):
    """Relay could not be reached; nothing is known about the request."""


def _headers(api_key):
    h = {"content-type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def _call(method, path, api_key="", **kw):
    try:
        resp = _session.request(method, API + path, headers=_headers(api_key),
                                timeout=HTTP_TIMEOUT, **kw)
    except requests.RequestException as e:
        raise RelayUnreachable(f"{path}: {e}") from e
    try:
        body = resp.json()
    except ValueError as e:
        raise RelayUnreachable(f"{path}: unreadable reply ({resp.status_code})") from e
    if resp.status_code >= 400:
        msg = body.get("message") if isinstance(body, dict) else None
        raise RelayError(f"{path}: {msg or resp.status_code}")
    return body


def price_usd(net: gas.Network, api_key="") -> float:
    """The USD price of one whole unit of the network's gas coin."""
    body = _call("GET", "/currencies/token/price", api_key,
                 params={"address": net.currency, "chainId": net.chain_id})
    try:
        price = float(body["price"])
    except (KeyError, TypeError, ValueError) as e:
        raise RelayError("price: unexpected reply") from e
    if price <= 0:
        raise RelayError("price: not positive")
    return price


def quote(net: gas.Network, recipient: str, amount: int, sender: str, api_key=""):
    """An exact-output quote: `amount` of the network's gas coin to `recipient`,
    paid for from `sender`'s ETH on Base. Returns Relay's answer after
    check_quote() has confirmed it is the quote that was asked for."""
    body = _call("POST", "/quote/v2", api_key, json={
        "user": sender, "originChainId": evm.BASE_CHAIN_ID,
        "originCurrency": gas.NETWORKS["base"].currency,
        "destinationChainId": net.chain_id, "destinationCurrency": net.currency,
        "tradeType": "EXACT_OUTPUT", "amount": str(amount), "recipient": recipient})
    check_quote(body, net, recipient, amount, sender)
    return body


def check_quote(q, net, recipient, amount, sender):
    """Raise RelayError unless q is exactly the payout that was asked for and
    costs no more than a node's ceiling. Returns (transaction, cost_usd)."""
    try:
        d = q["details"]
        out, cin = d["currencyOut"], d["currencyIn"]
        if out["currency"]["chainId"] != net.chain_id:
            raise RelayError("quote pays out on the wrong chain")
        if out["currency"]["address"].lower() != net.currency.lower():
            raise RelayError("quote pays out the wrong asset")
        if int(out["amount"]) < amount:
            raise RelayError("quote pays out less than was asked")
        if not gas.same_address(net, d["recipient"], recipient):
            raise RelayError("quote pays someone else")
        if d["sender"].lower() != sender.lower():
            raise RelayError("quote is for another sender")
        if cin["currency"]["chainId"] != evm.BASE_CHAIN_ID or \
                cin["currency"]["address"].lower() != gas.NETWORKS["base"].currency:
            raise RelayError("quote is not paid in ETH on Base")
        cost_usd = float(cin["amountUsd"])
        items = [i for s in q["steps"] for i in s.get("items", [])]
        if len(q["steps"]) != 1 or len(items) != 1 or q["steps"][0].get("kind") != "transaction":
            raise RelayError("quote needs more than one deposit")
        tx = items[0]["data"]
        if tx["chainId"] != evm.BASE_CHAIN_ID or tx["from"].lower() != sender.lower():
            raise RelayError("quote's transaction is not from this wallet on Base")
        if int(tx["value"]) != int(cin["amount"]):
            raise RelayError("quote's transaction value does not match its cost")
        if int(tx.get("gas", 0)) > MAX_DEPOSIT_GAS:
            raise RelayError("quote's transaction asks for too much gas")
        if not evm.is_valid_address(tx["to"]):
            raise RelayError("quote's transaction has no destination")
    except RelayError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise RelayError(f"quote is not in the expected shape ({e!r})") from e
    cap = gas.NODE_CAP_USD
    if cost_usd > cap:
        raise RelayError(f"quote costs ${cost_usd:.2f}, above the ${cap:.2f} ceiling")
    return tx, cost_usd


def transaction_of(q):
    """The deposit transaction of a checked quote."""
    return q["steps"][0]["items"][0]["data"]


def request_id(q) -> str:
    return q["requestId"] if "requestId" in q else q["steps"][0]["requestId"]


def deposit(q, secret: bytes, rpc_url: str) -> str:
    """Sign and send the quote's deposit. Returns the Base transaction hash.

    Gas fields come from the live network rather than the quote, which fixes
    them at the moment it was made.
    """
    tx = transaction_of(q)
    sender = evm.address_from_secret(secret)
    if tx["from"].lower() != sender.lower():
        raise RelayError("quote is not for this wallet")
    max_fee, tip = evm.get_fee_params(rpc_url)
    raw = evm.sign_transaction(
        secret, chain_id=evm.BASE_CHAIN_ID, nonce=evm.get_nonce(rpc_url, sender),
        to=tx["to"], value=int(tx["value"]), data=bytes.fromhex(tx.get("data", "0x")[2:]),
        gas=int(tx["gas"]) * 2, max_fee_per_gas=max_fee, max_priority_fee_per_gas=tip)
    evm.send_raw_transaction(rpc_url, raw)
    return evm.transaction_hash(raw)


def status(req_id: str, api_key="") -> dict:
    """Relay's view of a request: {"status": "waiting"|"pending"|"success"|
    "failure"|"refund"|..., ...}."""
    return _call("GET", "/intents/status/v3", api_key, params={"requestId": req_id})
