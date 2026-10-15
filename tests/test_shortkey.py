import pytest

import shortkey

SEED = bytes(range(48))
PASS = "short key passphrase"


def test_round_trip():
    text = shortkey.encode(SEED, PASS)
    parts = shortkey.decode(text)
    assert shortkey.open_seed(parts, shortkey.derive_kek(parts, PASS)) == SEED


def test_the_text_is_118_characters_and_url_safe():
    text = shortkey.encode(SEED, PASS)
    assert len(text) == 118 and text.startswith("lc1-")
    assert set(text[4:]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_each_text_has_its_own_salt():
    assert shortkey.encode(SEED, PASS) != shortkey.encode(SEED, PASS)


def test_wrong_passphrase():
    parts = shortkey.decode(shortkey.encode(SEED, PASS))
    with pytest.raises(ValueError, match="wrong passphrase"):
        shortkey.open_seed(parts, shortkey.derive_kek(parts, "not it at all"))


def test_a_cut_off_or_mistyped_paste_is_caught_before_the_passphrase():
    text = shortkey.encode(SEED, PASS)
    for bad in (text[:-1], text[:-10], text[:20] + ("A" if text[20] != "A" else "B") + text[21:]):
        with pytest.raises(ValueError, match="incomplete or mistyped"):
            shortkey.decode(bad)


def test_whitespace_from_a_wrapped_paste_is_ignored():
    text = shortkey.encode(SEED, PASS)
    wrapped = text[:40] + "\n  " + text[40:]
    assert shortkey.decode(wrapped) == shortkey.decode(text)


def test_not_a_key():
    with pytest.raises(ValueError, match="not a LapseCoin key"):
        shortkey.decode("hello")


def test_bad_seed_and_empty_passphrase_are_refused():
    with pytest.raises(ValueError):
        shortkey.encode(b"short", PASS)
    with pytest.raises(ValueError):
        shortkey.encode(SEED, "")
