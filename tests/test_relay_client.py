"""The Relay client: a quote is only signed if it says what was asked."""

import copy
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import evm
import gas
import relay

KEY = (5).to_bytes(32, "big")
ME = evm.address_from_secret(KEY)
DEST = "0x000000000000000000000000000000000000dEaD"
OP = gas.NETWORKS["optimism"]
ZERO = "0x" + "0" * 40


def good_quote(amount=10 ** 14, cost_wei=108065790187740, cost_usd="0.268387", dest=DEST):
    """Shaped like a real /quote/v2 answer."""
    return {
        "requestId": "0xreq",
        "steps": [{"id": "deposit", "kind": "transaction", "requestId": "0xreq", "items": [{
            "status": "incomplete",
            "data": {"from": ME, "to": "0x4cd00e387622c35bddb9b4c962c136462338bc31",
                     "data": "0x49290c1c00ff", "value": str(cost_wei), "chainId": 8453,
                     "gas": "32432", "maxFeePerGas": "6500000", "maxPriorityFeePerGas": "1000000"},
            "check": {"endpoint": "/intents/status/v3?requestId=0xreq", "method": "GET"}}]}],
        "details": {
            "sender": ME, "recipient": dest,
            "currencyIn": {"currency": {"chainId": 8453, "address": ZERO}, "amount": str(cost_wei),
                           "amountUsd": cost_usd},
            "currencyOut": {"currency": {"chainId": 10, "address": ZERO}, "amount": str(amount),
                            "amountUsd": "0.248369"}},
    }


def tweak(q, path, value):
    q = copy.deepcopy(q)
    cur = q
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return q


class TestCheckQuote:
    def test_the_real_shape_passes(self):
        tx, cost = relay.check_quote(good_quote(), OP, DEST, 10 ** 14, ME)
        assert tx["chainId"] == 8453 and cost == pytest.approx(0.268387)

    def test_paying_out_more_than_asked_is_fine(self):
        relay.check_quote(good_quote(amount=2 * 10 ** 14), OP, DEST, 10 ** 14, ME)

    @pytest.mark.parametrize("label,path,value,message", [
        ("wrong chain", ["details", "currencyOut", "currency", "chainId"], 1, "wrong chain"),
        ("wrong asset", ["details", "currencyOut", "currency", "address"], "0x" + "1" * 40, "wrong asset"),
        ("less than asked", ["details", "currencyOut", "amount"], str(10 ** 14 - 1), "less than"),
        ("someone else", ["details", "recipient"], "0x" + "2" * 40, "someone else"),
        ("another sender", ["details", "sender"], "0x" + "3" * 40, "another sender"),
        ("not ETH", ["details", "currencyIn", "currency", "address"], "0x" + "4" * 40, "not paid in ETH"),
        ("not Base", ["details", "currencyIn", "currency", "chainId"], 1, "not paid in ETH"),
        ("tx on another chain", ["steps", 0, "items", 0, "data", "chainId"], 1, "not from this wallet"),
        ("tx from another wallet", ["steps", 0, "items", 0, "data", "from"], "0x" + "5" * 40, "not from this wallet"),
        ("tx value differs from cost", ["steps", 0, "items", 0, "data", "value"], "999999999999999999",
         "value does not match"),
        ("too much gas", ["steps", 0, "items", 0, "data", "gas"], "9999999", "no usable gas limit"),
        ("no destination", ["steps", 0, "items", 0, "data", "to"], "nothing", "no destination"),
        ("not a transaction", ["steps", 0, "kind"], "signature", "more than one deposit"),
    ])
    def test_a_quote_that_is_not_what_was_asked_is_refused(self, label, path, value, message):
        with pytest.raises(relay.RelayError, match=message):
            relay.check_quote(tweak(good_quote(), path, value), OP, DEST, 10 ** 14, ME)

    def test_a_quote_over_the_ceiling_is_refused(self):
        with pytest.raises(relay.RelayError, match="ceiling"):
            relay.check_quote(good_quote(cost_usd="2.01"), OP, DEST, 10 ** 14, ME)

    def test_two_steps_are_refused(self):
        q = good_quote()
        q["steps"].append(copy.deepcopy(q["steps"][0]))
        with pytest.raises(relay.RelayError, match="more than one"):
            relay.check_quote(q, OP, DEST, 10 ** 14, ME)

    @pytest.mark.parametrize("q", [{}, {"details": {}}, {"steps": []}, None, "x", []])
    def test_garbage_is_a_relay_error_not_a_crash(self, q):
        with pytest.raises(relay.RelayError):
            relay.check_quote(q, OP, DEST, 10 ** 14, ME)

    def test_solana_recipients_compare_exactly(self):
        sol = gas.NETWORKS["solana"]
        a = "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK"
        q = good_quote(amount=2_050_000, dest=a)
        q["details"]["currencyOut"]["currency"] = {"chainId": sol.chain_id, "address": sol.currency}
        relay.check_quote(q, sol, a, 2_050_000, ME)
        with pytest.raises(relay.RelayError, match="someone else"):
            relay.check_quote(tweak(q, ["details", "recipient"], a.lower()), sol, a, 2_050_000, ME)


