"""Editing and deleting board posts.

Neither touches the chain: both are rules readers apply, the same way for
every client. An edit is itself a board post (it pays what a post pays and
counts as one), a delete is a small tagged transaction like a vote.
"""

import json
import os
import random
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import board_view as bv
import crypto
import local_reader
import mempool as mempool_mod
import peerpool as peerpool_mod
import tx as tx_mod
from board_view import (BOARD_MEMO_TAG, DELETE_TAG, MAX_POST_TEXT_BYTES, apply_splice,
                        build_board_edit, make_splice, parse_board_edit, resolve_board)
from chainstate import ChainState
from light_app import create_light_app
from params import TICKS_PER_LAPSE
from remote_reader import RemoteReader
from state import State
from tests.fixtures import address, make_tx, seed_balance
from tests.browser import submit
from tests.test_light import PASS, _FullNode, _Session
from wallet import Wallet

ALICE, BOB = "alice-addr", "bob-addr"


def ev(author, memo, hash_, pending=False, height=1):
    """An event as board_view._board_events yields it, with the hash chosen
    so a test can say which post a reference means."""
    return {"height": None if pending else height, "ts": 1000, "hash": hash_,
            "tx": {"from": author, "memo": memo, "nonce": 1}, "pending": pending}


def post(author, text, hash_, **kw):
    return ev(author, BOARD_MEMO_TAG + text, hash_, **kw)


def edit(author, ref, pos, ndel, inserted, hash_, **kw):
    return post(author, build_board_edit(ref, pos, ndel, inserted), hash_, **kw)


def delete(author, ref, hash_, **kw):
    return ev(author, DELETE_TAG + ref, hash_, **kw)


A1 = "aaaaaa" + "0" * 58          # a post's hash; its reference is "aaaaaa"
A2 = "bbbbbb" + "0" * 58


# ---------------------------------------------------------------------------
# The splice
# ---------------------------------------------------------------------------

class TestSplice:
    def test_header_round_trips(self):
        text = build_board_edit("abc123", 5, 3, "new words")
        assert text == "[e:abc123:5:3]new words"
        assert parse_board_edit(text) == ("abc123", 5, 3, "new words")

    @pytest.mark.parametrize("text", ["plain", "[e:abc12:1:1]x", "[e:ABCDEF:1:1]x",
                                      "[e:abc123:1]x", "x[e:abc123:1:1]", "[e:abc123:-1:1]x"])
    def test_anything_else_is_not_an_edit(self, text):
        assert parse_board_edit(text) is None

    def test_a_typo_fix_sends_a_few_characters(self):
        orig = "the quick brown fox jumps over the lazy dog, and then it sleeps"
        new = orig.replace("quick", "quack")
        pos, ndel, ins = make_splice(orig, new)
        assert (pos, ndel, ins) == (orig.index("quick") + 2, 1, "a")
        assert len(build_board_edit("abc123", pos, ndel, ins)) < 25

    @pytest.mark.parametrize("orig,new", [
        ("hello", "hello world"), ("hello world", "hello"), ("abc", "xyz"),
        ("aaaa", "aaaaa"), ("aaaaa", "aaaa"), ("abab", "ab"), ("x", "xx"),
        ("same", "same2same"), ("\U0001F680 go", "\U0001F680 stop"),
    ])
    def test_splice_turns_one_text_into_the_other(self, orig, new):
        assert apply_splice(orig, *make_splice(orig, new)) == new

    def test_splice_holds_for_random_texts(self):
        rng = random.Random(7)
        alphabet = "ab é\U0001F680"
        for _ in range(500):
            orig = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
            new = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
            if not new.strip():
                continue
            assert apply_splice(orig, *make_splice(orig, new)) == new

    def test_a_splice_past_the_end_does_not_apply(self):
        assert apply_splice("short", 6, 0, "x") is None
        assert apply_splice("short", 3, 5, "x") is None

    def test_a_splice_that_empties_the_post_does_not_apply(self):
        assert apply_splice("short", 0, 5, "") is None
        assert apply_splice("short", 0, 5, "   ") is None

    def test_a_post_cannot_be_edited_past_the_size_of_a_post(self):
        text = "x" * MAX_POST_TEXT_BYTES
        assert apply_splice(text, 0, 0, "y") is None
        assert apply_splice(text, 0, 1, "y") == "y" + "x" * (MAX_POST_TEXT_BYTES - 1)

    def test_the_cap_counts_bytes_not_characters(self):
        text = "x" * (MAX_POST_TEXT_BYTES - 1)
        assert apply_splice(text, 0, 0, "é") is None      # two bytes


