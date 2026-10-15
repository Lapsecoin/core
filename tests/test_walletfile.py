import json
import os

import pytest

import crypto
import falcon_seeded
import hdwallet
import shortkey
import walletfile
from walletfile import WalletFile

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")

PHRASE = "abandon " * 11 + "about"
PASS = "wallet file passphrase"
ADDR0 = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"
ADDR1 = "act.table.citizen.increase.olympic.valley.tell.island.scrub.print.sibling.east"


@pytest.fixture
def wf(tmp_path):
    return WalletFile.create(str(tmp_path / "w.json"), PHRASE, PASS)


def test_account_zero_matches_the_site_vector(wf):
    assert wf.address(0) == ADDR0
    assert [a["index"] for a in wf.accounts] == [0]


def test_file_is_private_and_holds_no_plain_secret(wf):
    assert os.stat(wf.path).st_mode & 0o777 == 0o600
    text = open(wf.path).read()
    assert "abandon" not in text and "about" not in text


def test_create_refuses_to_overwrite(wf):
    with pytest.raises(FileExistsError):
        WalletFile.create(wf.path, PHRASE, PASS)


def test_load_and_unlock(wf):
    again = WalletFile.load(wf.path)
    kek = again.derive_kek(PASS)
    assert again.phrase(kek) == PHRASE
    again.verify(kek)


def test_wrong_passphrase(wf):
    with pytest.raises(ValueError):
        wf.phrase(wf.derive_kek("another passphrase"))


def test_new_account_matches_the_site_vector_and_persists(wf):
    kek = wf.derive_kek(PASS)
    a = wf.new_account(kek)
    assert a["index"] == 1 and a["address"] == ADDR1
    assert WalletFile.load(wf.path).address(1) == ADDR1


def test_secret_key_signs_for_its_address(wf):
    kek = wf.derive_kek(PASS)
    wf.new_account(kek)
    for i in (0, 1):
        sk = wf.secret_key(kek, i)
        sig = crypto.sign(b"msg", sk)
        assert crypto.verify(b"msg", sig, wf.public_key(i))


def test_unknown_account_is_refused(wf):
    with pytest.raises(KeyError):
        wf.secret_key(wf.derive_kek(PASS), 5)


def test_an_edited_account_list_is_caught_on_unlock(wf):
    kek = wf.derive_kek(PASS)
    wf.new_account(kek)
    data = json.load(open(wf.path))
    data["accounts"][1]["address"] = data["accounts"][0]["address"]
    json.dump(data, open(wf.path, "w"))
    reloaded = WalletFile.load(wf.path)
    with pytest.raises(walletfile.WalletFileError, match="altered"):
        reloaded.verify(kek)


def test_export_gives_a_short_key_for_one_account_only(wf):
    kek = wf.derive_kek(PASS)
    wf.new_account(kek)
    text = wf.export_account_key(kek, 1, "box passphrase", PASS)
    assert len(text) == 118
    parts = shortkey.decode(text)
    seed = shortkey.open_seed(parts, shortkey.derive_kek(parts, "box passphrase"))
    assert seed == hdwallet.falcon_seed_for_account(PHRASE, "", 1)
    assert PHRASE.split()[0].encode() not in text.encode()


def test_export_refuses_the_wallet_passphrase_and_an_empty_one(wf):
    kek = wf.derive_kek(PASS)
    with pytest.raises(ValueError, match="different passphrase"):
        wf.export_account_key(kek, 0, PASS, PASS)
    with pytest.raises(ValueError):
        wf.export_account_key(kek, 0, "")


def test_discovered_accounts_are_listed_in_order(wf):
    kek = wf.derive_kek(PASS)
    wf.add_discovered(kek, [3, 1])
    assert [a["index"] for a in wf.accounts] == [0, 1, 3]
    assert wf.address(1) == ADDR1


def test_labels_and_phrase_shown_flag(wf):
    wf.set_label(0, "mining")
    assert WalletFile.load(wf.path).account(0)["label"] == "mining"
    pending = WalletFile.create(wf.path + "2", PHRASE, PASS, phrase_shown=False)
    assert not pending.phrase_shown
    pending.mark_phrase_shown()
    assert WalletFile.load(pending.path).phrase_shown


def test_not_a_wallet_file(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({"public_key": "00", "ciphertext": "", "salt": ""}))
    assert not WalletFile.is_wallet_file(str(p))
    with pytest.raises(walletfile.WalletFileError):
        WalletFile.load(str(p))


def test_no_temp_files_left_behind(wf):
    wf.new_account(wf.derive_kek(PASS))
    assert os.listdir(os.path.dirname(wf.path)) == ["w.json"]
