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
import base_wallet
import crypto
import evm
import peerpool as peerpool_mod
import settings as settings_mod
from chainstate import ChainState
from params import TICKS_PER_LAPSE
from tests.browser import submit
from tests.test_light import PASS, _FullNode

DEST = "0x000000000000000000000000000000000000dEaD"
RPC = "http://rpc.test"


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
        r = submit(page.client, "/send", asset="base", base_to=DEST,
                   base_amount="0.01", passphrase=PASS)
        html = r.get_data(as_text=True)
        assert "Sent on Base." in html and len(page.chain.sent) == 1
        assert evm.transaction_hash(page.chain.sent[0]) in html

    def test_wrong_passphrase_sends_nothing_and_says_so(self, page):
        html = submit(page.client, "/send", asset="base", base_to=DEST,
                      base_amount="0.01", passphrase="wrong").get_data(as_text=True)
        assert page.chain.sent == []
        assert re.search(r"alert-err[^>]*>[^<]*wrong passphrase", html)

    @pytest.mark.parametrize("fields,message", [
        (dict(base_to="", base_amount="0.01"), "Enter a destination"),
        (dict(base_to=DEST, base_amount="lots"), "Enter an amount"),
        (dict(base_to="nope", base_amount="0.01"), "valid EVM address"),
        (dict(base_to=DEST, base_amount="0.01", passphrase=""), "Passphrase required"),
    ])
    def test_bad_input_is_explained_and_nothing_is_sent(self, page, fields, message):
        fields.setdefault("passphrase", PASS)
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