# ---------------------------------------------------------------------------
# What readers make of the events
# ---------------------------------------------------------------------------

class TestResolve:
    def test_an_edit_changes_its_posts_text_and_is_not_a_post_itself(self):
        out = resolve_board([post(ALICE, "hello wrold", A1),
                             edit(ALICE, "aaaaaa", 7, 2, "or", A2)])
        assert [(p["text"], p["edited"]) for p in out] == [("hello world", True)]

    def test_edits_apply_in_order_each_to_what_the_last_left(self):
        out = resolve_board([post(ALICE, "one", A1),
                             edit(ALICE, "aaaaaa", 3, 0, " two", "c" * 64),
                             edit(ALICE, "aaaaaa", 7, 0, " three", "d" * 64)])
        assert out[0]["text"] == "one two three"

    def test_only_the_author_can_edit(self):
        out = resolve_board([post(ALICE, "mine", A1),
                             edit(BOB, "aaaaaa", 0, 4, "bob's", A2)])
        assert out[0]["text"] == "mine" and not out[0]["edited"]
        # Bob's attempt is not hidden: it is the ordinary post it also is.
        assert len(out) == 2 and out[1]["from"] == BOB
        assert out[1]["text"] == "[e:aaaaaa:0:4]bob's"

    def test_an_edit_cannot_reach_a_post_that_comes_after_it(self):
        out = resolve_board([edit(ALICE, "aaaaaa", 0, 0, "x", A2),
                             post(ALICE, "later", A1)])
        assert [p["text"] for p in out] == ["[e:aaaaaa:0:0]x", "later"]

    def test_an_edit_that_does_not_fit_is_shown_as_a_post(self):
        out = resolve_board([post(ALICE, "short", A1),
                             edit(ALICE, "aaaaaa", 99, 0, "x", A2)])
        assert out[0]["text"] == "short" and len(out) == 2

    def test_an_edit_of_nothing_is_shown_as_a_post(self):
        out = resolve_board([edit(ALICE, "ffffff", 0, 0, "x", A2)])
        assert len(out) == 1

    def test_a_reference_means_the_senders_most_recent_post_with_it(self):
        out = resolve_board([post(ALICE, "first", A1),
                             post(ALICE, "second", "aaaaaa" + "1" * 58),
                             edit(ALICE, "aaaaaa", 0, 6, "SECOND", "e" * 64)])
        assert [p["text"] for p in out] == ["first", "SECOND"]

    def test_a_reference_is_looked_up_among_the_senders_own_posts_only(self):
        out = resolve_board([post(BOB, "bob's", A1),
                             post(ALICE, "alice's", "aaaaaa" + "1" * 58),
                             edit(ALICE, "aaaaaa", 0, 7, "ALICE'S", "e" * 64)])
        assert [p["text"] for p in out] == ["bob's", "ALICE'S"]

    def test_an_edit_keeps_the_posts_reference(self):
        out = resolve_board([post(ALICE, "text", A1), edit(ALICE, "aaaaaa", 0, 4, "new", A2)])
        assert out[0]["ref6"] == "aaaaaa"

    def test_a_delete_empties_the_post_and_keeps_its_place(self):
        out = resolve_board([post(ALICE, "regret", A1), delete(ALICE, "aaaaaa", A2)])
        assert len(out) == 1 and out[0]["deleted"] and out[0]["text"] == ""

    def test_only_the_author_can_delete(self):
        out = resolve_board([post(ALICE, "mine", A1), delete(BOB, "aaaaaa", A2)])
        assert not out[0]["deleted"] and out[0]["text"] == "mine"

    def test_deleting_what_does_not_exist_changes_nothing(self):
        out = resolve_board([post(ALICE, "mine", A1), delete(ALICE, "ffffff", A2)])
        assert not out[0]["deleted"]

    def test_an_edit_after_a_delete_does_nothing_and_is_not_shown(self):
        out = resolve_board([post(ALICE, "regret", A1), delete(ALICE, "aaaaaa", A2),
                             edit(ALICE, "aaaaaa", 0, 0, "x", "c" * 64)])
        assert len(out) == 1 and out[0]["deleted"] and out[0]["text"] == ""

    def test_an_edit_before_a_delete_is_overtaken_by_it(self):
        out = resolve_board([post(ALICE, "regret", A1),
                             edit(ALICE, "aaaaaa", 0, 6, "more regret", A2),
                             delete(ALICE, "aaaaaa", "c" * 64)])
        assert out[0]["deleted"] and out[0]["text"] == ""

    def test_pending_edits_and_deletes_count_like_confirmed_ones(self):
        out = resolve_board([post(ALICE, "one", A1),
                             edit(ALICE, "aaaaaa", 3, 0, "!", A2, pending=True)])
        assert out[0]["text"] == "one!"
        out = resolve_board([post(ALICE, "one", A1),
                             delete(ALICE, "aaaaaa", A2, pending=True)])
        assert out[0]["deleted"]

    def test_an_edit_carries_a_profile_header_like_any_post(self):
        events = [post(ALICE, "text", A1),
                  post(ALICE, "[p:3:Al]" + build_board_edit("aaaaaa", 0, 4, "new"), A2)]
        assert resolve_board(events)[0]["text"] == "new"


