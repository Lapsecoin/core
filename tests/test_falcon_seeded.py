"""Seeded FALCON-512 key generation: deterministic, the format liboqs
expects, and keys liboqs signs and verifies with."""

import pytest

import falcon_seeded

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(),
    reason="native/falcon_seeded is not built (make -C native/falcon_seeded)")

SEED = bytes(range(48))


def test_same_seed_same_key():
    assert falcon_seeded.keypair_from_seed(SEED) == falcon_seeded.keypair_from_seed(SEED)


def test_different_seed_different_key():
    other = bytes(48)
    assert falcon_seeded.keypair_from_seed(SEED)[1] != falcon_seeded.keypair_from_seed(other)[1]


def test_key_sizes_and_headers():
    sk, pk = falcon_seeded.keypair_from_seed(SEED)
    assert len(sk) == falcon_seeded.SECRET_KEY_BYTES and sk[0] == 0x59
    assert len(pk) == falcon_seeded.PUBLIC_KEY_BYTES and pk[0] == 0x09


def test_wrong_seed_length_is_refused():
    with pytest.raises(ValueError):
        falcon_seeded.keypair_from_seed(b"short")


def test_liboqs_signs_and_verifies_with_a_derived_key():
    crypto = pytest.importorskip("crypto")
    sk, pk = falcon_seeded.keypair_from_seed(SEED)
    sig = crypto.sign(b"hello", sk)
    assert crypto.verify(b"hello", sig, pk)
    assert not crypto.verify(b"hellp", sig, pk)
