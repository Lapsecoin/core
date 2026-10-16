"""Who a node runs as and when it asks for a passphrase: what it was given, in
what order that wins, which entry formats are read, and the plan for each start.
Every row is a promise about what a person will see."""

import os
import subprocess
import sys

import pytest

import api
import falcon_seeded
import peerpool as peerpool_mod
import startup
import wallet as wallet_mod
from chainstate import ChainState
from node import Node
from tests.test_accounts_ui import PASS, PHRASE
from tests.test_api import _FakeNode
from walletfile import WalletFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDR_A = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"
ADDR_B = "act.table.citizen.increase.olympic.valley.tell.island.scrub.print.sibling.east"
LC1 = "lc1-" + "x" * 40

needs_native = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (wallet_mod.ADDRESS_ENV, wallet_mod.KEY_ENV, wallet_mod.PASSPHRASE_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def make_wallet_file(phrase_shown=True):
    WalletFile.create(wallet_mod.WALLET_FILE, PHRASE, PASS, phrase_shown=phrase_shown)


# What it was given, and which wins
@needs_native
def test_with_nothing_given_the_wallet_file_is_used(home):
    make_wallet_file()
    assert wallet_mod.find_source().kind == "wallet"


def test_an_address_on_the_command_line_beats_a_wallet_file(home):
    open(wallet_mod.WALLET_FILE, "w").write("{}")
    s = wallet_mod.find_source(address_text=ADDR_A)
    assert s == wallet_mod.Source("address", ADDR_A)


def test_an_address_from_the_environment_beats_a_wallet_file(home, monkeypatch):
    open(wallet_mod.WALLET_FILE, "w").write("{}")
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, ADDR_B)
    assert wallet_mod.find_source() == wallet_mod.Source("address", ADDR_B)


def test_the_command_line_beats_the_environment(home, monkeypatch):
    monkeypatch.setenv(wallet_mod.KEY_ENV, LC1)
    assert wallet_mod.find_source(address_text=ADDR_A).kind == "address"
    monkeypatch.delenv(wallet_mod.KEY_ENV)
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, ADDR_B)
    assert wallet_mod.find_source(LC1).kind == "key"


def test_an_address_and_a_key_together_on_the_command_line_is_an_error(home):
    with pytest.raises(ValueError, match="--address and --key both given"):
        wallet_mod.find_source(LC1, address_text=ADDR_A)


def test_an_address_and_a_key_both_in_the_environment_is_an_error(home, monkeypatch):
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, ADDR_A)
    monkeypatch.setenv(wallet_mod.KEY_ENV, LC1)
    with pytest.raises(ValueError, match="both set"):
        wallet_mod.find_source()


def test_a_blank_option_counts_as_not_given(home, monkeypatch):
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, ADDR_A)
    assert wallet_mod.find_source("  ", address_text="").kind == "address"


# How an address may be written
@pytest.mark.parametrize("text", [
    ADDR_A,
    ADDR_A.replace(".", " "),
    ADDR_A.replace(".", ", "),
    ADDR_A.replace(".", "\n"),
    "  " + ADDR_A.upper() + "  ",
])
def test_an_address_is_read_however_it_was_pasted(home, text):
    assert wallet_mod.find_source(address_text=text).value == ADDR_A


def test_the_wrong_number_of_words_says_how_many(home):
    short = ".".join(ADDR_A.split(".")[:11])
    with pytest.raises(ValueError, match=r"--address: an address is 12 words.*has 11"):
        wallet_mod.find_source(address_text=short)
    with pytest.raises(ValueError, match="has 13"):
        wallet_mod.find_source(address_text=ADDR_A + ".tree")


def test_a_word_not_in_the_list_is_named_by_position(home):
    words = ADDR_A.split(".")
    words[4] = "industri"
    with pytest.raises(ValueError, match=r"word 5 of the address, 'industri'"):
        wallet_mod.find_source(address_text=".".join(words))


def test_an_address_in_the_environment_names_the_variable_in_its_error(home, monkeypatch):
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, "not an address")
    with pytest.raises(ValueError, match=r"\$LAPSECOIN_ADDRESS"):
        wallet_mod.find_source()


def test_an_address_is_not_taken_for_a_key(home):
    with pytest.raises(ValueError, match="--key takes the key text"):
        wallet_mod.find_source(ADDR_A)


