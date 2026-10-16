"""Where the node's key comes from: the wallet file, an lc1- key, an old key,
and the commands that run and exit (--restore, --new-address, --export)."""

import json
import os
import subprocess
import sys

import pytest

import crypto
import falcon_seeded
import hdwallet
import shortkey
import wallet
from walletfile import WalletFile
from tests.legacy_key import save_key

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")

PASS = "wallet test passphrase"
BOX = "mining box passphrase"
PHRASE = "abandon " * 11 + "about"
ADDR0 = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"
ADDR1 = "act.table.citizen.increase.olympic.valley.tell.island.scrub.print.sibling.east"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in (wallet.KEY_ENV, wallet.PASSPHRASE_ENV, wallet.EXPORT_PASSPHRASE_ENV):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def make_legacy(path):
    sk, pk = crypto.generate_keypair()
    save_key(str(path), sk, pk, PASS)
    return pk


# Choosing a source
def test_nothing_here_means_a_new_wallet(home):
    assert wallet.find_source().kind == "new"


def test_the_wallet_file_is_used_when_it_exists(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    assert wallet.find_source().kind == "wallet"


def test_an_old_key_file_means_migrate(home):
    make_legacy(home / wallet.LEGACY_KEY_FILE)
    assert wallet.find_source().kind == "legacy"


def test_a_wallet_file_beats_an_old_key_file(home):
    """A crash part way through moving to a phrase leaves both."""
    make_legacy(home / wallet.LEGACY_KEY_FILE)
    wallet.create_wallet(PASS, phrase=PHRASE)
    assert wallet.find_source().kind == "wallet"


def test_key_text_beats_files_and_never_migrates(home):
    make_legacy(home / wallet.LEGACY_KEY_FILE)
    text = shortkey.encode(bytes(48), BOX)
    assert wallet.find_source(text).kind == "key"
    assert wallet.find_source(json.dumps(json.load(open(wallet.LEGACY_KEY_FILE)))).kind == "text"


def test_the_environment_variable_takes_key_text(home, monkeypatch):
    monkeypatch.setenv(wallet.KEY_ENV, shortkey.encode(bytes(48), BOX))
    assert wallet.find_source().kind == "key"


def test_a_path_is_not_accepted(home):
    for bad in ("some/where.json", "lapsecoin_key.json", "{not json"):
        with pytest.raises(ValueError):
            wallet.find_source(bad)


def test_a_noise_line_in_front_of_old_key_text_is_ignored(home):
    make_legacy(home / "old.json")
    text = json.dumps(json.load(open("old.json")))
    assert wallet.find_source("liboqs-python faulthandler is disabled\n" + text + "\n").kind == "text"


# Unlocking each kind
def test_phrase_wallet_signs_for_its_account(home):
    w, kek, phrase = wallet.create_wallet(PASS, phrase=PHRASE)
    assert w.addr == ADDR0 and phrase == PHRASE
    again, kek2 = wallet.unlock(wallet.find_source(), PASS)
    sk = again.secret_key(kek2)
    assert crypto.verify(b"x", crypto.sign(b"x", sk), again.pk)


def test_the_wrong_passphrase_is_refused(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    with pytest.raises(ValueError):
        wallet.unlock(wallet.find_source(), "not the passphrase")


def test_an_account_that_is_not_in_the_wallet_is_refused(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    with pytest.raises(ValueError, match="account 3"):
        wallet.unlock(wallet.find_source(), PASS, account=3)


def test_seed_key_wallet_matches_the_account_it_came_from(home):
    wf = WalletFile.create(str(home / "w.json"), PHRASE, PASS)
    kek = wf.derive_kek(PASS)
    wf.new_account(kek)
    text = wf.export_account_key(kek, 1, BOX, PASS)
    w, kbox = wallet.unlock(wallet.find_source(text), BOX)
    assert w.addr == ADDR1
    assert crypto.verify(b"m", crypto.sign(b"m", w.secret_key(kbox)), w.pk)
    with pytest.raises(ValueError):
        wallet.unlock(wallet.find_source(text), "wrong passphrase")


def test_old_key_text_still_runs_without_touching_disk(home):
    pk = make_legacy(home / "old.json")
    text = json.dumps(json.load(open("old.json")))
    os.remove("old.json")
    w, kek = wallet.unlock(wallet.find_source(text), PASS)
    assert w.pk == pk
    assert not os.listdir(home)


def test_sign_tx_builds_a_valid_transaction(home):
    import tx as tx_mod
    w, kek, _ = wallet.create_wallet(PASS, phrase=PHRASE)
    t, fee = w.sign_tx([{"to": ADDR1, "amount": 5}], 1, 1, kek=kek)
    assert t["from"] == ADDR0 and fee == 1
    assert tx_mod._check_signature(t) == (True, None)
    t2, _ = w.sign_tx([{"to": ADDR1, "amount": 5}], 2, 1, passphrase=PASS)
    assert tx_mod._check_signature(t2) == (True, None)


def test_create_flags_the_phrase_until_it_is_shown(home):
    w, kek, phrase = wallet.create_wallet(PASS)
    assert len(phrase.split()) == 12 and hdwallet.validate_phrase(phrase) == phrase
    assert not w.walletfile.phrase_shown
    assert not WalletFile.load(wallet.WALLET_FILE).phrase_shown


# The console flow
def test_console_creates_a_wallet_and_prints_the_phrase_once(home, monkeypatch, capsys):
    monkeypatch.setenv(wallet.PASSPHRASE_ENV, PASS)
    w, kek, passphrase = wallet.startup_console()
    out = capsys.readouterr().out
    phrase = WalletFile.load(wallet.WALLET_FILE).phrase(kek)
    assert all(word in out for word in phrase.split())
    assert WalletFile.load(wallet.WALLET_FILE).phrase_shown
    # the next start unlocks it and does not show the phrase again
    w2, _kek2, _ = wallet.startup_console()
    assert w2.addr == w.addr and phrase.split()[0] + " " not in capsys.readouterr().out


def test_console_shows_the_phrase_again_if_it_was_never_acknowledged(home, monkeypatch, capsys):
    monkeypatch.setenv(wallet.PASSPHRASE_ENV, PASS)
    w, kek, phrase = wallet.create_wallet(PASS)          # then "crashed"
    wallet.startup_console()
    out = capsys.readouterr().out
    assert all(word in out for word in phrase.split())
    assert WalletFile.load(wallet.WALLET_FILE).phrase_shown


def test_console_exits_on_the_wrong_passphrase(home, monkeypatch):
    wallet.create_wallet(PASS, phrase=PHRASE)
    monkeypatch.setenv(wallet.PASSPHRASE_ENV, "definitely wrong")
    with pytest.raises(SystemExit):
        wallet.startup_console()


# The commands
def run_main(cwd, *args, stdin=None, env=None):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), *args],
        capture_output=True, text=True, cwd=str(cwd), input=stdin,
        env={**os.environ, **(env or {})}, timeout=180)


