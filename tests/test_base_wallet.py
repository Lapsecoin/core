"""The Base gas wallet: sealed under the node key, created once, spendable
only with the right passphrase, and shown on the send page."""

import json
import os
import re
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import base_send
import gas
import base_wallet
import crypto
import evm
import peerpool as peerpool_mod
import relay
import settings as settings_mod
from chainstate import ChainState
from params import TICKS_PER_LAPSE
from tests.browser import submit
from tests.test_light import PASS, _FullNode

DEST = "0x000000000000000000000000000000000000dEaD"
RPC = "http://rpc.test"
ZERO = "0x" + "0" * 40


@pytest.fixture
def kek(tmp_path):
    keyfile = str(tmp_path / "node.key")
    sk, pk = crypto.generate_keypair()
    crypto.save_key(keyfile, sk, pk, PASS)
    return keyfile, crypto.derive_kek(keyfile, PASS)


class TestKeyFile:
    def test_created_once_and_stable(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        addr = base_wallet.ensure(path, kek[1])
        assert evm.is_valid_address(addr)
        assert base_wallet.ensure(path, kek[1]) == addr
        assert base_wallet.load_address(path) == addr

    def test_create_never_overwrites(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        base_wallet.create(path, kek[1])
        with pytest.raises(FileExistsError):
            base_wallet.create(path, kek[1])

    def test_file_is_private_and_holds_no_secret_in_the_clear(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        base_wallet.create(path, kek[1])
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        secret = base_wallet.decrypt_secret(path, kek[1])
        assert secret.hex() not in open(path).read()
        assert not os.path.exists(path + ".tmp")

    def test_wrong_key_does_not_open_it(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        base_wallet.create(path, kek[1])
        with pytest.raises(ValueError, match="wrong passphrase"):
            base_wallet.decrypt_secret(path, os.urandom(32))

    def test_a_swapped_address_is_caught(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        base_wallet.create(path, kek[1])
        data = json.load(open(path))
        data["address"] = evm.generate_keypair()[1]
        json.dump(data, open(path, "w"))
        with pytest.raises(ValueError, match="does not match"):
            base_wallet.decrypt_secret(path, kek[1])

    def test_missing_or_unreadable_file_has_no_address(self, tmp_path):
        assert base_wallet.load_address(str(tmp_path / "none.key")) is None
        bad = tmp_path / "bad.key"
        bad.write_text("not json")
        assert base_wallet.load_address(str(bad)) is None

    def test_lives_next_to_the_node_key(self, tmp_path):
        assert base_wallet.key_path_for(str(tmp_path / "node.key")) == str(tmp_path / "base_gas.key")


class FakeChain:
    """A Base endpoint that holds one balance and records what is sent."""

    def __init__(self, monkeypatch, balance, max_fee=1_000_000, tip=1_000):
        self.sent = []
        self.balance = balance
        monkeypatch.setattr(evm, "get_fee_params", lambda url: (max_fee, tip))
        monkeypatch.setattr(evm, "get_balance_wei", lambda url, a: self.balance)
        monkeypatch.setattr(evm, "get_nonce", lambda url, a: 3)
        monkeypatch.setattr(evm, "send_raw_transaction",
                            lambda url, raw: self.sent.append(raw) or "0x")


class TestSendEth:
    def _wallet(self, tmp_path, kek):
        path = str(tmp_path / "base_gas.key")
        return path, base_wallet.create(path, kek[1])

    def test_sends_a_signed_transfer_with_the_expected_fields(self, tmp_path, kek, monkeypatch):
        chain = FakeChain(monkeypatch, balance=10 ** 18)
        path, addr = self._wallet(tmp_path, kek)
        tx_hash = base_wallet.send_eth(path, kek[1], RPC, DEST, 10 ** 15)
        assert len(chain.sent) == 1 and tx_hash == evm.transaction_hash(chain.sent[0])
        raw = chain.sent[0]
        assert raw[:1] == b"\x02"
        # chain id 8453 (0x2105), nonce 3, to, value 1e15 are all in the payload
        assert bytes.fromhex("822105") in raw and bytes.fromhex(DEST[2:].lower()) in raw

    def test_refuses_more_than_balance_plus_the_network_fee(self, tmp_path, kek, monkeypatch):
        chain = FakeChain(monkeypatch, balance=10 ** 15, max_fee=10 ** 9)
        path, _ = self._wallet(tmp_path, kek)
        with pytest.raises(ValueError, match="more than this wallet can send"):
            base_wallet.send_eth(path, kek[1], RPC, DEST, 10 ** 15)
        assert chain.sent == []

    @pytest.mark.parametrize("value", [0, -5])
    def test_refuses_nothing_to_send(self, tmp_path, kek, monkeypatch, value):
        FakeChain(monkeypatch, balance=10 ** 18)
        path, _ = self._wallet(tmp_path, kek)
        with pytest.raises(ValueError):
            base_wallet.send_eth(path, kek[1], RPC, DEST, value)

    def test_refuses_a_bad_or_own_address(self, tmp_path, kek, monkeypatch):
        chain = FakeChain(monkeypatch, balance=10 ** 18)
        path, addr = self._wallet(tmp_path, kek)
        with pytest.raises(ValueError, match="valid EVM address"):
            base_wallet.send_eth(path, kek[1], RPC, "0xabc", 1)
        with pytest.raises(ValueError, match="own address"):
            base_wallet.send_eth(path, kek[1], RPC, addr, 1)
        assert chain.sent == []

    def test_wrong_key_sends_nothing(self, tmp_path, kek, monkeypatch):
        chain = FakeChain(monkeypatch, balance=10 ** 18)
        path, _ = self._wallet(tmp_path, kek)
        with pytest.raises(ValueError):
            base_wallet.send_eth(path, os.urandom(32), RPC, DEST, 1)
        assert chain.sent == []


@pytest.fixture
def page(tmp_path, kek, monkeypatch):
    keyfile, kek_bytes = kek
    path = str(tmp_path / "base_gas.key")
    addr = base_wallet.create(path, kek_bytes)
    cs = ChainState.from_genesis()
    node = _FullNode(cs)
    node.keyfile = keyfile
    node.base_wallet_path = path
    chain = FakeChain(monkeypatch, balance=5 * 10 ** 17)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    return type("P", (), dict(node=node, client=client, addr=addr, chain=chain))


class TestSendPage:
    def test_shows_the_address_and_balance(self, page):
        html = page.client.get("/send").get_data(as_text=True)
        assert page.addr in html and "0.500000 ETH" in html
        assert "ETH on Base" in html and 'id="base-panel"' in html

    def test_balance_reads_unavailable_when_the_endpoint_is_down(self, page, monkeypatch):
        def down(url, a):
            raise evm.EVMUnreachable("down")
        monkeypatch.setattr(evm, "get_balance_wei", down)
        html = page.client.get("/send").get_data(as_text=True)
        assert page.addr in html and "unavailable" in html

    def test_a_send_goes_through_and_is_reported_once(self, page):
        r = submit(page.client, "/send", asset="base", base_to=DEST, base_network="base",
                   base_token=ZERO, base_amount="0.01", passphrase=PASS)
        html = r.get_data(as_text=True)
        assert "Sent from Base." in html and len(page.chain.sent) == 1
        assert evm.transaction_hash(page.chain.sent[0]) in html

    def test_wrong_passphrase_sends_nothing_and_says_so(self, page):
        html = submit(page.client, "/send", asset="base", base_to=DEST, base_network="base",
                      base_token=ZERO, base_amount="0.01", passphrase="wrong").get_data(as_text=True)
        assert page.chain.sent == []
        assert re.search(r"alert-err[^>]*>[^<]*wrong passphrase", html)

    @pytest.mark.parametrize("fields,message", [
        (dict(base_to="", base_amount="0.01"), "Enter a destination"),
        (dict(base_to=DEST, base_amount="lots"), "Enter an amount"),
        (dict(base_to="nope", base_amount="0.01"), "valid Base address"),
        (dict(base_to=DEST, base_amount="0.01", passphrase=""), "Passphrase required"),
        (dict(base_to=DEST, base_amount="0.01", base_network="dogechain"), "Pick a network"),
        (dict(base_to=DEST, base_amount="0.01", base_token="0x" + "9" * 40), "Pick a token"),
    ])
    def test_bad_input_is_explained_and_nothing_is_sent(self, page, fields, message):
        fields.setdefault("passphrase", PASS)
        fields.setdefault("base_network", "base")
        fields.setdefault("base_token", ZERO)
        html = submit(page.client, "/send", asset="base", **fields).get_data(as_text=True)
        assert message in html and page.chain.sent == []

    def test_a_send_of_lapse_still_works_beside_it(self, page):
        # the LAPSE form is untouched by the new tab
        html = page.client.get("/send").get_data(as_text=True)
        assert 'id="lapse-panel"' in html and 'name="outputs"' in html

    def test_without_a_wallet_the_tab_says_so(self, page):
        os.remove(page.node.base_wallet_path)
        html = page.client.get("/send").get_data(as_text=True)
        assert "no Base gas wallet yet" in html


def test_rpc_url_falls_back_to_the_public_endpoint():
    class N:
        settings = settings_mod.Settings(type("M", (), dict(
            get_meta=lambda self, k: "  ", set_meta=lambda *a: None))())
    assert base_send.BaseSend(N())._rpc_url() == evm.DEFAULT_BASE_RPC


USDC_OP = "0x0b2c639c533813f4aa9d7837caf62653d097ff85"
LISTED = {10: [{"symbol": "ETH", "address": ZERO, "decimals": 18},
               {"symbol": "USDC", "address": USDC_OP, "decimals": 6}],
          8453: [{"symbol": "ETH", "address": ZERO, "decimals": 18}]}


def exit_quote(sender, dest, amount, chain_id=10, token=USDC_OP, impact="-0.24"):
    return {
        "requestId": "0xreq",
        "steps": [{"id": "deposit", "kind": "transaction", "items": [{"data": {
            "from": sender, "to": "0x4cd00e387622c35bddb9b4c962c136462338bc31",
            "data": "0x49290c1c", "value": str(amount), "chainId": 8453, "gas": "32432"}}]}],
        "details": {
            "sender": sender, "recipient": dest,
            "totalImpact": {"usd": "-0.06", "percent": impact},
            "currencyIn": {"currency": {"chainId": 8453, "address": ZERO}, "amount": str(amount),
                           "amountUsd": "24.79"},
            "currencyOut": {"currency": {"chainId": chain_id, "address": token, "symbol": "USDC",
                                         "decimals": 6},
                            "amount": "24739355", "minimumAmount": "24244568", "amountUsd": "24.73"}}}


class TestSendFromBase:
    """Sending to another network or token is a quoted swap through Relay."""

    @pytest.fixture
    def relayed(self, page, monkeypatch):
        calls = {"quotes": [], "deposits": []}
        monkeypatch.setattr(relay, "tokens", lambda key="": LISTED)

        def quote(net, token, dest, amount, sender, api_key=""):
            calls["quotes"].append((net.slug, token, dest, amount))
            return exit_quote(sender, dest, amount, net.chain_id, token)
        monkeypatch.setattr(relay, "quote_exit", quote)
        monkeypatch.setattr(relay, "deposit", lambda q, secret, url: calls["deposits"].append(q) or "0xdeposit")
        return type("R", (), dict(page=page, calls=calls))

    def test_the_token_list_starts_with_the_gas_coin(self, relayed):
        d = relayed.page.client.get("/api/send/base/tokens?network=optimism").get_json()
        assert [t["symbol"] for t in d["tokens"]] == ["ETH", "USDC"]
        assert relayed.page.client.get("/api/send/base/tokens?network=nope").status_code == 400

    def test_the_token_list_falls_back_to_the_gas_coin_when_relay_is_down(self, relayed, monkeypatch):
        def down(key=""):
            raise relay.RelayUnreachable("down")
        monkeypatch.setattr(relay, "tokens", down)
        d = relayed.page.client.get("/api/send/base/tokens?network=optimism").get_json()
        assert [t["symbol"] for t in d["tokens"]] == ["ETH"]

    def test_a_plain_eth_transfer_on_base_needs_no_quote(self, relayed):
        d = relayed.page.client.get("/api/send/base/preview", query_string=dict(
            network="base", token=ZERO, to=DEST, amount="0.01")).get_json()
        assert d["ok"] and d["direct"] and relayed.calls["quotes"] == []

    def test_the_preview_says_what_will_arrive(self, relayed):
        d = relayed.page.client.get("/api/send/base/preview", query_string=dict(
            network="optimism", token=USDC_OP, to=DEST, amount="0.01")).get_json()
        assert d["ok"] and not d["direct"]
        assert (d["receive"], d["minimum"], d["symbol"], d["decimals"]) == (24739355, 24244568, "USDC", 6)
        assert d["impact_percent"] == pytest.approx(0.24)
        assert relayed.calls["quotes"] == [("optimism", USDC_OP, DEST, 10 ** 16)]

    def test_the_preview_explains_bad_input_and_a_failed_quote(self, relayed, monkeypatch):
        r = relayed.page.client.get("/api/send/base/preview", query_string=dict(
            network="optimism", token=USDC_OP, to="nope", amount="0.01"))
        assert r.status_code == 400 and "valid Optimism address" in r.get_json()["error"]

        def refuse(*a, **k):
            raise relay.RelayError("no route")
        monkeypatch.setattr(relay, "quote_exit", refuse)
        r = relayed.page.client.get("/api/send/base/preview", query_string=dict(
            network="optimism", token=USDC_OP, to=DEST, amount="0.01"))
        assert r.status_code == 502 and "no route" in r.get_json()["error"]

    def test_a_send_to_another_network_goes_through_relay(self, relayed):
        r = submit(relayed.page.client, "/send", asset="base", base_to=DEST, base_network="optimism",
                   base_token=USDC_OP, base_amount="0.01", passphrase=PASS)
        html = r.get_data(as_text=True)
        assert "Sent from Base." in html and "0xdeposit" in html
        assert len(relayed.calls["deposits"]) == 1 and relayed.page.chain.sent == []
        assert relayed.calls["quotes"][-1] == ("optimism", USDC_OP, DEST, 10 ** 16)

    def test_a_token_on_base_itself_is_a_swap_not_a_transfer(self, relayed, monkeypatch):
        usdc_base = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        LISTED[8453].append({"symbol": "USDC", "address": usdc_base, "decimals": 6})
        try:
            submit(relayed.page.client, "/send", asset="base", base_to=DEST, base_network="base",
                   base_token=usdc_base, base_amount="0.01", passphrase=PASS)
        finally:
            LISTED[8453].pop()
        assert len(relayed.calls["deposits"]) == 1 and relayed.page.chain.sent == []

    def test_it_will_not_spend_more_than_the_wallet_holds(self, relayed):
        html = submit(relayed.page.client, "/send", asset="base", base_to=DEST, base_network="optimism",
                      base_token=USDC_OP, base_amount="5", passphrase=PASS).get_data(as_text=True)
        assert "more than this wallet can send" in html and relayed.calls["deposits"] == []

    def test_a_relay_refusal_is_shown_and_nothing_is_sent(self, relayed, monkeypatch):
        def refuse(*a, **k):
            raise relay.RelayError("amount too low")
        monkeypatch.setattr(relay, "quote_exit", refuse)
        html = submit(relayed.page.client, "/send", asset="base", base_to=DEST, base_network="optimism",
                      base_token=USDC_OP, base_amount="0.01", passphrase=PASS).get_data(as_text=True)
        assert "amount too low" in html and relayed.calls["deposits"] == []


class TestExitQuoteChecks:
    ME = evm.address_from_secret((5).to_bytes(32, "big"))
    OP = gas.NETWORKS["optimism"]

    def check(self, q, amount=10 ** 16, token=USDC_OP, dest=DEST):
        return relay.check_exit_quote(q, self.OP, token, dest, amount, self.ME)

    def test_a_good_quote_is_summarised(self):
        s = self.check(exit_quote(self.ME, DEST, 10 ** 16))
        assert s["receive"] == 24739355 and s["minimum"] == 24244568 and s["symbol"] == "USDC"
        assert s["impact_percent"] == pytest.approx(0.24)

    def test_a_quote_spending_a_different_amount_is_refused(self):
        with pytest.raises(relay.RelayError, match="different amount"):
            self.check(exit_quote(self.ME, DEST, 2 * 10 ** 16))

    @pytest.mark.parametrize("path,value,message", [
        (["details", "recipient"], "0x" + "2" * 40, "someone else"),
        (["details", "currencyOut", "currency", "address"], "0x" + "3" * 40, "wrong asset"),
        (["details", "currencyOut", "currency", "chainId"], 1, "wrong chain"),
        (["details", "currencyOut", "minimumAmount"], "0", "pays out nothing"),
        (["details", "totalImpact", "percent"], "-35", "loses 35.0%"),
        (["steps", 0, "items", 0, "data", "value"], "1", "does not match"),
    ])
    def test_a_quote_that_is_not_what_was_asked_is_refused(self, path, value, message):
        q = exit_quote(self.ME, DEST, 10 ** 16)
        cur = q
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
        with pytest.raises(relay.RelayError, match=message):
            self.check(q)

    def test_garbage_is_a_relay_error(self):
        for q in ({}, {"details": {}}, None):
            with pytest.raises(relay.RelayError):
                self.check(q)

    def test_the_token_list_is_read_from_the_chain_data(self, monkeypatch):
        relay._tokens_cache.update(at=0.0, data={})
        body = {"chains": [{"id": 10, "featuredTokens": [
            {"symbol": "ETH", "address": ZERO, "decimals": 18, "name": "Ether"}]},
            {"id": 99, "disabled": True, "featuredTokens": [{"symbol": "X", "address": "a", "decimals": 1}]}]}
        calls = []
        monkeypatch.setattr(relay, "_call", lambda *a, **k: calls.append(a) or body)
        assert relay.tokens() == {10: [{"symbol": "ETH", "address": ZERO, "decimals": 18}]}
        relay.tokens()
        assert len(calls) == 1                   # cached
        relay._tokens_cache.update(at=0.0, data={})