# What an address-only node is
def test_an_address_gives_a_wallet_that_holds_no_key(home):
    w = wallet_mod.open_locked(wallet_mod.Source("address", ADDR_A))
    assert w.addr == ADDR_A and w.can_sign is False and w.pk is None
    with pytest.raises(ValueError, match="holds no key"):
        w.derive_kek("anything")
    with pytest.raises(ValueError, match="no key to unlock"):
        wallet_mod.unlock(wallet_mod.Source("address", ADDR_A), "anything")


def test_wallet_commands_say_an_address_only_node_has_no_wallet(home):
    with pytest.raises(SystemExit) as stop:
        wallet_mod._need_wallet(wallet_mod.Source("address", ADDR_A), "export")
    assert "address only" in str(stop.value)


# The plan for every start. Rows: what it runs as, window, terminal, passphrase in the environment
NOW, LATER, NEVER = startup.ASK, startup.DEFER, startup.WATCH


@needs_native
@pytest.mark.parametrize("kind, gui, tty, env, expected", [
    # an address never asks
    ("address", True,  True,  False, NEVER),
    ("address", False, True,  False, NEVER),
    ("address", False, False, False, NEVER),
    ("address", False, False, True,  NEVER),
    # a confirmed wallet and an old single-key text can wait
    ("wallet",  True,  True,  False, LATER),
    ("wallet",  True,  False, False, LATER),
    ("wallet",  False, False, False, LATER),
    ("wallet",  False, True,  False, NOW),       # a terminal prompts
    ("wallet",  True,  True,  True,  NOW),       # the environment already has it
    ("wallet",  False, False, True,  NOW),
    ("text",    True,  True,  False, LATER),
    ("text",    False, False, False, LATER),
    ("text",    False, True,  False, NOW),
    # these have to ask first
    ("key",     True,  True,  False, NOW),
    ("key",     False, True,  False, NOW),
    ("key",     False, False, True,  NOW),
    ("legacy",  True,  False, False, NOW),
    ("legacy",  False, True,  False, NOW),
    ("new",     True,  False, False, NOW),
    ("new",     False, True,  False, NOW),
    ("new",     False, False, True,  NOW),
    ("unconfirmed", True,  False, False, NOW),
    ("unconfirmed", False, True,  False, NOW),
])
def test_the_plan(home, kind, gui, tty, env, expected):
    if kind == "wallet":
        make_wallet_file()
        source = wallet_mod.Source("wallet", wallet_mod.WALLET_FILE)
    elif kind == "unconfirmed":
        make_wallet_file(phrase_shown=False)
        source = wallet_mod.Source("wallet", wallet_mod.WALLET_FILE)
    elif kind == "address":
        source = wallet_mod.Source("address", ADDR_A)
    elif kind == "text":
        source = wallet_mod.Source("text", {"public_key": "00"})
    else:
        source = wallet_mod.Source(kind, "x")
    assert startup.plan(source, 0, gui=gui, tty=tty, env_passphrase=env).how == expected


@needs_native
@pytest.mark.parametrize("kind", ["key", "legacy", "new", "unconfirmed"])
def test_a_start_that_must_ask_and_cannot_stops_and_says_why(home, kind):
    if kind == "unconfirmed":
        make_wallet_file(phrase_shown=False)
        source = wallet_mod.Source("wallet", wallet_mod.WALLET_FILE)
    else:
        source = wallet_mod.Source(kind, "x")
    with pytest.raises(ValueError, match="cannot ask.*LAPSECOIN_PASSPHRASE"):
        startup.plan(source, 0, gui=False, tty=False, env_passphrase=False)


# The pages of a node that holds no key
class WatchNode(_FakeNode):
    can_sign = Node.can_sign

    def __init__(self):
        super().__init__(ChainState.from_genesis(), addr=ADDR_A)
        self.wallet = wallet_mod.WatchWallet(ADDR_A)
        self.needs_unlock = False


@pytest.fixture
def watch_client():
    return api.create_private_app(WatchNode(), peerpool_mod.PeerPool()).test_client()


@pytest.mark.parametrize("path", ["/send", "/fees"])
def test_the_signing_pages_say_there_is_no_key(watch_client, path):
    r = watch_client.get(path)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "holds no key" in html and ADDR_A in html and 'type="password"' not in html


