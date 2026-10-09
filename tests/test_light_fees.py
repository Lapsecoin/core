"""The light client can ask for gas too: it prices and signs on its own
machine, sends the request through its node, and reads the request's
progress from that node."""

import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import crypto
import evm
import fees_ui
import gas
import gaslock
import peerpool as peerpool_mod
import tx as tx_mod
from chainstate import ChainState
from light_app import create_light_app
from node import NodeView
from params import TICKS_PER_LAPSE
from remote_reader import RemoteReader
from tests.browser import submit, tokens
from tests.fixtures import make_block
from tests.test_gas_worker import DEST, DEST_KEY, ESC, FakeIO
from tests.test_light import PASS, _FullNode, _Session
from wallet import Wallet

GAS_PRICE = 10 ** 10


@pytest.fixture
def light(tmp_path):
    sk, pk = crypto.generate_keypair()
    keyfile = str(tmp_path / "wallet.key")
    crypto.save_key(keyfile, sk, pk, PASS)
    wallet = Wallet(keyfile, pk)
    cs = ChainState.from_genesis()
    cs.state.credit(wallet.addr, 1000 * TICKS_PER_LAPSE)
    node = _FullNode(cs)
    node.view = NodeView(cs)
    node.gas_io = FakeIO()                         # what the remote node sees
    node.storage.get_tx_height = lambda txid: next(
        (b["height"] for b in node.cs.chain for t in b.get("transactions", [])
         if tx_mod.tx_hash(t) == txid), None)
    app = api.create_app(node, peerpool_mod.PeerPool())
    reader = RemoteReader(["http://node.test"], refresh=0, session=_Session(app.test_client()))
    mine = FakeIO()                                # what the light client sees
    mine.gas_price_value = GAS_PRICE
    client = create_light_app(reader, wallet, gas_io=mine).test_client()
    return type("L", (), dict(client=client, node=node, cs=cs, wallet=wallet, io=mine))


def mine_block(l, txs=()):
    blk = make_block(l.cs.height + 1, l.cs.tip["hash"], list(txs), builder_index=9)
    l.cs = l.cs.apply_block(blk)
    l.node.cs = l.cs
    l.node.view = NodeView(l.cs)
    return blk


def prepare(l):
    return l.client.get("/api/fees/prepare", query_string=dict(
        network="optimism", action="send", dest=DEST)).get_json()


def request(l):
    p = prepare(l)
    sig = evm.sign_message(p["message"].encode(), DEST_KEY)
    return submit(l.client, "/fees", follow=False, network="optimism", action="send", dest=DEST,
                  target=str(p["target"]), signature=sig, passphrase=PASS)


def banner(l, r):
    return l.client.get(r.headers["Location"]).get_data(as_text=True)


class TestPage:
    def test_the_light_client_has_the_fees_page_and_a_nav_link(self, light):
        html = light.client.get("/fees").get_data(as_text=True)
        assert 'value="optimism"' in html and 'href="/fees"' in html

    def test_the_plan_is_worked_out_locally(self, light):
        p = light.client.get("/api/fees/plan", query_string=dict(
            network="optimism", action="send", dest=DEST)).get_json()
        assert p["ok"] and p["target"] == int(65_000 * GAS_PRICE * gas.GAS_SAFETY)

    def test_the_message_uses_the_wallets_own_nonce(self, light):
        p = prepare(light)
        assert p["message"] == gas.request_message(
            "optimism", p["target"], DEST, light.wallet.addr, 1).decode()

    def test_a_sanctioned_address_is_refused_here_too(self, light):
        light.io.listed = {DEST}
        r = light.client.get("/api/fees/plan", query_string=dict(
            network="optimism", action="send", dest=DEST))
        assert r.get_json()["error"] == fees_ui.REFUSED


class TestRequest:
    def test_a_signed_request_goes_through_the_node_and_to_its_status_page(self, light):
        r = request(light)
        assert r.status_code == 303 and r.headers["Location"].startswith("/fees/")
        txid = r.headers["Location"].rsplit("/", 1)[1]
        t = light.node.mempool.get(txid)
        assert t["outputs"] == [{"to": ESC, "amount": fees_ui.LOCK}] and t["from"] == light.wallet.addr
        assert gas.verify_request_signature(gas.parse_request_memo(t["memo"]), t["from"], t["nonce"])

    def test_a_wrong_signature_is_refused_before_anything_is_sent(self, light):
        p = prepare(light)
        sig = evm.sign_message(b"other text", DEST_KEY)
        r = submit(light.client, "/fees", follow=False, network="optimism", action="send", dest=DEST,
                   target=str(p["target"]), signature=sig, passphrase=PASS)
        assert "does not match" in banner(light, r) and light.node.mempool.all_txs() == []

    def test_a_wrong_passphrase_sends_nothing(self, light):
        p = prepare(light)
        sig = evm.sign_message(p["message"].encode(), DEST_KEY)
        submit(light.client, "/fees", network="optimism", action="send", dest=DEST,
               target=str(p["target"]), signature=sig, passphrase="wrong")
        assert light.node.mempool.all_txs() == []


class TestStatus:
    def test_the_status_page_follows_the_request_from_the_mempool_to_the_chain(self, light):
        txid = request(light).headers["Location"].rsplit("/", 1)[1]
        page = light.client.get(f"/fees/{txid}").get_data(as_text=True)
        assert 'id="gas-card"' in page and f"/api/gas/{txid}" in page
        assert light.client.get(f"/api/gas/{txid}").get_json() == {"stage": "pending"}
        mine_block(light, [light.node.mempool.get(txid)])
        s = light.client.get(f"/api/gas/{txid}").get_json()
        assert s["stage"] == "collecting" and s["dest"] == DEST and s["close"] == 1 + gaslock.CLAIM_WINDOW_BLOCKS

    def test_a_made_up_hash_is_not_found(self, light):
        assert light.client.get("/fees/not-a-hash").status_code == 404
        assert light.client.get("/api/gas/" + "ab" * 32).get_json() == {"stage": "none"}
