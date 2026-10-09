"""The Fees page and the status of a request, end to end through the app."""

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
import gas_status
import gaslock
import peerpool as peerpool_mod
import relay
import tx as tx_mod
from chainstate import ChainState
from node import NodeView
from params import TICKS_PER_LAPSE
from tests.browser import submit, tokens
from tests.fixtures import address, make_block, make_tx
from tests.test_gas_worker import (ASKER, DEST, ESC, FakeIO, ME, NET, PRICE, TARGET,
                                   World)
from tests.test_light import PASS, _FullNode

GAS_PRICE = 10 ** 10      # 10 gwei: a send needs 9.75e14 wei, over a node's ceiling


class AskerNode(_FullNode):
    """A full node whose own key signs, the way the private app expects."""

    def __init__(self, cs, keyfile, sk, pk):
        super().__init__(cs)
        self.keyfile = keyfile
        self.pk_hex = pk.hex()
        self.addr = crypto.public_key_to_address(pk)
        self.gas_io = FakeIO()
        self.storage.get_tx_height = lambda txid: next(
            (b["height"] for b in cs.chain for t in b.get("transactions", [])
             if tx_mod.tx_hash(t) == txid), None)

    def build_and_sign_tx(self, outs, fee=0, passphrase=None, memo=""):
        kek = crypto.derive_kek(self.keyfile, passphrase)
        nonce = max(self.cs.state.get_nonce(self.addr), self.mempool.pending_nonce(self.addr)) + 1
        s = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        return tx_mod.create(self.addr, self.pk_hex, outs, nonce, fee, s, memo=memo), fee


@pytest.fixture
def app(tmp_path, monkeypatch):
    sk, pk = crypto.generate_keypair()
    keyfile = str(tmp_path / "node.key")
    crypto.save_key(keyfile, sk, pk, PASS)
    cs = ChainState.from_genesis()
    node = AskerNode(cs, keyfile, sk, pk)
    cs.state.credit(node.addr, 1000 * TICKS_PER_LAPSE)
    node.view = NodeView(cs)                  # the pages read the published view
    node.gas_io.gas_price_value = GAS_PRICE
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    return type("A", (), dict(node=node, client=client, io=node.gas_io))


def post(app, passphrase=PASS, **kw):
    fields = dict(network="optimism", action="send", dest=DEST, passphrase=passphrase)
    fields.update(kw)
    return submit(app.client, "/fees", page="/fees", follow=False, **fields)


def follow(app, response):
    return app.client.get(response.headers["Location"]).get_data(as_text=True)