# ---------------------------------------------------------------------------
# Through the board, with a real chain
# ---------------------------------------------------------------------------

def _chain_with(*txs):
    cs = ChainState.from_genesis()
    for i, t in enumerate(txs, 1):
        cs.chain.append({"height": i, "timestamp": 1000 + i, "transactions": [t],
                         "hash": f"h{i}"})
    return cs


def _tx(author_index, memo, nonce, burn=True):
    state = State()
    seed_balance(state, author_index, 1000.0)
    return make_tx(author_index, 1, TICKS_PER_LAPSE, state, fee=100, nonce_override=nonce,
                   memo=memo, outputs_override=[{"to": crypto.burn_address(), "amount": 1}])


class TestSnapshot:
    def _board(self, cs, mempool=None):
        mem = mempool or mempool_mod.Mempool()
        snap = bv.build_board_snapshot(cs.chain, cs.state.nicknames, mem)
        return bv.board_page_data(snap, 1)

    def test_the_page_shows_the_edited_text_and_the_edit_is_not_a_row(self):
        root = _tx(0, BOARD_MEMO_TAG + "helo", 1)
        ref = tx_mod.tx_hash(root)[:6]
        fix = _tx(0, BOARD_MEMO_TAG + build_board_edit(ref, 3, 0, "l"), 2)
        data = self._board(_chain_with(root, fix))
        assert [(r["text"], r["edited"]) for r in data["rows"]] == [("hello", True)]

    def test_an_edit_counts_as_a_post_a_delete_does_not(self):
        root = _tx(0, BOARD_MEMO_TAG + "helo", 1)
        ref = tx_mod.tx_hash(root)[:6]
        fix = _tx(0, BOARD_MEMO_TAG + build_board_edit(ref, 3, 0, "l"), 2)
        gone = _tx(0, DELETE_TAG + ref, 3)
        assert self._board(_chain_with(root, fix))["post_count"] == 2
        assert self._board(_chain_with(root, fix, gone))["post_count"] == 2

    def test_under_the_consensus_rules_an_edit_is_a_board_post_and_a_delete_is_not(self):
        edit_tx = _tx(0, BOARD_MEMO_TAG + build_board_edit("abc123", 0, 0, "x"), 1)
        del_tx = _tx(0, DELETE_TAG + "abc123", 2)
        assert tx_mod.is_board_post(edit_tx) and not tx_mod.is_board_post(del_tx)
        state = State()
        seed_balance(state, 0, 1000.0)
        before = state.total_board_posts
        state.apply_tx(edit_tx)
        assert state.total_board_posts == before + 1
        state.apply_tx(del_tx)
        assert state.total_board_posts == before + 1

    def test_an_edit_pays_the_board_fee_floor(self):
        """It is a board post, so the node enforces the floor on it."""
        state = State()
        seed_balance(state, 0, 1000.0)
        state.total_board_posts = 250 * 5        # five steps up: the floor is 3**5 = 243 ticks
        cheap = _tx(0, BOARD_MEMO_TAG + build_board_edit("abc123", 0, 0, "x"), 1)
        ok, err = tx_mod.validate(cheap, state)
        assert not ok and "fee" in err.lower()

    def test_a_delete_does_not_pay_it(self):
        state = State()
        seed_balance(state, 0, 1000.0)
        state.total_board_posts = 250 * 5        # a floor a fee of 100 ticks would not clear
        ok, err = tx_mod.validate(_tx(0, DELETE_TAG + "abc123", 1), state)
        assert ok, err

    def test_a_reply_to_a_deleted_post_says_so(self):
        root = _tx(0, BOARD_MEMO_TAG + "gone soon", 1)
        ref = tx_mod.tx_hash(root)[:6]
        reply = _tx(1, BOARD_MEMO_TAG + f"[r:{ref}]a reply", 1)
        gone = _tx(0, DELETE_TAG + ref, 2)
        data = self._board(_chain_with(root, reply, gone))
        assert data["targets"][ref]["deleted"] is True
        assert data["targets"][ref]["text"] == ""

    def test_a_pending_edit_shows_at_once(self):
        root = _tx(0, BOARD_MEMO_TAG + "helo", 1)
        ref = tx_mod.tx_hash(root)[:6]
        mem = mempool_mod.Mempool()
        mem.add(_tx(0, BOARD_MEMO_TAG + build_board_edit(ref, 3, 0, "l"), 2))
        data = self._board(_chain_with(root), mem)
        assert [r["text"] for r in data["rows"]] == ["hello"]