@pytest.mark.parametrize("path", ["/board", "/board/delete", "/board/vote", "/fees",
                                  "/addresses/new", "/addresses/label", "/addresses/export", "/send"])
def test_nothing_is_signed_on_a_node_without_a_key(watch_client, path):
    r = watch_client.post(path, data={"passphrase": "x"})
    assert r.status_code == 403 and "holds no key" in r.get_data(as_text=True)


def test_the_rest_of_the_private_pages_still_work(watch_client):
    for path in ("/blocks", "/mempool", "/network", "/settings"):
        assert watch_client.get(path).status_code == 200, path


def test_there_is_nothing_to_unlock(watch_client):
    r = watch_client.get("/unlock")
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_the_menu_offers_no_wallet_and_no_fee_requests(watch_client):
    html = watch_client.get("/blocks").get_data(as_text=True)
    assert 'href="/send"' not in html and 'href="/fees"' not in html
    assert 'href="/board"' in html


def test_the_board_has_no_compose_form_without_a_key(watch_client):
    html = watch_client.get("/board").get_data(as_text=True)
    assert '<form method="POST" action="/board' not in html


def test_a_node_given_a_passphrase_cannot_unlock_what_has_no_key():
    n = Node.__new__(Node)
    n.wallet = wallet_mod.WatchWallet(ADDR_A)
    with pytest.raises(ValueError, match="holds no key"):
        n.unlock_with_passphrase("anything")


# What was removed because it was never needed
def test_the_discovery_slot_and_write_time_come_from_the_nodes_address():
    import discovery_dht

    def dht(address):
        return discovery_dht.DHTDiscovery(lambda addr: None, "a" * 64, 8333, address)

    a, same = dht(ADDR_A), dht(ADDR_A)
    where = "1.2.3.4:8333"
    assert a.slot_for(where) == same.slot_for(where) and a._my_write_offset() == same._my_write_offset()
    # different nodes spread out: over a handful of addresses and places they do not all coincide
    slots = {dht(w).slot_for(where) for w in (ADDR_A, ADDR_B, ADDR_A + "x", ADDR_B + "x", "a.b", "c.d")}
    offsets = {dht(w)._my_write_offset() for w in (ADDR_A, ADDR_B, ADDR_A + "x", ADDR_B + "x", "a.b", "c.d")}
    assert len(slots) > 1 and len(offsets) > 1
    assert not hasattr(a, "node_pubkey_hex")


def test_a_node_carries_no_public_key_of_its_own():
    assert not hasattr(Node.__new__(Node), "pk_hex")


# The command line
def run_main(*args, env=None, cwd=None):
    full = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src"), **(env or {})}
    for name in (wallet_mod.ADDRESS_ENV, wallet_mod.KEY_ENV, wallet_mod.PASSPHRASE_ENV):
        full.pop(name, None) if not (env and name in env) else None
    return subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "--no-gui", *args],
                          capture_output=True, text=True, timeout=120, cwd=cwd, env=full)


def test_the_command_line_stops_on_both_an_address_and_a_key(home):
    r = run_main("--address", ADDR_A, "--key", LC1, cwd=home)
    assert r.returncode != 0 and "--address and --key both given" in (r.stdout + r.stderr)


def test_the_command_line_stops_on_a_bad_address_and_says_which_word(home):
    bad = ADDR_A.replace("industry", "industri")
    r = run_main("--address", bad, cwd=home)
    assert r.returncode != 0 and "word 5 of the address" in (r.stdout + r.stderr)


def test_wallet_commands_refuse_an_address(home):
    r = run_main("--address", ADDR_A, "--export", "1", cwd=home)
    assert r.returncode != 0 and "holds no key" in (r.stdout + r.stderr)


# Conflicts, and what is passed over
@pytest.mark.parametrize("other, name", [({"key_text": LC1}, "--key"), ({"address_text": ADDR_A}, "--address")])
def test_account_with_a_key_or_an_address_is_an_error(home, other, name):
    with pytest.raises(ValueError, match=f"{name} and --account both given"):
        wallet_mod.find_source(account_given=True, **other)


@needs_native
def test_account_runs_the_wallet_file_and_beats_the_environment(home, monkeypatch):
    make_wallet_file()
    monkeypatch.setenv(wallet_mod.KEY_ENV, LC1)
    notes = []
    source = wallet_mod.find_source(account_given=True, notes=notes)
    assert source.kind == "wallet"
    assert any("$LAPSECOIN_KEY is ignored: --account was given" in n for n in notes)


