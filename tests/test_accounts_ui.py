"""The Addresses page, and choosing which of the wallet's addresses a send
signs as, against a real recovery-phrase wallet."""

import re
import types

import pytest

import api
import crypto
import falcon_seeded
import peerpool as peerpool_mod
import shortkey
import wallet as wallet_mod
from chainstate import ChainState
from node import Node
from params import TICKS_PER_LAPSE
from tests.browser import submit, tokens
from tests.test_api import _FakeNode

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")

PASS = "accounts ui passphrase"
BOX = "mining box passphrase"
PHRASE = "abandon " * 11 + "about"
ADDR0 = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"
ADDR1 = "act.table.citizen.increase.olympic.valley.tell.island.scrub.print.sibling.east"


class WalletNode(_FakeNode):
    """The routes' view of a node that runs on a real PhraseWallet."""
    accounts = Node.accounts
    wallet_for = Node.wallet_for
    build_and_sign_tx = Node.build_and_sign_tx
    _build_and_sign_tx_with_kek = Node._build_and_sign_tx_with_kek

    def __init__(self, cs, w):
        super().__init__(cs, addr=w.addr)
        self.wallet = w
        self.pk_hex = w.pk_hex

    def submit_tx_from_api(self, t, timeout=5):
        ok, why = self.mempool.add(t)
        return ok, (why if not ok else why)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w, kek, _ = wallet_mod.create_wallet(PASS, phrase=PHRASE)
    w.walletfile.new_account(kek)
    cs = ChainState.from_genesis()
    cs.state.credit(ADDR0, 100 * TICKS_PER_LAPSE)
    cs.state.credit(ADDR1, 50 * TICKS_PER_LAPSE)
    node = WalletNode(cs, w)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    return types.SimpleNamespace(node=node, client=client, wf=w.walletfile, cs=cs)


def page(client, path="/addresses"):
    return client.get(path).get_data(as_text=True)


# The page
def test_the_page_lists_every_address_with_its_balance(world):
    html = page(world.client)
    assert ADDR0 in html and ADDR1 in html
    assert "100 LAPSE" in html and "50 LAPSE" in html
    assert html.count("This node") >= 1


def test_the_nav_has_an_addresses_entry(world):
    assert 'href="/addresses"' in page(world.client)


def test_the_json_list_has_public_facts_only(world):
    data = world.client.get("/api/wallet/accounts").get_json()
    assert [(a["index"], a["address"], a["active"]) for a in data] == [
        (0, ADDR0, True), (1, ADDR1, False)]
    assert "phrase" not in str(data).lower()


def test_no_page_shows_the_recovery_phrase(world):
    for path in ("/addresses", "/send", "/api/wallet/accounts"):
        assert "abandon" not in page(world.client, path)


# Adding and naming
def test_new_address_needs_the_passphrase(world):
    html = submit(world.client, "/addresses/new", page="/addresses", passphrase="nope nope nope").get_data(as_text=True)
    assert "not this wallet" in html
    assert [a["index"] for a in world.wf.accounts] == [0, 1]


def test_new_address_adds_the_next_account(world):
    html = submit(world.client, "/addresses/new", page="/addresses", passphrase=PASS).get_data(as_text=True)
    assert "Added address 2" in html
    assert [a["index"] for a in world.wf.accounts] == [0, 1, 2]


def test_a_name_can_be_set(world):
    csrf, _ = tokens(world.client, "/addresses")
    world.client.post("/addresses/label", data={"csrf_token": csrf, "account": 1, "label": "shop"})
    assert world.wf.account(1)["label"] == "shop"
    assert "shop" in page(world.client)


# Exporting
def test_export_shows_the_short_key_once_and_it_opens_that_account(world):
    html = submit(world.client, "/addresses/export", page="/addresses", account=1,
                  passphrase=PASS, new_passphrase=BOX).get_data(as_text=True)
    key = re.search(r"lc1-[A-Za-z0-9_-]{114}", html).group(0)
    w, _ = wallet_mod.unlock(wallet_mod.find_source(key), BOX)
    assert w.addr == ADDR1
    assert "recovery phrase" in html            # says what it does not hold
    # a reload does not show it again
    assert "lc1-" not in page(world.client)


def test_export_refuses_the_wallets_own_passphrase_and_a_short_one(world):
    for bad in (PASS, "short"):
        html = submit(world.client, "/addresses/export", page="/addresses", account=0,
                      passphrase=PASS, new_passphrase=bad).get_data(as_text=True)
        assert "lc1-" not in html and "passphrase" in html.lower()


def test_export_with_the_wrong_wallet_passphrase_gives_nothing(world):
    html = submit(world.client, "/addresses/export", page="/addresses", account=0,
                  passphrase="not the one", new_passphrase=BOX).get_data(as_text=True)
    assert "lc1-" not in html and "not this wallet" in html


def test_a_form_works_only_once(world):
    csrf, form = tokens(world.client, "/addresses")
    data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS}
    world.client.post("/addresses/new", data=data)
    world.client.post("/addresses/new", data=data)
    assert len(world.wf.accounts) == 3


# Choosing the address a send signs as
def sent(world):
    return world.node.mempool.all_txs()


def test_send_offers_the_picker_and_defaults_to_the_nodes_address(world):
    html = page(world.client, "/send")
    assert 'name="account"' in html and 'value="0" data-addr="%s"' % ADDR0 in html
    assert re.search(r'<option value="0"[^>]*selected', html)


