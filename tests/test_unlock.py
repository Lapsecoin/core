"""A node that starts without its passphrase: which starts can wait for it, the
lock page, and what the passphrase switches on when it is typed."""

import threading
import types

import pytest

import api
import falcon_seeded
import peerpool as peerpool_mod
import wallet as wallet_mod
from chainstate import ChainState
from node import Node
from tests.browser import tokens
from tests.test_accounts_ui import PASS, PHRASE, WalletNode

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")


class LockedNode(WalletNode):
    unlock = Node.unlock
    unlock_with_passphrase = Node.unlock_with_passphrase

    def __init__(self, cs, w):
        super().__init__(cs, w)
        self.needs_unlock = True
        self._kek = None
        self.background_jobs = []
        self._jobs_lock = threading.Lock()
        self._jobs_up = False


class Job:
    def __init__(self):
        self.started = 0

    def start(self, node):
        self.started += 1


@pytest.fixture
def locked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w, _kek, _ = wallet_mod.create_wallet(PASS, phrase=PHRASE)
    w = wallet_mod.PhraseWallet(w.walletfile, 0)
    node = LockedNode(ChainState.from_genesis(), w)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    return types.SimpleNamespace(node=node, client=client, wf=w.walletfile)


# Which starts can wait for the passphrase
def test_a_recovery_phrase_wallet_opens_without_the_passphrase(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w, _kek, _ = wallet_mod.create_wallet(PASS, phrase=PHRASE)
    source = wallet_mod.Source("wallet", wallet_mod.WALLET_FILE)
    opened = wallet_mod.open_locked(source, 0)
    assert opened.addr == w.addr


def test_a_wallet_whose_phrase_was_never_shown_asks_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wallet_mod.WalletFile.create(wallet_mod.WALLET_FILE, PHRASE, PASS, phrase_shown=False)
    source = wallet_mod.Source("wallet", wallet_mod.WALLET_FILE)
    assert wallet_mod.open_locked(source, 0) is None


@pytest.mark.parametrize("kind", ["new", "legacy", "key"])
def test_starts_that_make_or_hide_the_address_ask_first(kind):
    assert wallet_mod.open_locked(wallet_mod.Source(kind, "whatever"), 0) is None


# The lock page
def test_a_locked_node_sends_pages_to_the_lock_page(locked):
    r = locked.client.get("/addresses")
    assert r.status_code == 302 and r.headers["Location"].startswith("/unlock?next=/addresses")


def test_a_locked_node_answers_the_api_with_locked(locked):
    assert locked.client.get("/api/info").status_code == 423


def test_the_lock_page_and_its_styles_stay_open(locked):
    assert locked.client.get("/unlock").status_code == 200
    assert locked.client.get("/css/base.css").status_code == 200


def test_the_lock_page_lists_what_the_passphrase_starts(locked):
    html = locked.client.get("/unlock").get_data(as_text=True)
    assert "Create your Base wallet" in html


def test_the_list_follows_the_wallets_state(locked):
    locked.wf._data["scan_pending"] = True
    items = api.unlock_ui.pending_work(locked.node)
    assert any("other addresses" in i for i in items)


def test_a_wrong_passphrase_leaves_it_locked(locked):
    csrf, _ = tokens(locked.client, "/unlock")
    r = locked.client.post("/unlock", data={"csrf_token": csrf, "passphrase": "not it at all", "next": "/"})
    assert r.status_code == 400 and "not this node" in r.get_data(as_text=True)
    assert locked.node.needs_unlock and locked.node._kek is None


def test_a_post_without_the_page_token_is_refused(locked):
    r = locked.client.post("/unlock", data={"csrf_token": "x", "passphrase": PASS})
    assert r.status_code == 403 and locked.node.needs_unlock


def test_the_right_passphrase_opens_the_pages(locked):
    csrf, _ = tokens(locked.client, "/unlock")
    r = locked.client.post("/unlock", data={"csrf_token": csrf, "passphrase": PASS, "next": "/addresses"})
    assert r.status_code == 302 and r.headers["Location"] == "/addresses"
    assert not locked.node.needs_unlock and locked.node._kek is not None
    assert locked.client.get("/addresses").status_code == 200


def test_it_never_sends_you_to_another_site(locked):
    csrf, _ = tokens(locked.client, "/unlock")
    r = locked.client.post("/unlock", data={"csrf_token": csrf, "passphrase": PASS, "next": "//evil.example"})
    assert r.headers["Location"] == "/"


# What the passphrase starts
def test_waiting_jobs_start_once_when_the_loop_is_already_up():
    n = LockedNode.__new__(LockedNode)
    n.needs_unlock, n._kek, n.background_jobs = True, None, []
    n._jobs_lock, n._jobs_up = threading.Lock(), True
    job = Job()
    n.unlock(b"k", [job])
    assert job.started == 1 and n._kek == b"k" and not n.needs_unlock


def test_waiting_jobs_are_left_for_the_loop_when_it_is_not_up_yet():
    n = LockedNode.__new__(LockedNode)
    n.needs_unlock, n._kek, n.background_jobs = True, None, []
    n._jobs_lock, n._jobs_up = threading.Lock(), False
    job = Job()
    n.unlock(b"k", [job])
    assert job.started == 0 and n.background_jobs == [job]


def test_unlocking_creates_the_base_wallet_when_there_is_none(locked, tmp_path):
    import base_wallet
    locked.node.base_wallet_path = str(tmp_path / "base_wallet.json")
    assert base_wallet.load_address(locked.node.base_wallet_path) is None
    locked.node.unlock_with_passphrase(PASS)
    assert base_wallet.load_address(locked.node.base_wallet_path)