def test_restore_new_address_and_export_end_to_end(home):
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    out = run_main(home, "--restore", stdin=PHRASE + "\n", env=env)
    assert out.returncode == 0, out.stderr
    assert ADDR0 in out.stdout
    assert WalletFile.load(str(home / wallet.WALLET_FILE)).scan_pending

    out = run_main(home, "--new-address", env=env)
    assert out.returncode == 0, out.stderr
    assert ADDR1 in out.stdout

    out = run_main(home, "--export", "--account", "1", env=env)
    assert out.returncode == 0, out.stderr
    text = [ln for ln in out.stdout.splitlines() if ln.startswith("lc1-")][0]
    assert len(text) == 118
    w, _ = wallet.unlock(wallet.find_source(text), BOX)
    assert w.addr == ADDR1


def test_export_refuses_the_wallet_passphrase(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    out = run_main(home, "--export", env={wallet.PASSPHRASE_ENV: PASS,
                                          wallet.EXPORT_PASSPHRASE_ENV: PASS})
    assert out.returncode != 0 and "different" in out.stderr


def test_export_of_an_account_that_does_not_exist(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    out = run_main(home, "--export", "--account", "9", env={
        wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX})
    assert out.returncode != 0 and "account 9" in out.stderr


def test_restore_refuses_to_overwrite_and_checks_the_phrase(home):
    env = {wallet.PASSPHRASE_ENV: PASS}
    bad = run_main(home, "--restore", stdin="abandon abandon abandon\n", env=env)
    assert bad.returncode != 0 and "12, 15" in bad.stderr
    typo = run_main(home, "--restore", stdin="abandon " * 12 + "\n", env=env)
    assert typo.returncode != 0 and "typo" in typo.stderr
    assert run_main(home, "--restore", stdin=PHRASE + "\n", env=env).returncode == 0
    again = run_main(home, "--restore", stdin=PHRASE + "\n", env=env)
    assert again.returncode != 0 and "already exists" in again.stderr


def test_export_of_an_old_key_file_prints_it_as_before(home):
    make_legacy(home / wallet.LEGACY_KEY_FILE)
    out = run_main(home, "--export")
    assert out.returncode == 0, out.stderr
    assert wallet.find_source(out.stdout).kind == "text"


# --account takes a number or a name

def test_account_by_number_or_by_name(home):
    w, kek, _ = wallet.create_wallet(PASS, phrase=PHRASE)
    w.walletfile.new_account(kek)
    w.walletfile.set_label(1, "Shop")
    assert wallet.resolve_account("1") == 1 and wallet.resolve_account(None) == 0
    assert wallet.resolve_account("shop") == 1 and wallet.resolve_account("SHOP") == 1


def test_an_unknown_or_repeated_name_says_what_the_names_are(home):
    w, kek, _ = wallet.create_wallet(PASS, phrase=PHRASE)
    w.walletfile.new_account(kek)
    w.walletfile.new_account(kek)
    w.walletfile.set_label(0, "mining")
    w.walletfile.set_label(1, "shop")
    w.walletfile.set_label(2, "shop")
    with pytest.raises(ValueError, match="mining"):
        wallet.resolve_account("nobody")
    with pytest.raises(ValueError, match="more than one"):
        wallet.resolve_account("shop")


def test_a_name_needs_a_wallet_to_look_it_up_in(home):
    with pytest.raises(ValueError, match="no wallet"):
        wallet.resolve_account("shop")


def test_export_accepts_the_name(home):
    w, kek, _ = wallet.create_wallet(PASS, phrase=PHRASE)
    w.walletfile.new_account(kek)
    w.walletfile.set_label(1, "shop")
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    out = run_main(home, "--export", "--account", "shop", env=env)
    assert out.returncode == 0, out.stderr
    text = [ln for ln in out.stdout.splitlines() if ln.startswith("lc1-")][0]
    assert wallet.unlock(wallet.find_source(text), BOX)[0].addr == ADDR1


# --export NAME makes the address when there is none

def test_export_by_a_new_name_creates_that_address_and_keeps_stdout_clean(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    out = run_main(home, "--export", "rig1", env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1].startswith("lc1-")
    assert [ln for ln in out.stdout.splitlines() if not ln.startswith("lc1-") and ln.strip()
            ] == [ln for ln in out.stdout.splitlines() if "faulthandler" in ln]
    assert "Created address 1 named 'rig1'" in out.stderr and ADDR1 in out.stderr
    wf = WalletFile.load(str(home / wallet.WALLET_FILE))
    assert wf.account(1)["label"] == "rig1" and wf.address(1) == ADDR1


def test_two_rigs_get_two_addresses_and_a_rig_keeps_its_own(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    keys = {}
    for name in ("rig1", "rig2", "RIG1"):
        out = run_main(home, "--export", name, env=env)
        assert out.returncode == 0, out.stderr
        keys[name] = [ln for ln in out.stdout.splitlines() if ln.startswith("lc1-")][0]
    addr = lambda n: wallet.unlock(wallet.find_source(keys[n]), BOX)[0].addr
    assert addr("rig1") != addr("rig2")
    assert addr("RIG1") == addr("rig1")                  # same name: the same address again
    assert [a["index"] for a in WalletFile.load(str(home / wallet.WALLET_FILE)).accounts] == [0, 1, 2]


def test_export_without_a_value_still_uses_account(home):
    w, kek, _ = wallet.create_wallet(PASS, phrase=PHRASE)
    w.walletfile.new_account(kek)
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    out = run_main(home, "--export", "--account", "1", env=env)
    assert out.returncode == 0, out.stderr
    text = [ln for ln in out.stdout.splitlines() if ln.startswith("lc1-")][0]
    assert wallet.unlock(wallet.find_source(text), BOX)[0].addr == ADDR1


def test_a_number_that_is_not_there_is_not_created(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    env = {wallet.PASSPHRASE_ENV: PASS, wallet.EXPORT_PASSPHRASE_ENV: BOX}
    out = run_main(home, "--export", "7", env=env)
    assert out.returncode != 0 and "account 7" in out.stderr
    assert len(WalletFile.load(str(home / wallet.WALLET_FILE)).accounts) == 1


def test_the_node_does_not_invent_an_address_from_an_unknown_name(home):
    wallet.create_wallet(PASS, phrase=PHRASE)
    out = run_main(home, "--new-address", "--account", "nobody", env={wallet.PASSPHRASE_ENV: PASS})
    assert out.returncode != 0 and "no address is named" in out.stderr
