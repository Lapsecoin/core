"""--key / $LAPSECOIN_KEY: a path or the text --export prints."""
import os
import subprocess
import sys

import pytest

import crypto
import wallet

PASS = "key option passphrase"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(wallet.KEY_ENV, raising=False)
    return tmp_path


def make_key(path):
    sk, pk = crypto.generate_keypair()
    crypto.save_key(str(path), sk, pk, PASS)
    return pk.hex()


def test_a_path_is_used_as_it_is(home):
    assert wallet.key_path("some/where.json") == "some/where.json"


def test_the_default_is_the_key_file_in_the_working_directory(home):
    assert wallet.key_path() == wallet.DEFAULT_KEY


def test_the_environment_variable_is_a_path_too(home, monkeypatch):
    monkeypatch.setenv(wallet.KEY_ENV, "from/env.json")
    assert wallet.key_path() == "from/env.json"


def test_the_option_beats_the_environment(home, monkeypatch):
    monkeypatch.setenv(wallet.KEY_ENV, "from/env.json")
    assert wallet.key_path("from/option.json") == "from/option.json"


def test_exported_text_round_trips_through_the_option(home):
    source = home / "other.json"
    pub = make_key(source)
    text = wallet.export_key(str(source))
    assert "\n" not in text
    path = wallet.key_path(text)
    assert path == wallet.DEFAULT_KEY
    assert crypto.load_pubkey(path).hex() == pub
    crypto.decrypt_secret_key(path, passphrase=PASS)   # opens with the passphrase


def test_exported_text_works_through_the_environment(home, monkeypatch):
    source = home / "other.json"
    pub = make_key(source)
    monkeypatch.setenv(wallet.KEY_ENV, wallet.export_key(str(source)))
    assert crypto.load_pubkey(wallet.key_path()).hex() == pub


def test_the_saved_file_is_private(home):
    source = home / "other.json"
    make_key(source)
    wallet.key_path(wallet.export_key(str(source)))
    assert os.stat(wallet.DEFAULT_KEY).st_mode & 0o077 == 0


def test_the_same_key_again_is_fine(home):
    source = home / "other.json"
    make_key(source)
    text = wallet.export_key(str(source))
    wallet.key_path(text)
    assert wallet.key_path(text) == wallet.DEFAULT_KEY


def test_a_different_key_never_overwrites_the_one_on_disk(home):
    make_key(home / wallet.DEFAULT_KEY)
    other = home / "other.json"
    make_key(other)
    with pytest.raises(ValueError, match="different key"):
        wallet.key_path(wallet.export_key(str(other)))


def test_text_that_is_not_a_key_is_refused(home):
    for bad in ("{}", "{not json", '{"public_key": 1, "ciphertext": "a", "salt": "b"}'):
        with pytest.raises(ValueError):
            wallet.key_path(bad)
    assert not os.path.exists(wallet.DEFAULT_KEY)


def test_a_noise_line_in_front_of_the_exported_text_is_ignored(home):
    source = home / "other.json"
    pub = make_key(source)
    noisy = "liboqs-python faulthandler is disabled\n" + wallet.export_key(str(source)) + "\n"
    assert crypto.load_pubkey(wallet.key_path(noisy)).hex() == pub


def run_export(*args, env=None):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), "--export", *args],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, **(env or {})}, timeout=120)


def test_export_prints_the_key_and_the_old_option_name_still_works(home):
    source = home / "k.json"
    make_key(source)
    for flag in ("--key", "--keyfile"):
        out = run_export(flag, str(source))
        assert out.returncode == 0, out.stderr
        assert wallet.key_path(out.stdout) == wallet.DEFAULT_KEY
        assert crypto.load_pubkey(wallet.key_path(out.stdout)).hex() == crypto.load_pubkey(str(source)).hex()
        os.remove(wallet.DEFAULT_KEY)


def test_export_reads_the_environment_variable(home):
    source = home / "k.json"
    make_key(source)
    out = run_export(env={wallet.KEY_ENV: str(source)})
    assert out.returncode == 0, out.stderr
    assert crypto.load_pubkey(wallet.key_path(out.stdout)).hex() == crypto.load_pubkey(str(source)).hex()