# ---------------------------------------------------------------------------
# The pages: full node and light client alike
# ---------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    """A funded wallet that has one post on the board, and Bob's."""
    sk, pk = crypto.generate_keypair()
    keyfile = str(tmp_path / "wallet.key")
    crypto.save_key(keyfile, sk, pk, PASS)
    wallet = Wallet(keyfile, pk)
    cs = ChainState.from_genesis()
    cs.state.credit(wallet.addr, 1000 * TICKS_PER_LAPSE)
    mine = tx_mod.create(wallet.addr, wallet.pk_hex,
                         [{"to": crypto.burn_address(), "amount": 1}], 1, 100, sk,
                         memo=BOARD_MEMO_TAG + "my frist post")
    theirs = {"from": address(0), "nonce": 1, "fee": 100,
              "outputs": [{"to": crypto.burn_address(), "amount": 1}],
              "memo": BOARD_MEMO_TAG + "bob was here"}
    cs.chain.append({"height": 1, "timestamp": int(time.time()),
                     "transactions": [mine, theirs], "hash": "h1"})
    cs.state.apply_tx(mine)
    node = _FullNode(cs)
    session = _Session(api.create_app(node, peerpool_mod.PeerPool()).test_client())
    reader = RemoteReader(["http://node.test"], refresh=0, session=session)
    app = create_light_app(reader, wallet)
    return type("World", (), dict(wallet=wallet, cs=cs, node=node, app=app,
                                  client=app.test_client(), reader=reader,
                                  mine_ref=tx_mod.tx_hash(mine)[:6],
                                  their_ref=tx_mod.tx_hash(theirs)[:6]))


def _csrf(world):
    return re.search(r'name="csrf_token" value="([^"]+)"',
                     world.client.get("/board").get_data(as_text=True)).group(1)


