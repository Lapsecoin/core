"""A form posts once: reloading, going Back, double clicking or replaying
what the browser sent must not send a payment or a post twice."""

import os
import re
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import crypto
import forms
import peerpool as peerpool_mod
import tx as tx_mod
from chainstate import ChainState
from flask import Flask
from forms import ALREADY_SENT, Forms, Notes, OneTimeTokens, forms_for
from light_app import create_light_app
from params import TICKS_PER_LAPSE
from remote_reader import RemoteReader
from tests.browser import submit, tokens
from tests.fixtures import address
from tests.test_light import PASS, _FullNode, _Session, _TxIndex
from wallet import Wallet


# ---------------------------------------------------------------------------
# The two small pieces
# ---------------------------------------------------------------------------

class TestOneTimeTokens:
    def test_a_token_works_once(self):
        t = OneTimeTokens()
        token = t.issue()
        assert t.consume(token) is True
        assert t.consume(token) is False

    def test_a_token_never_issued_does_not_work(self):
        assert OneTimeTokens().consume("made-up") is False

    @pytest.mark.parametrize("junk", [None, 7, b"abc", ["x"], {"a": 1}, ""])
    def test_junk_does_not_work_and_does_not_raise(self, junk):
        assert OneTimeTokens().consume(junk) is False

    def test_tokens_are_not_guessable_or_repeated(self):
        t = OneTimeTokens()
        issued = {t.issue() for _ in range(500)}
        assert len(issued) == 500 and all(len(x) >= 20 for x in issued)

    def test_each_token_is_independent(self):
        t = OneTimeTokens()
        a, b = t.issue(), t.issue()
        assert t.consume(a) and t.consume(b)

    def test_an_expired_token_does_not_work(self, monkeypatch):
        t = OneTimeTokens(ttl=60)
        token = t.issue()
        now = time.monotonic()
        monkeypatch.setattr(forms.time, "monotonic", lambda: now + 61)
        assert t.consume(token) is False

    def test_memory_is_bounded_oldest_first(self):
        t = OneTimeTokens(limit=10)
        tokens_ = [t.issue() for _ in range(25)]
        assert len(t._live) <= 10
        assert t.consume(tokens_[0]) is False        # long gone
        assert t.consume(tokens_[-1]) is True

    def test_issuing_clears_out_the_expired(self, monkeypatch):
        t = OneTimeTokens(ttl=60)
        for _ in range(5):
            t.issue()
        now = time.monotonic()
        monkeypatch.setattr(forms.time, "monotonic", lambda: now + 61)
        t.issue()
        assert len(t._live) == 1

    def test_two_at_once_one_wins(self):
        t = OneTimeTokens()
        token = t.issue()
        wins = []
        gate = threading.Barrier(8)

        def go():
            gate.wait()
            wins.append(t.consume(token))
        threads = [threading.Thread(target=go) for _ in range(8)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        assert wins.count(True) == 1


class TestNotes:
    def test_handed_over_once(self):
        n = Notes()
        note_id = n.keep({"a": 1})
        assert n.take(note_id) == {"a": 1}
        assert n.take(note_id) == {}

    @pytest.mark.parametrize("junk", [None, 7, "nope", "", b"x"])
    def test_nothing_for_an_id_that_was_not_given(self, junk):
        assert Notes().take(junk) == {}

    def test_expires(self, monkeypatch):
        n = Notes(ttl=10)
        note_id = n.keep({"a": 1})
        now = time.monotonic()
        monkeypatch.setattr(forms.time, "monotonic", lambda: now + 11)
        assert n.take(note_id) == {}

    def test_memory_is_bounded(self):
        n = Notes(limit=5)
        ids = [n.keep({"i": i}) for i in range(20)]
        assert len(n._held) <= 5
        assert n.take(ids[0]) == {}
        assert n.take(ids[-1]) == {"i": 19}

    def test_ids_are_not_guessable(self):
        n = Notes()
        assert len({n.keep({}) for _ in range(300)}) == 300


class TestFormsForAnApp:
    def test_one_per_app_shared_by_every_route(self):
        app = Flask(__name__)
        assert forms_for(app) is forms_for(app)
        assert forms_for(app) is not forms_for(Flask(__name__))

    def test_done_is_a_303_to_the_page(self):
        app = Flask(__name__)
        with app.test_request_context():
            r = Forms().done("/board", {"x": 1}, page=2)
        assert r.status_code == 303
        assert r.headers["Location"].startswith("/board?page=2&note=")

    def test_done_with_nothing_to_say_carries_no_note(self):
        app = Flask(__name__)
        with app.test_request_context():
            assert Forms().done("/board").headers["Location"] == "/board"


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    sk, pk = crypto.generate_keypair()
    keyfile = str(tmp_path / "wallet.key")
    crypto.save_key(keyfile, sk, pk, PASS)
    wallet = Wallet(keyfile, pk)
    cs = ChainState.from_genesis()
    cs.state.credit(wallet.addr, 1000 * TICKS_PER_LAPSE)
    root = {"from": address(0), "nonce": 1, "fee": 100,
            "outputs": [{"to": crypto.burn_address(), "amount": 1}],
            "memo": tx_mod.BOARD_MEMO_TAG + "the root post"}
    cs.chain.append({"height": 1, "timestamp": int(time.time()),
                     "transactions": [root], "hash": "h1"})
    node = _FullNode(cs)
    app = api.create_app(node, peerpool_mod.PeerPool())
    session = _Session(app.test_client())
    reader = RemoteReader(["http://node.test"], refresh=0, session=session)
    light = create_light_app(reader, wallet)
    return type("W", (), dict(wallet=wallet, node=node, client=light.test_client(),
                              app=light, ref=tx_mod.tx_hash(root)[:6]))


def sent(world):
    return world.node.mempool.all_txs()


def banner(html):
    """The text of the error banner on a page, or None. (The page's own
    script mentions the same words, so look at the banner, not the page.)"""
    m = re.search(r'<div class="board-error"[^>]*?(?<!hidden)>(.*?)</div>', html, re.S)
    return m.group(1).strip() if m else None


class TestTheBugAsReported:
    """Wrote a reply, reloaded, the browser said 'resend form', and it sent
    the same reply again."""

    def test_a_post_is_answered_with_a_redirect_not_a_page(self, world):
        csrf, form = tokens(world.client, "/board")
        r = world.client.post("/board", data={"csrf_token": csrf, "form_token": form,
                                              "passphrase": PASS, "message": "hello"})
        assert r.status_code == 303 and r.headers["Location"] == "/board"
        assert b"Redirecting" in r.get_data() and b"board-compose" not in r.get_data()

    def test_reloading_after_it_does_not_send_it_again(self, world):
        submit(world.client, "/board", passphrase=PASS, message="reply", reply_ref=world.ref)
        assert len(sent(world)) == 1
        for _ in range(5):                        # reload, reload, reload
            assert world.client.get("/board").status_code == 200
        assert len(sent(world)) == 1

    def test_the_page_after_a_post_is_a_get_so_the_browser_has_nothing_to_resend(self, world):
        r = submit(world.client, "/board", follow=True, passphrase=PASS, message="x")
        assert r.history and r.history[0].status_code == 303
        assert r.request.method == "GET"

    def test_the_same_post_replayed_is_refused(self, world):
        csrf, form = tokens(world.client, "/board")
        data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS, "message": "once"}
        world.client.post("/board", data=data)
        again = world.client.post("/board", data=data, follow_redirects=True)
        assert len(sent(world)) == 1
        assert ALREADY_SENT in again.get_data(as_text=True)

    def test_a_refused_replay_does_not_put_the_text_back_to_be_sent_again(self, world):
        csrf, form = tokens(world.client, "/board")
        data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS, "message": "unique-text-xyz"}
        world.client.post("/board", data=data)
        html = world.client.post("/board", data=data, follow_redirects=True).get_data(as_text=True)
        compose = html[html.index('id="board-message"'):html.index("</textarea>")]
        assert "unique-text-xyz" not in compose

    def test_a_double_click_sends_one(self, world):
        csrf, form = tokens(world.client, "/board")
        data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS, "message": "double"}
        clients = [world.app.test_client() for _ in range(6)]
        gate = threading.Barrier(len(clients))

        def go(c):
            gate.wait()
            c.post("/board", data=data)
        threads = [threading.Thread(target=go, args=(c,)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(sent(world)) == 1

    def test_going_back_to_an_old_page_and_sending_again_is_refused(self, world):
        old_csrf, old_form = tokens(world.client, "/board")
        world.client.post("/board", data={"csrf_token": old_csrf, "form_token": old_form,
                                          "passphrase": PASS, "message": "first"})
        world.client.get("/board")                         # a newer page, a newer token
        r = world.client.post("/board", data={"csrf_token": old_csrf, "form_token": old_form,
                                              "passphrase": PASS, "message": "first"},
                              follow_redirects=True)
        assert len(sent(world)) == 1 and ALREADY_SENT in r.get_data(as_text=True)

    def test_two_tabs_each_send_once(self, world):
        a, b = tokens(world.client, "/board"), tokens(world.client, "/board")
        for csrf, form in (a, b):
            for _ in range(2):
                world.client.post("/board", data={"csrf_token": csrf, "form_token": form,
                                                  "passphrase": PASS, "message": f"from {form[:4]}"})
        assert len(sent(world)) == 2

    def test_a_post_with_no_token_is_refused(self, world):
        csrf, _ = tokens(world.client, "/board")
        r = world.client.post("/board", data={"csrf_token": csrf, "passphrase": PASS,
                                              "message": "no token"}, follow_redirects=True)
        assert sent(world) == [] and ALREADY_SENT in r.get_data(as_text=True)

    def test_a_forged_token_is_refused(self, world):
        csrf, _ = tokens(world.client, "/board")
        world.client.post("/board", data={"csrf_token": csrf, "form_token": "forged",
                                          "passphrase": PASS, "message": "x"})
        assert sent(world) == []

    def test_without_the_csrf_token_nothing_happens_and_the_form_token_is_not_burned(self, world):
        _, form = tokens(world.client, "/board")
        world.client.post("/board", data={"form_token": form, "passphrase": PASS, "message": "x"})
        assert sent(world) == []
        assert forms_for(world.app).tokens.consume(form) is True

    def test_a_token_is_not_good_on_a_restarted_app(self, world):
        csrf, form = tokens(world.client, "/board")
        world.app.extensions["forms"] = Forms()   # as after a restart
        r = world.client.post("/board", data={"csrf_token": csrf, "form_token": form,
                                              "passphrase": PASS, "message": "x"},
                              follow_redirects=True)
        assert sent(world) == [] and "restarted" in r.get_data(as_text=True)


class TestWhenSomethingIsWrong:
    def test_the_error_is_shown_once_and_the_text_kept(self, world):
        html = submit(world.client, "/board", passphrase=PASS,
                      message="y" * 300).get_data(as_text=True)
        assert "too long" in banner(html)
        compose = html[html.index('id="board-message"'):html.index("</textarea>")]
        assert "y" * 300 in compose
        # reloading the page that showed it shows neither the error nor a retry
        again = world.client.get("/board").get_data(as_text=True)
        assert banner(again) is None and sent(world) == []

    def test_the_error_does_not_come_back_on_reload_of_its_own_url(self, world):
        r = submit(world.client, "/board", follow=False, passphrase=PASS, message="y" * 300)
        url = r.headers["Location"]
        assert "too long" in banner(world.client.get(url).get_data(as_text=True))
        assert banner(world.client.get(url).get_data(as_text=True)) is None

    def test_a_failed_attempt_cannot_be_retried_by_reloading_it(self, world):
        r = submit(world.client, "/board", follow=False, passphrase="wrong", message="hello")
        assert sent(world) == []
        for _ in range(3):
            world.client.get(r.headers["Location"])
        assert sent(world) == []

    def test_the_page_a_failure_returns_to_is_kept(self, world):
        csrf, form = tokens(world.client, "/board")
        r = world.client.post("/board?page=3", data={"csrf_token": csrf, "form_token": form,
                                                     "passphrase": PASS, "message": "z" * 300})
        assert r.headers["Location"].startswith("/board?page=3&note=")

    def test_a_mistyped_passphrase_leaves_the_form_usable_again(self, world):
        submit(world.client, "/board", passphrase="wrong", message="hello")
        assert sent(world) == []
        submit(world.client, "/board", passphrase=PASS, message="hello")
        assert len(sent(world)) == 1


class TestNotesCannotBeAbused:
    def test_a_note_id_nobody_issued_shows_nothing(self, world):
        html = world.client.get("/board?note=deadbeefdeadbeef").get_data(as_text=True)
        assert banner(html) is None

    def test_what_a_note_says_is_escaped(self, world):
        note = forms_for(world.app).notes.keep(
            {"compose_err": "<script>alert(1)</script>", "message_value": "<img src=x onerror=1>"})
        html = world.client.get(f"/board?note={note}").get_data(as_text=True)
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=1>" not in html
        assert "&lt;script&gt;" in html

    def test_a_note_cannot_override_what_the_page_decides(self, world):
        note = forms_for(world.app).notes.keep(
            {"csrf_token": "evil", "form_token": "evil", "fees": {"next_block": 0},
             "compose_err": "ok"})
        html = world.client.get(f"/board?note={note}").get_data(as_text=True)
        assert 'value="evil"' not in html

    def test_a_note_is_not_shown_by_another_page(self, world):
        note = forms_for(world.app).notes.keep({"alert_err": "for the send page"})
        assert "for the send page" not in world.client.get(f"/board?note={note}").get_data(as_text=True)

    def test_the_public_app_issues_no_token_and_reads_no_note(self, world):
        app = api.create_app(world.node, peerpool_mod.PeerPool())
        note = forms_for(app).notes.keep({"compose_err": "x"})
        html = app.test_client().get(f"/board?note={note}").get_data(as_text=True)
        assert 'name="form_token"' not in html
        assert forms_for(app).notes.take(note) == {"compose_err": "x"}      # still there


# ---------------------------------------------------------------------------
# Paying: the case that matters most
# ---------------------------------------------------------------------------

class TestSendingMoneyOnce:
    PAY = {"outputs": f"{address(3)},1000"}

    def test_a_send_is_answered_with_a_redirect_and_shows_once(self, world):
        r = submit(world.client, "/send", follow=False, passphrase=PASS, **self.PAY)
        assert r.status_code == 303 and r.headers["Location"].startswith("/send?note=")
        first = world.client.get(r.headers["Location"]).get_data(as_text=True)
        second = world.client.get(r.headers["Location"]).get_data(as_text=True)
        assert "Sent." in first and "Sent." not in second

    def test_reloading_never_pays_again(self, world):
        r = submit(world.client, "/send", follow=False, passphrase=PASS, **self.PAY)
        for _ in range(10):
            world.client.get(r.headers["Location"])
            world.client.get("/send")
        assert [t["outputs"][0]["amount"] for t in sent(world)] == [1000]

    def test_replaying_the_form_never_pays_again(self, world):
        csrf, form = tokens(world.client, "/send")
        data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS, **self.PAY}
        world.client.post("/send", data=data)
        again = world.client.post("/send", data=data, follow_redirects=True)
        assert len(sent(world)) == 1 and ALREADY_SENT in again.get_data(as_text=True)

    def test_a_double_click_pays_once(self, world):
        csrf, form = tokens(world.client, "/send")
        data = {"csrf_token": csrf, "form_token": form, "passphrase": PASS, **self.PAY}
        clients = [world.app.test_client() for _ in range(6)]
        gate = threading.Barrier(len(clients))

        def go(c):
            gate.wait()
            c.post("/send", data=data)
        threads = [threading.Thread(target=go, args=(c,)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(sent(world)) == 1

    def test_a_mistake_is_shown_once_with_what_was_typed(self, world):
        r = submit(world.client, "/send", follow=False, passphrase=PASS, outputs="not an address,5")
        url = r.headers["Location"]
        html = world.client.get(url).get_data(as_text=True)
        assert "invalid address" in html and "not an address,5" in html
        assert "invalid address" not in world.client.get(url).get_data(as_text=True)
        assert sent(world) == []

    def test_a_wrong_passphrase_is_not_retried_by_reloading(self, world):
        r = submit(world.client, "/send", follow=False, passphrase="wrong", **self.PAY)
        world.client.get(r.headers["Location"])
        world.client.get(r.headers["Location"])
        assert sent(world) == []

    def test_the_send_form_carries_a_token_each_time(self, world):
        a = tokens(world.client, "/send")[1]
        b = tokens(world.client, "/send")[1]
        assert a and b and a != b

    def test_a_send_with_a_stale_token_is_refused(self, world):
        csrf, _ = tokens(world.client, "/send")
        world.client.post("/send", data={"csrf_token": csrf, "form_token": "stale",
                                         "passphrase": PASS, **self.PAY})
        assert sent(world) == []