def test_account_without_a_wallet_file_is_an_error(home):
    with pytest.raises(ValueError, match="--account picks an address of .* and there is none here"):
        wallet_mod.find_source(account_given=True)


def test_account_with_only_an_old_key_file_says_how_to_move_it(home):
    open(wallet_mod.LEGACY_KEY_FILE, "w").write("{}")
    with pytest.raises(ValueError, match="start without --account to move it"):
        wallet_mod.find_source(account_given=True)


def test_what_the_command_line_passes_over_is_reported(home, monkeypatch):
    monkeypatch.setenv(wallet_mod.KEY_ENV, LC1)
    notes = []
    assert wallet_mod.find_source(address_text=ADDR_A, notes=notes).kind == "address"
    assert notes == ["$LAPSECOIN_KEY is ignored: --address was given"]


@pytest.mark.parametrize("which, word", [("burn", "burn"), ("escrow", "escrow")])
def test_an_address_that_destroys_the_reward_is_refused(home, which, word):
    import crypto
    address = crypto.burn_address() if which == "burn" else crypto.escrow_address()
    with pytest.raises(ValueError, match=f"that is the {word} address"):
        wallet_mod.find_source(address_text=address)


@needs_native
def test_the_wallet_files_addresses_are_found_without_a_passphrase(home):
    make_wallet_file()
    first = WalletFile.load(wallet_mod.WALLET_FILE).accounts[0]["address"]
    assert wallet_mod.local_account_of(first) == (0, "")
    assert wallet_mod.local_account_of(ADDR_B) is None      # account 1 of that phrase, not in the file


def test_no_wallet_file_or_a_broken_one_means_no_match(home):
    assert wallet_mod.local_account_of(ADDR_A) is None
    open(wallet_mod.WALLET_FILE, "w").write("not json")
    assert wallet_mod.local_account_of(ADDR_A) is None


@needs_native
def test_an_address_that_is_one_of_the_wallets_is_pointed_out_and_still_watch_only(home):
    make_wallet_file()
    mine = WalletFile.load(wallet_mod.WALLET_FILE).accounts[0]["address"]
    [note] = wallet_mod.notes_about_local_wallet("address", mine)
    assert "address 0" in note and "Running with no key, as asked" in note and "--account 0" in note
    assert wallet_mod.notes_about_local_wallet("address", ADDR_B) == []


@needs_native
def test_a_key_is_compared_with_the_wallet_file_after_it_is_opened(home):
    make_wallet_file()
    mine = WalletFile.load(wallet_mod.WALLET_FILE).accounts[0]["address"]
    [same] = wallet_mod.notes_about_local_wallet("key", mine)
    assert "this key is address 0" in same and "wallet file is not used" in same
    [other] = wallet_mod.notes_about_local_wallet("key", ADDR_B)
    assert "not an address of" in other and "wallet file is not used" in other


def test_nothing_is_said_about_a_wallet_file_that_is_not_there(home):
    assert wallet_mod.notes_about_local_wallet("address", ADDR_A) == []
    assert wallet_mod.notes_about_local_wallet("key", ADDR_A) == []


def test_the_command_line_stops_on_account_with_a_key(home):
    r = run_main("--account", "1", "--key", LC1, cwd=home)
    assert r.returncode != 0 and "--key and --account both given" in (r.stdout + r.stderr)


def test_the_command_line_stops_on_account_with_no_wallet(home):
    r = run_main("--account", "1", cwd=home)
    assert r.returncode != 0 and "there is none here" in (r.stdout + r.stderr)


def test_help_explains_who_the_node_runs_as_and_every_variable_it_reads():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "-h"], capture_output=True,
                         text=True, timeout=60, env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")}).stdout
    for needle in ("who this node runs as", "wallet commands", "network", "data and logs",
                   wallet_mod.ADDRESS_ENV, wallet_mod.KEY_ENV, wallet_mod.PASSPHRASE_ENV,
                   wallet_mod.EXPORT_PASSPHRASE_ENV, "LAPSECOIN_PORT", "LAPSECOIN_MINING_ENABLED"):
        assert needle in out, needle