class TestPages:
    def test_you_can_edit_and_delete_your_own_posts_only(self, world):
        html = world.client.get("/api/board/fragment?page=1").get_data(as_text=True)
        post_blocks = html.split('class="rc-root')
        own = [b for b in post_blocks if "my frist post" in b][0]
        other = [b for b in post_blocks if "bob was here" in b][0]
        assert "rc-editBtn" in own and "rc-deleteBtn" in own
        assert "rc-editBtn" not in other and "rc-deleteBtn" not in other

    def test_editing_posts_a_splice_the_node_accepts(self, world):
        resp = submit(world.client, "/board", passphrase=PASS,
                      edit_ref=world.mine_ref, edit_orig="my frist post",
                      message="my first post")
        (pending,) = world.node.mempool.all_txs()
        assert pending["memo"] == BOARD_MEMO_TAG + f"[e:{world.mine_ref}:4:2]ir"
        assert tx_mod.is_board_post(pending)
        html = resp.get_data(as_text=True)
        assert "my first post" in html and "my frist post" not in html
        assert "(edited)" in html

    def test_an_edit_costs_less_than_the_post_it_fixes(self, world):
        long_text = "word " * 36
        full = world.client.get("/api/board/fee?bytes=%d" % len(long_text)).get_json()
        cheap = world.client.get("/api/board/fee", query_string={
            "edit_ref": world.mine_ref, "orig": long_text, "new": long_text + "!"}).get_json()
        assert cheap["ok"] and cheap["fee"] < full["fee"]

    @pytest.mark.parametrize("fields,why", [
        ({"edit_orig": "my frist post", "message": "my frist post"}, "Nothing changed"),
        ({"edit_orig": "my frist post", "message": "   "}, "Write something"),
        ({"edit_orig": "my frist post", "message": "x" * 300}, "too long"),
    ])
    def test_a_bad_edit_is_refused_before_anything_is_signed(self, world, fields, why):
        resp = submit(world.client, "/board", passphrase=PASS,
                      edit_ref=world.mine_ref, **fields)
        assert why in resp.get_data(as_text=True)
        assert world.node.mempool.all_txs() == []

    def test_an_edit_with_a_bad_reference_is_refused(self, world):
        resp = submit(world.client, "/board", passphrase=PASS, edit_ref="nope",
                      edit_orig="a", message="b")
        assert "Bad edit request" in resp.get_data(as_text=True)
        assert world.node.mempool.all_txs() == []

    def test_an_edit_cannot_also_be_a_reply(self, world):
        submit(world.client, "/board", passphrase=PASS,
               edit_ref=world.mine_ref, edit_orig="my frist post",
               message="my first post", reply_ref=world.their_ref)
        (pending,) = world.node.mempool.all_txs()
        assert "[r:" not in pending["memo"]

    def test_deleting_hides_the_post_and_leaves_a_tombstone(self, world):
        resp = world.client.post("/board/delete", data={
            "csrf_token": _csrf(world), "passphrase": PASS, "ref": world.mine_ref})
        (pending,) = world.node.mempool.all_txs()
        assert pending["memo"] == DELETE_TAG + world.mine_ref
        assert not tx_mod.is_board_post(pending)
        html = resp.get_data(as_text=True)
        assert "my frist post" not in html and "Deleted by its author." in html
        assert "bob was here" in html

    def test_the_board_tab_stays_lit_after_posting_deleting_or_voting(self, world):
        for path, data in (("/board", {"message": "hi"}),
                           ("/board/delete", {"ref": world.mine_ref}),
                           ("/board/vote", {"ref": world.their_ref, "dir": "+"})):
            html = submit(world.client, path, page="/board", passphrase=PASS,
                          **data).get_data(as_text=True)
            assert '<a href="/board" class="active">' in html, path

    def test_a_deleted_post_offers_no_buttons(self, world):
        world.client.post("/board/delete", data={
            "csrf_token": _csrf(world), "passphrase": PASS, "ref": world.mine_ref})
        html = world.client.get("/api/board/fragment?page=1").get_data(as_text=True)
        own = [b for b in html.split('class="rc-root') if "Deleted by its author." in b][0]
        for button in ("rc-editBtn", "rc-deleteBtn", "rc-replyBtn", "rc-voteButton",
                       "rc-votes "):
            assert button not in own

    def test_delete_is_priced_like_a_vote_not_like_a_post(self, world):
        quote = world.client.get("/api/board/delete_fee",
                                 query_string={"ref": world.mine_ref}).get_json()
        post = world.client.get("/api/board/fee?bytes=20").get_json()
        assert quote["ok"] and quote["fee"] <= post["fee"]

    def test_delete_needs_the_csrf_token_and_a_real_reference(self, world):
        world.client.post("/board/delete", data={"passphrase": PASS, "ref": world.mine_ref})
        world.client.post("/board/delete", data={
            "csrf_token": _csrf(world), "passphrase": PASS, "ref": "zz"})
        assert world.node.mempool.all_txs() == []

    def test_the_node_accepts_a_delete_of_a_post_that_is_not_yours_and_readers_ignore_it(self, world):
        world.client.post("/board/delete", data={
            "csrf_token": _csrf(world), "passphrase": PASS, "ref": world.their_ref})
        assert len(world.node.mempool.all_txs()) == 1
        html = world.client.get("/api/board/fragment?page=1").get_data(as_text=True)
        assert "bob was here" in html and "Deleted by its author." not in html

    def test_the_board_the_node_serves_has_the_edit_applied(self, world):
        submit(world.client, "/board", passphrase=PASS,
               edit_ref=world.mine_ref, edit_orig="my frist post",
               message="my first post")
        data = world.reader.board_page(1)
        assert sorted(r["text"] for r in data["rows"]) == ["bob was here", "my first post"]
        assert [r["edited"] for r in data["rows"] if r["text"] == "my first post"] == [True]