def test_a_send_signs_as_the_picked_address(world):
    submit(world.client, "/send", page="/send", passphrase=PASS, account=1,
           outputs=f"{ADDR0},1")
    (t,) = sent(world)
    assert t["from"] == ADDR1
    assert world.wf.last_account == 1


def test_the_picker_then_defaults_to_the_last_used_address(world):
    submit(world.client, "/send", page="/send", passphrase=PASS, account=1, outputs=f"{ADDR0},1")
    html = page(world.client, "/send")
    assert re.search(r'<option value="1"[^>]*selected', html)
    submit(world.client, "/send", page="/send", passphrase=PASS, outputs=f"{ADDR0},1")
    assert sent(world)[-1]["from"] == ADDR1          # no pick: the last one used


def test_a_number_that_is_not_in_the_wallet_signs_as_the_default(world):
    submit(world.client, "/send", page="/send", passphrase=PASS, account=99, outputs=f"{ADDR1},1")
    (t,) = sent(world)
    assert t["from"] == ADDR0


def test_the_fee_check_uses_the_picked_address(world):
    poor = world.client.get("/api/send/fee", query_string={
        "outputs": f"{ADDR0},{60 * TICKS_PER_LAPSE}", "account": 1}).get_json()
    rich = world.client.get("/api/send/fee", query_string={
        "outputs": f"{ADDR1},{60 * TICKS_PER_LAPSE}", "account": 0}).get_json()
    assert poor["ok"] is False and "Insufficient" in poor["reason"]
    assert rich["ok"] is True


def test_a_single_key_node_shows_no_picker_and_cannot_add_addresses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sk, pk = crypto.generate_keypair()
    crypto.save_key(str(tmp_path / "k.json"), sk, pk, PASS)
    w = wallet_mod.Wallet(str(tmp_path / "k.json"), pk)
    cs = ChainState.from_genesis()
    node = WalletNode(cs, w)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    assert 'name="account"' not in page(client, "/send")
    html = page(client)
    assert "single exported key" in html and "/addresses/new" not in html
    out = submit(client, "/addresses/new", page="/send", passphrase=PASS).get_data(as_text=True)
    assert "single key" in out


# The board
def test_the_board_offers_post_as_and_marks_every_address_as_mine(world):
    import tx as tx_mod
    html = page(world.client, "/board")
    assert 'id="board-account"' in html
    submit(world.client, "/board", passphrase=PASS, message="from the shop", account=1)
    (t,) = sent(world)
    assert t["from"] == ADDR1
    assert world.wf.last_account == 1
    # the pending post reads as mine (its Edit/Delete show) even though the node's own address is 0
    html = page(world.client, "/board")
    assert "from the shop" in html and re.search(r'rc-root mine[^>]*id="post-', html)
    assert re.search(r'<option value="1"[^>]*selected', html)


def test_a_vote_and_a_delete_sign_as_the_address_that_the_page_names(world):
    import tx as tx_mod
    submit(world.client, "/board", passphrase=PASS, message="first", account=1)
    ref = tx_mod.tx_hash(sent(world)[0])[:tx_mod.REPLY_REF_LEN]
    csrf, _ = tokens(world.client, "/board")
    r = world.client.post("/board/delete", data={"csrf_token": csrf, "ref": ref,
                                                 "passphrase": PASS, "account": 1})
    assert r.get_json()["ok"] is True
    assert sent(world)[-1]["from"] == ADDR1
    r = world.client.post("/board/vote", data={"csrf_token": csrf, "ref": ref, "dir": "+",
                                               "passphrase": PASS, "account": 0})
    assert r.get_json()["ok"] is True
    assert sent(world)[-1]["from"] == ADDR0


def test_the_board_fragment_follows_the_picked_address(world):
    a = world.client.get("/api/board/fragment", query_string={"account": 0})
    b = world.client.get("/api/board/fragment", query_string={"account": 1})
    assert a.headers["ETag"] != b.headers["ETag"]


def test_the_post_as_picker_uses_the_names_given_on_the_addresses_page(world):
    csrf, _ = tokens(world.client, "/addresses")
    world.client.post("/addresses/label", data={"csrf_token": csrf, "account": 1, "label": "shop"})
    html = page(world.client, "/board")
    assert re.search(r'<option value="1"[^>]*>shop</option>', html)
    assert re.search(r'<option value="0"[^>]*>Address 0</option>', html)      # unnamed: its number


def test_a_post_by_one_of_my_addresses_shows_the_name_i_gave_it(world):
    csrf, _ = tokens(world.client, "/addresses")
    world.client.post("/addresses/label", data={"csrf_token": csrf, "account": 1, "label": "shop"})
    submit(world.client, "/board", passphrase=PASS, message="from the shop", account=1)
    html = page(world.client, "/board")
    assert re.search(r'class="rc-username[^"]*"[^>]*>shop</a>', html)


def test_a_single_address_wallet_shows_no_made_up_name_on_its_own_posts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w, kek, _ = wallet_mod.create_wallet(PASS, phrase=PHRASE)
    cs = ChainState.from_genesis()
    cs.state.credit(ADDR0, 100 * TICKS_PER_LAPSE)
    node = WalletNode(cs, w)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    submit(client, "/board", passphrase=PASS, message="solo")
    html = page(client, "/board")
    assert "Address 0" not in html and 'id="board-account"' not in html