class TestPage:
    def test_it_lists_networks_and_actions(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        for n in gas.NETWORKS.values():
            assert f'value="{n.slug}"' in html
        for a in gas.ACTIONS.values():
            assert f'value="{a.key}"' in html
        assert 'href="/fees"' in html

    def test_one_button_says_what_it_does_and_nothing_asks_to_be_paid(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        assert html.count('type="submit"') == 1
        assert "Burn 10 LAPSE and request" in html
        assert 'id="sign-btn"' not in html and "Pay " not in html and "pay " not in html.split("<script>")[0]

    def test_nothing_asks_the_user_to_sign_anything(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        assert html.count("<button") == 1
        for word in ("personal_sign", "signMessage", "ethereum.request", "eth_requestAccounts", 'name="signature"'):
            assert word not in html

    def test_it_says_the_gift_is_as_is_and_when_the_burn_comes_back(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        assert "A free gift, as is. The 10 LAPSE comes back only if no node offers." in html

    def test_the_page_is_a_short_read(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        body = html.split("<h2>Fees</h2>", 1)[1].split("<script>", 1)[0]
        body = re.sub(r"<select.*?</select>|<details.*?</details>", " ", body, flags=re.S)   # the lists, not the copy
        words = re.sub(r"<[^>]+>", " ", body).split()
        assert len(words) < 60, len(words)

    def test_with_enough_lapse_there_is_no_hint_about_getting_some(self, app):
        html = app.client.get("/fees").get_data(as_text=True)
        assert "Mine some" not in html and "You need 10 LAPSE" not in html

    def test_without_enough_lapse_it_says_to_mine_or_use_discord(self, app):
        app.node.cs.state.debit(app.node.addr, 995 * TICKS_PER_LAPSE)
        app.node.view = NodeView(app.node.cs)
        html = app.client.get("/fees").get_data(as_text=True)
        assert "You need 10 LAPSE. Mine some, or get free LAPSE on" in html
        assert f'<a href="{fees_ui.DISCORD}" target="_blank"' in html and 'type="submit" id="submit-btn" style="flex:0 0 auto" disabled' in html

    def test_an_unreadable_balance_does_not_block_the_page(self, app, monkeypatch):
        monkeypatch.setattr(type(app.node.view.state), "get_balance",
                            lambda self, a: (_ for _ in ()).throw(RuntimeError("down")))
        assert app.client.get("/fees").status_code == 200

    def test_the_public_app_has_no_fees_page_but_has_the_status(self, app):
        public = api.create_app(app.node, peerpool_mod.PeerPool()).test_client()
        assert public.get("/fees").status_code == 404
        assert public.get("/api/gas/" + "ab" * 32).get_json() == {"stage": "none"}


class TestPlan:
    def get(self, app, **kw):
        q = dict(network="optimism", action="send", dest=DEST)
        q.update(kw)
        return app.client.get("/api/fees/plan", query_string=q)

    def test_a_plan_for_an_expensive_action_says_it_is_capped(self, app):
        p = self.get(app).get_json()
        assert p["ok"] and p["needs_help"]
        assert p["target"] == int(65_000 * GAS_PRICE * gas.GAS_SAFETY)
        assert 0 < p["covers_share"] < 1 and p["deliver_usd"] == pytest.approx(
            gas.payout_cap_usd(NET), rel=1e-6)
        assert p["lock"] == fees_ui.LOCK

    def test_a_cheap_action_is_covered_in_full(self, app, monkeypatch):
        app.io.gas_price_value = 10 ** 6
        p = self.get(app).get_json()
        assert p["covers_share"] == 1.0

    def test_an_address_that_already_has_enough_needs_nothing(self, app):
        app.io.dest = 10 ** 18
        p = self.get(app).get_json()
        assert p["ok"] and not p["needs_help"] and p["deliver"] == 0

    def test_solana_uses_fixed_amounts(self, app):
        p = self.get(app, network="solana", dest="DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK").get_json()
        assert p["target"] == gas.ACTIONS["send"].svm_lamports

    def test_without_an_address_it_still_prices_the_action(self, app):
        assert self.get(app, dest="").get_json()["ok"]

    @pytest.mark.parametrize("kw,message", [
        (dict(network="dogechain"), "Pick a network"),
        (dict(action="teleport"), "Pick what you want to do"),
        (dict(dest="0x12"), "not a valid Optimism address"),
    ])
    def test_bad_input_is_explained(self, app, kw, message):
        r = self.get(app, **kw)
        assert r.status_code == 400 and message in r.get_json()["error"]

    def test_an_unreachable_network_is_a_calm_message_not_a_crash(self, app, monkeypatch):
        def down(*a, **k):
            raise evm.EVMUnreachable("down")
        app.io.dest_balance = down
        r = self.get(app)
        assert r.status_code == 502 and "Try again" in r.get_json()["error"]

    def test_a_relay_failure_is_the_same(self, app, monkeypatch):
        def down(net):
            raise relay.RelayUnreachable("down")
        app.io.price = down
        assert self.get(app).status_code == 502


class TestSanctioned:
    def test_a_listed_address_is_refused_on_the_page(self, app):
        app.io.listed = {DEST}
        q = dict(network="optimism", action="send", dest=DEST)
        assert app.client.get("/api/fees/plan", query_string=q).get_json()["error"] == fees_ui.REFUSED

    def test_a_listed_address_cannot_be_submitted(self, app):
        app.io.listed = {DEST}
        r = post(app)
        assert fees_ui.REFUSED in follow(app, r) and app.node.mempool.all_txs() == []

    def test_an_oracle_outage_does_not_block_the_page(self, app):
        app.io.screening_down = True
        r = app.client.get("/api/fees/plan", query_string=dict(network="optimism", action="send", dest=DEST))
        assert r.get_json()["ok"]


class TestSubmit:
    def test_one_post_burns_the_lapse_and_goes_to_the_transaction_page(self, app):
        r = post(app)
        assert r.status_code == 303
        txid = r.headers["Location"].rsplit("/", 1)[1]
        t = app.node.mempool.get(txid)
        assert t and t["outputs"] == [{"to": ESC, "amount": fees_ui.LOCK}]
        req = gas.parse_request_memo(t["memo"])
        assert req["dest"] == DEST and req["network"] == "optimism"
        assert req["target"] == int(65_000 * GAS_PRICE * gas.GAS_SAFETY)
        assert gaslock.check_lock(t, 1)[0] and tx_mod.validate(t, app.node.cs.state)[0]

    def test_nothing_is_signed_by_anyone_but_the_sender(self, app):
        t = app.node.mempool.get(post(app).headers["Location"].rsplit("/", 1)[1])
        assert t["from"] == app.node.addr and "signature" not in t["memo"] and len(t["memo"].split()) == 4

    def test_a_funded_address_cannot_ask(self, app):
        app.io.dest = 10 ** 18
        assert "already holds enough" in follow(app, post(app))

    def test_wrong_passphrase_sends_nothing(self, app):
        post(app, passphrase="wrong")
        assert app.node.mempool.all_txs() == []

    def test_a_missing_passphrase_is_asked_for(self, app):
        assert "Passphrase required" in follow(app, post(app, passphrase=""))

    def test_not_enough_lapse_is_said_plainly(self, app):
        app.node.cs.state.debit(app.node.addr, 999 * TICKS_PER_LAPSE + 5 * 10 ** 7)
        app.node.view = NodeView(app.node.cs)
        assert "Not enough LAPSE" in follow(app, post(app))

    @pytest.mark.parametrize("kw,message", [
        (dict(network="dogechain"), "Pick a network"),
        (dict(action="teleport"), "Pick what you want to do"),
        (dict(dest=""), "Enter the address"),
        (dict(dest="0x12"), "not a valid Optimism address"),
    ])
    def test_bad_input_is_explained_and_nothing_is_sent(self, app, kw, message):
        r = post(app, **kw)
        assert message in follow(app, r) and app.node.mempool.all_txs() == []

    def test_an_unreachable_network_is_a_calm_message(self, app):
        def down(net, addr):
            raise evm.EVMUnreachable("down")
        app.io.dest_balance = down
        assert "Try again" in follow(app, post(app))

    def test_the_form_works_once(self, app):
        csrf, form = tokens(app.client, "/fees")
        data = dict(csrf_token=csrf, form_token=form, network="optimism", action="send",
                    dest=DEST, passphrase=PASS)
        first = app.client.post("/fees", data=data)
        again = app.client.post("/fees", data=data, follow_redirects=True)
        assert first.status_code == 303
        assert "already" in again.get_data(as_text=True).lower()
        assert len(app.node.mempool.all_txs()) == 1

    def test_a_bad_csrf_token_is_refused(self, app):
        r = app.client.post("/fees", data=dict(csrf_token="x", form_token="y", network="optimism"),
                            follow_redirects=True)
        assert "Session expired" in r.get_data(as_text=True)

    def test_bad_input_keeps_what_was_typed(self, app):
        assert DEST in follow(app, post(app, passphrase=""))


class TestStatus:
    """The chain-derived stages, each from a real chain of signed transactions."""

    def world(self, tmp_path):
        w = World(tmp_path)
        return w

    def status(self, w, txid, funded=False, claimer_funded=lambda b: True):
        return gas_status.request_status(w.chain, len(w.chain) - 1, txid, 1,
                                         lambda: funded, claimer_funded)

    def test_a_malformed_transaction_is_not_a_request(self, tmp_path):
        w = self.world(tmp_path)
        assert self.status(w, "ab" * 32) is None

    def test_collecting_while_the_window_is_open(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        s = self.status(w, txid)
        assert s["stage"] == "collecting" and s["blocks_left"] == gaslock.CLAIM_WINDOW_BLOCKS
        assert s["eta_seconds"] == gaslock.CLAIM_WINDOW_BLOCKS * gas_status.BLOCK_SECONDS
        assert s["claimers"] == 0 and s["network"] == "optimism" and s["dest"] == DEST

    def test_an_offer_shortens_the_wait_and_is_counted(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3, 4])
        s = self.status(w, txid)
        assert s["stage"] == "collecting" and s["claimers"] == 2 and s["blocks_left"] == 1

    def test_no_offers_means_the_lock_came_back(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.mine_until(1 + gaslock.CLAIM_WINDOW_BLOCKS)
        s = self.status(w, txid)
        assert s["stage"] == "unclaimed" and s["lock_outcome"] == "refunded"

    def test_after_the_window_a_node_is_paying_then_the_next(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3, 4, 5])
        r_close = 3
        w.mine_until(r_close)
        s = self.status(w, txid)
        assert s["stage"] == "paying" and s["current"]["rank"] == 1 and len(s["order"]) == 3
        assert s["lock_outcome"] == "burned" and s["ranks_left"] == 2
        w.mine_until(r_close + 1 + gas.RANK_SLOT_BLOCKS)
        s = self.status(w, txid)
        assert s["current"]["rank"] == 2 and s["ranks_left"] == 1
        assert s["order"][1]["id"] == s["current"]["id"]

    def test_funded_wins_whatever_the_slot(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3])
        w.mine_until(4)
        assert self.status(w, txid, funded=True)["stage"] == "funded"

    def test_everyone_having_had_a_turn_is_stated_honestly(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3, 4])
        w.mine_until(3 + 1 + 2 * gas.RANK_SLOT_BLOCKS)
        s = self.status(w, txid)
        assert s["stage"] == "gave_up" and s["lock_outcome"] == "burned"

    def test_claimers_who_cannot_pay_are_not_in_the_order(self, tmp_path):
        w = self.world(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3])
        w.mine_until(3)
        s = self.status(w, txid, claimer_funded=lambda b: False)
        assert s["stage"] == "no_eligible" and s["order"] == []

    def test_ids_do_not_show_whole_addresses(self, tmp_path):
        assert gas_status.short(address(3)).count(".") == 2


class TestStatusEndpoint:
    def node_for(self, w, mempool=None):
        from types import SimpleNamespace
        w.node.storage = SimpleNamespace(
            get_tx_height=lambda txid: w.heights.get(txid))
        w.node.mempool = SimpleNamespace(get=lambda txid: (mempool or {}).get(txid))
        return w.node

    def test_unknown_and_pending_and_chain(self, tmp_path):
        w = World(tmp_path)
        txid = w.request()
        w.heights = {txid: 1}
        w.io.dest = 0
        node = self.node_for(w)
        s = fees_ui.status_for(node, w.io, txid)
        assert s["stage"] == "collecting"
        assert fees_ui.status_for(node, w.io, "cd" * 32) == {"stage": "none"}
        # a request still in the mempool
        pending = make_tx(ASKER, 0, 0, None, fee=1000, nonce_override=9,
                          memo=gas.build_request_memo("optimism", 5, DEST),
                          outputs_override=[{"to": ESC, "amount": gaslock.MIN_LOCK}])
        w.heights = {}
        node = self.node_for(w, mempool={"p" * 64: pending})
        assert fees_ui.status_for(node, w.io, "p" * 64) == {"stage": "pending"}
        # a mempool transaction that is not a fee request
        other = make_tx(ASKER, 0, 1, None, fee=1000, nonce_override=10)
        node = self.node_for(w, mempool={"q" * 64: other})
        assert fees_ui.status_for(node, w.io, "q" * 64) == {"stage": "none"}

    def test_funded_is_read_from_the_destination_itself(self, tmp_path):
        w = World(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3])
        w.mine_until(4)
        w.heights = {txid: 1}
        node = self.node_for(w)
        w.io.dest = 0
        assert fees_ui.status_for(node, w.io, txid)["stage"] == "paying"
        w.io.dest = TARGET
        assert fees_ui.status_for(node, w.io, txid)["stage"] == "funded"

    def test_an_unreachable_destination_is_not_called_funded(self, tmp_path):
        w = World(tmp_path)
        txid = w.request()
        w.claim_together(txid, [3])
        w.mine_until(4)
        w.heights = {txid: 1}
        node = self.node_for(w)

        def down(net, addr):
            raise evm.EVMUnreachable("down")
        w.io.dest_balance = down
        assert fees_ui.status_for(node, w.io, txid)["stage"] == "paying"