class TestFullNodeToo:
    """The full node's own private app has the same buttons and routes, from
    the same code."""

    def test_private_app_deletes_through_the_same_handler(self, tmp_path):
        sk, pk = crypto.generate_keypair()
        keyfile = str(tmp_path / "node.key")
        crypto.save_key(keyfile, sk, pk, PASS)
        addr = crypto.public_key_to_address(pk)
        cs = ChainState.from_genesis()
        cs.state.credit(addr, 1000 * TICKS_PER_LAPSE)
        mine = tx_mod.create(addr, pk.hex(), [{"to": crypto.burn_address(), "amount": 1}],
                             1, 100, sk, memo=BOARD_MEMO_TAG + "full node post")
        cs.chain.append({"height": 1, "timestamp": 1000, "transactions": [mine], "hash": "h1"})
        cs.state.apply_tx(mine)

        class SigningNode(_FullNode):
            def __init__(self):
                super().__init__(cs)
                self.addr, self.pk_hex, self.keyfile = addr, pk.hex(), keyfile

            def build_and_sign_tx(self, outs, fee=0, passphrase=None, memo=""):
                kek = crypto.derive_kek(self.keyfile, passphrase)
                nonce = max(cs.state.get_nonce(addr), self.mempool.pending_nonce(addr)) + 1
                s = crypto.decrypt_secret_key(self.keyfile, kek=kek)
                return tx_mod.create(addr, self.pk_hex, outs, nonce, fee, s, memo=memo), fee

        node = SigningNode()
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"',
                          client.get("/board").get_data(as_text=True)).group(1)
        ref = tx_mod.tx_hash(mine)[:6]
        html = client.post("/board/delete", data={"csrf_token": token, "passphrase": PASS,
                                                  "ref": ref}).get_data(as_text=True)
        assert [t["memo"] for t in node.mempool.all_txs()] == [DELETE_TAG + ref]
        assert "Deleted by its author." in html and "full node post" not in html

    def test_public_app_shows_the_result_but_no_buttons(self):
        cs = ChainState.from_genesis()
        root = _tx(0, BOARD_MEMO_TAG + "helo", 1)
        ref = tx_mod.tx_hash(root)[:6]
        fix = _tx(0, BOARD_MEMO_TAG + build_board_edit(ref, 3, 0, "l"), 2)
        cs.chain += [{"height": 1, "timestamp": 1, "transactions": [root], "hash": "h1"},
                     {"height": 2, "timestamp": 2, "transactions": [fix], "hash": "h2"}]
        client = api.create_app(_FullNode(cs), peerpool_mod.PeerPool()).test_client()
        html = client.get("/api/board/fragment?page=1").get_data(as_text=True)
        assert "hello" in html and "(edited)" in html
        assert "rc-editBtn" not in html and "rc-deleteBtn" not in html
