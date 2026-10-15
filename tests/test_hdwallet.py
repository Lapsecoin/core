"""The derivation against the vectors the project site publishes
(wallets.html), and the phrase helpers."""

import hashlib

import pytest

import crypto
import falcon_seeded
import hdwallet

PHRASE = "abandon " * 11 + "about"

# (passphrase, account, FALCON seed, public key SHA-256, address)
VECTORS = [
    ("", 0,
     "fb49a518faeecb48d54921bd28ad8b9cee7b0e58f856f7968cdbe874f968ba07e39ac6960e48974e6913c4d353338e95",
     "e7f162aa47073308083eda3238a6817d53092ba55407c8e57f65df5e829ed515",
     "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"),
    ("", 1,
     "ba57d27131c984f025d2d0c5573ddee34dfc9ce0bbfe3e478613cf3269f6e379fb3c816df3252826eb726ff5b02fee0d",
     "027b9ca53959a5e1f7b3b3c1d55b1ea2df39131b865f2a53ba544ec8a586f378",
     "act.table.citizen.increase.olympic.valley.tell.island.scrub.print.sibling.east"),
    ("TREZOR", 0,
     "201915b01be79dc6b659521e7e69858fc5a372dfe9e4d83fd1f1de631dff4629303447f83c01acb5fa74ef81b0f48d79",
     "da9cd9addb76ea3613dadfb9110cbbcfd4ecdf4764edd433657d406f7e8b3033",
     "surround.traffic.hidden.report.huge.brand.exhaust.fossil.ribbon.mask.confirm.panic"),
]


def test_bip39_standard_seeds():
    assert hdwallet.bip39_seed(PHRASE).hex().startswith("5eb00bbddcf06908")
    assert hdwallet.bip39_seed(PHRASE, "TREZOR").hex().startswith("c55257c360c07c72")


@pytest.mark.parametrize("passphrase,account,seed,_pk,_addr", VECTORS)
def test_falcon_seed_vectors(passphrase, account, seed, _pk, _addr):
    assert hdwallet.falcon_seed_for_account(PHRASE, passphrase, account).hex() == seed


@pytest.mark.skipif(not falcon_seeded.available(), reason="native/falcon_seeded is not built")
@pytest.mark.parametrize("passphrase,account,_seed,pk_sha,addr", VECTORS)
def test_public_key_and_address_vectors(passphrase, account, _seed, pk_sha, addr):
    seed = hdwallet.falcon_seed_for_account(PHRASE, passphrase, account)
    _sk, pk = falcon_seeded.keypair_from_seed(seed)
    assert hashlib.sha256(pk).hexdigest() == pk_sha
    assert crypto.public_key_to_address(pk) == addr


def test_accounts_differ():
    a = hdwallet.falcon_seed_for_account(PHRASE, "", 0)
    assert a != hdwallet.falcon_seed_for_account(PHRASE, "", 1)


@pytest.mark.parametrize("n", [12, 15, 18, 21, 24])
def test_generated_phrases_validate(n):
    p = hdwallet.generate_phrase(n)
    assert len(p.split()) == n and hdwallet.validate_phrase(p) == p


def test_validate_normalizes_case_and_spacing():
    assert hdwallet.validate_phrase("  ABANDON " + "abandon  " * 10 + "About\n") == PHRASE


def test_validate_rejects_typos_and_unknown_words():
    with pytest.raises(ValueError, match="checksum"):
        hdwallet.validate_phrase("abandon " * 12)
    with pytest.raises(ValueError, match="not a recovery phrase word"):
        hdwallet.validate_phrase("abandon " * 11 + "zzzz")
    with pytest.raises(ValueError, match="12, 15"):
        hdwallet.validate_phrase("abandon abandon")


def test_account_range():
    with pytest.raises(ValueError):
        hdwallet.falcon_seed_for_account(PHRASE, "", -1)