class FakeHttp:
    def __init__(self, monkeypatch, status=200, body=None, exc=None):
        self.calls = []
        self.status, self.body, self.exc = status, body, exc
        monkeypatch.setattr(relay._session, "request", self._req)

    def _req(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.exc:
            raise self.exc
        outer = self

        class R:
            status_code = outer.status

            def json(self):
                if outer.body is ValueError:
                    raise ValueError("html")
                return outer.body
        return R()


class TestCalls:
    def test_quote_asks_for_exact_output_to_the_recipient(self, monkeypatch):
        http = FakeHttp(monkeypatch, body=good_quote())
        relay.quote(OP, DEST, 10 ** 14, ME, api_key="k")
        method, url, kw = http.calls[0]
        assert (method, url) == ("POST", relay.API + "/quote/v2")
        assert kw["json"] == {"user": ME, "originChainId": 8453, "originCurrency": ZERO,
                              "destinationChainId": 10, "destinationCurrency": ZERO,
                              "tradeType": "EXACT_OUTPUT", "amount": str(10 ** 14), "recipient": DEST}
        assert kw["headers"]["x-api-key"] == "k"

    def test_no_key_means_no_header(self, monkeypatch):
        http = FakeHttp(monkeypatch, body=good_quote())
        relay.quote(OP, DEST, 10 ** 14, ME)
        assert "x-api-key" not in http.calls[0][2]["headers"]

    def test_a_bad_quote_from_the_api_never_leaves_quote(self, monkeypatch):
        FakeHttp(monkeypatch, body=tweak(good_quote(), ["details", "recipient"], "0x" + "9" * 40))
        with pytest.raises(relay.RelayError, match="someone else"):
            relay.quote(OP, DEST, 10 ** 14, ME)

    def test_price(self, monkeypatch):
        http = FakeHttp(monkeypatch, body={"price": 740.25})
        assert relay.price_usd(gas.NETWORKS["bsc"]) == 740.25
        assert http.calls[0][2]["params"] == {"address": ZERO, "chainId": 56}

    @pytest.mark.parametrize("body", [{}, {"price": "x"}, {"price": 0}, {"price": -1}, None])
    def test_a_bad_price_is_an_error(self, monkeypatch, body):
        FakeHttp(monkeypatch, body=body)
        with pytest.raises(relay.RelayError):
            relay.price_usd(OP)

    def test_a_refusal_carries_relays_message(self, monkeypatch):
        FakeHttp(monkeypatch, status=400, body={"message": "amount too low"})
        with pytest.raises(relay.RelayError, match="amount too low"):
            relay.quote(OP, DEST, 1, ME)

    def test_unreachable_and_unreadable_are_distinct_from_a_refusal(self, monkeypatch):
        FakeHttp(monkeypatch, exc=requests.ConnectionError("down"))
        with pytest.raises(relay.RelayUnreachable):
            relay.status("0xreq")
        FakeHttp(monkeypatch, body=ValueError)
        with pytest.raises(relay.RelayUnreachable):
            relay.status("0xreq")

    def test_status(self, monkeypatch):
        http = FakeHttp(monkeypatch, body={"status": "success"})
        assert relay.status("0xreq") == {"status": "success"}
        assert http.calls[0][2]["params"] == {"requestId": "0xreq"}


class TestDeposit:
    def test_signs_the_quotes_call_with_live_fees_and_sends_it(self, monkeypatch):
        sent = []
        monkeypatch.setattr(evm, "get_fee_params", lambda url: (7_000_000, 1_000))
        monkeypatch.setattr(evm, "get_nonce", lambda url, a: 11)
        monkeypatch.setattr(evm, "send_raw_transaction", lambda url, raw: sent.append(raw) or "0x")
        tx_hash = relay.deposit(good_quote(), KEY, "http://rpc")
        raw = sent[0]
        assert tx_hash == evm.transaction_hash(raw) and raw[:1] == b"\x02"
        assert bytes.fromhex("49290c1c00ff") in raw
        assert bytes.fromhex("4cd00e387622c35bddb9b4c962c136462338bc31") in raw

    def test_a_quote_for_another_wallet_is_not_signed(self, monkeypatch):
        monkeypatch.setattr(evm, "send_raw_transaction", lambda *a: pytest.fail("sent"))
        other = tweak(good_quote(), ["steps", 0, "items", 0, "data", "from"], "0x" + "7" * 40)
        with pytest.raises(relay.RelayError, match="not for this wallet"):
            relay.deposit(other, KEY, "http://rpc")
