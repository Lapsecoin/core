"""
Unit tests for crypto.py

Covers: FALCON-512 keygen, sign, verify; SHA-256 helpers; address derivation;
address validation; canonical JSON; serialization for signing; key storage
(save_key / load_pubkey / decrypt_secret_key / derive_kek / sign_with_keyfile).

All tests are pure and local. No network, no chain, no disk (key-file tests
use a tmp_path fixture).
"""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import crypto
from params import ADDRESS_WORD_COUNT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def keypair():
    """One FALCON-512 keypair shared across read-only tests in this module."""
    return crypto.generate_keypair()


@pytest.fixture(scope="module")
def sk(keypair):
    return keypair[0]


@pytest.fixture(scope="module")
def pk(keypair):
    return keypair[1]


# ---------------------------------------------------------------------------
# 1. Key generation
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_generate_returns_two_bytes_objects(self, keypair):
        sk, pk = keypair
        assert isinstance(sk, bytes)
        assert isinstance(pk, bytes)

    def test_secret_key_length(self, sk):
        # FALCON-512 sk is 1281 bytes (NIST standard)
        assert len(sk) == 1281

    def test_public_key_length(self, pk):
        # FALCON-512 pk is 897 bytes
        assert len(pk) == 897

    def test_two_keypairs_differ(self):
        sk1, pk1 = crypto.generate_keypair()
        sk2, pk2 = crypto.generate_keypair()
        assert sk1 != sk2
        assert pk1 != pk2


# ---------------------------------------------------------------------------
# 2. Sign and verify
# ---------------------------------------------------------------------------

class TestSignVerify:
    def test_valid_signature_verifies(self, sk, pk):
        msg = b"hello lapsecoin"
        sig = crypto.sign(msg, sk)
        assert crypto.verify(msg, sig, pk) is True

    def test_wrong_message_fails(self, sk, pk):
        msg = b"hello lapsecoin"
        sig = crypto.sign(msg, sk)
        assert crypto.verify(b"wrong message", sig, pk) is False

    def test_wrong_key_fails(self, sk, pk):
        sk2, pk2 = crypto.generate_keypair()
        msg = b"hello lapsecoin"
        sig = crypto.sign(msg, sk)
        assert crypto.verify(msg, sig, pk2) is False

    def test_tampered_signature_fails(self, sk, pk):
        msg = b"hello lapsecoin"
        sig = bytearray(crypto.sign(msg, sk))
        sig[10] ^= 0xFF
        assert crypto.verify(msg, bytes(sig), pk) is False

    def test_verify_accepts_hex_string_signature(self, sk, pk):
        msg = b"hex sig test"
        sig_bytes = crypto.sign(msg, sk)
        sig_hex = sig_bytes.hex()
        assert crypto.verify(msg, sig_hex, pk) is True

    def test_sign_returns_bytes(self, sk, pk):
        sig = crypto.sign(b"test", sk)
        assert isinstance(sig, bytes)

    def test_verify_returns_false_for_junk_signature(self, pk):
        assert crypto.verify(b"msg", b"\x00" * 100, pk) is False

    def test_empty_message_signs_and_verifies(self, sk, pk):
        sig = crypto.sign(b"", sk)
        assert crypto.verify(b"", sig, pk) is True


# ---------------------------------------------------------------------------
# 3. SHA-256 helpers
# ---------------------------------------------------------------------------

class TestSha256:
    def test_sha256_bytes_matches_hashlib(self):
        data = b"lapsecoin genesis"
        expected = hashlib.sha256(data).digest()
        assert crypto.sha256(data) == expected

    def test_sha256_string_input(self):
        data = "lapsecoin genesis"
        expected = hashlib.sha256(data.encode()).digest()
        assert crypto.sha256(data) == expected

    def test_sha256_hex_returns_hex_string(self):
        h = crypto.sha256_hex(b"test")
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha256_hex_matches_hashlib(self):
        data = b"test data"
        expected = hashlib.sha256(data).hexdigest()
        assert crypto.sha256_hex(data) == expected

    def test_sha256_hex_string_input(self):
        assert crypto.sha256_hex("test") == crypto.sha256_hex(b"test")

    def test_sha256_deterministic(self):
        assert crypto.sha256(b"abc") == crypto.sha256(b"abc")


# ---------------------------------------------------------------------------
# 4. Address derivation
# ---------------------------------------------------------------------------

class TestAddressDerivation:
    def test_address_returns_string(self, pk):
        addr = crypto.public_key_to_address(pk)
        assert isinstance(addr, str)

    def test_address_is_twelve_dot_separated_words(self, pk):
        addr = crypto.public_key_to_address(pk)
        parts = addr.split(".")
        assert len(parts) == ADDRESS_WORD_COUNT

    def test_address_words_are_bip39(self, pk):
        from crypto import _WORDLIST_SET
        addr = crypto.public_key_to_address(pk)
        for word in addr.split("."):
            assert word in _WORDLIST_SET

    def test_different_keys_produce_different_addresses(self):
        _, pk1 = crypto.generate_keypair()
        _, pk2 = crypto.generate_keypair()
        assert crypto.public_key_to_address(pk1) != crypto.public_key_to_address(pk2)

    def test_address_is_deterministic(self, pk):
        addr1 = crypto.public_key_to_address(pk)
        addr2 = crypto.public_key_to_address(pk)
        assert addr1 == addr2


# ---------------------------------------------------------------------------
# 5. Address validation
# ---------------------------------------------------------------------------

class TestAddressValidation:
    def test_valid_address_passes(self, pk):
        addr = crypto.public_key_to_address(pk)
        assert crypto.is_valid_address(addr) is True

    def test_too_few_words_fails(self):
        from crypto import _WORDLIST
        words = ".".join(_WORDLIST[:ADDRESS_WORD_COUNT - 1])
        assert crypto.is_valid_address(words) is False

    def test_too_many_words_fails(self):
        from crypto import _WORDLIST
        words = ".".join(_WORDLIST[:ADDRESS_WORD_COUNT + 1])
        assert crypto.is_valid_address(words) is False

    def test_non_bip39_word_fails(self):
        from crypto import _WORDLIST
        words = list(_WORDLIST[:ADDRESS_WORD_COUNT])
        words[3] = "NOTAWORD"
        assert crypto.is_valid_address(".".join(words)) is False

    def test_non_string_fails(self):
        assert crypto.is_valid_address(None) is False
        assert crypto.is_valid_address(123) is False
        assert crypto.is_valid_address([]) is False

    def test_empty_string_fails(self):
        assert crypto.is_valid_address("") is False

    def test_burn_address_is_not_valid_bip39(self):
        # "burn" must NOT pass is_valid_address. It's a sentinel, not a real addr
        assert crypto.is_valid_address("burn") is False


# ---------------------------------------------------------------------------
# 6. Canonical JSON
# ---------------------------------------------------------------------------

class TestCanonicalJson:
    def test_keys_sorted(self):
        obj = {"z": 1, "a": 2, "m": 3}
        data = crypto.canonical_json(obj)
        parsed = json.loads(data)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_no_whitespace(self):
        data = crypto.canonical_json({"a": 1})
        assert b" " not in data
        assert b"\n" not in data

    def test_deterministic_across_calls(self):
        obj = {"b": [1, 2], "a": "x"}
        assert crypto.canonical_json(obj) == crypto.canonical_json(obj)

    def test_different_insertion_order_same_output(self):
        obj1 = {"a": 1, "b": 2}
        obj2 = {"b": 2, "a": 1}
        assert crypto.canonical_json(obj1) == crypto.canonical_json(obj2)


# ---------------------------------------------------------------------------
# 7. Serialize for signing
# ---------------------------------------------------------------------------

class TestSerializeForSigning:
    def test_excludes_signature_field(self):
        obj = {"from": "addr", "fee": 10, "signature": "abc123"}
        result = json.loads(crypto.serialize_for_signing(obj))
        assert "signature" not in result

    def test_includes_all_other_fields(self):
        obj = {"from": "addr", "fee": 10, "signature": "abc123", "nonce": 1}
        result = json.loads(crypto.serialize_for_signing(obj))
        assert "from" in result
        assert "fee" in result
        assert "nonce" in result

    def test_returns_bytes(self):
        assert isinstance(crypto.serialize_for_signing({"a": 1}), bytes)

    def test_idempotent_without_signature(self):
        obj = {"from": "addr", "fee": 10}
        r1 = crypto.serialize_for_signing(obj)
        r2 = crypto.serialize_for_signing(obj)
        assert r1 == r2


# ---------------------------------------------------------------------------
# 8. Key storage: save / load / decrypt
# ---------------------------------------------------------------------------

class TestKeyStorage:
    def test_save_and_load_pubkey(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="testpass")
        loaded_pk = crypto.load_pubkey(path)
        assert loaded_pk == pk

    def test_file_permissions_are_0600(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="testpass")
        mode = oct(os.stat(path).st_mode)[-3:]
        assert mode == "600"

    def test_decrypt_with_correct_passphrase(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="correctpass")
        recovered = crypto.decrypt_secret_key(path, passphrase="correctpass")
        assert recovered == sk

    def test_decrypt_with_wrong_passphrase_raises(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="correctpass")
        with pytest.raises(ValueError, match="wrong passphrase"):
            crypto.decrypt_secret_key(path, passphrase="wrongpass")

    def test_save_without_passphrase_raises(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        with pytest.raises(ValueError, match="passphrase"):
            crypto.save_key(path, sk, pk, passphrase="")

    def test_derive_kek_and_decrypt(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="kekpass")
        kek = crypto.derive_kek(path, "kekpass")
        assert isinstance(kek, bytes)
        assert len(kek) == 32
        recovered = crypto.decrypt_secret_key(path, kek=kek)
        assert recovered == sk

    def test_sign_with_keyfile(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="signpass")
        kek = crypto.derive_kek(path, "signpass")
        msg = b"sign me"
        sig = crypto.sign_with_keyfile(msg, path, kek)
        assert crypto.verify(msg, sig, pk) is True

    def test_key_file_is_valid_json(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="testpass")
        with open(path) as f:
            data = json.load(f)
        assert "public_key" in data
        assert "ciphertext" in data
        assert "salt" in data

    def test_derive_kek_without_passphrase_raises(self, tmp_path, sk, pk):
        path = str(tmp_path / "key.json")
        crypto.save_key(path, sk, pk, passphrase="testpass")
        with pytest.raises(ValueError, match="passphrase"):
            crypto.derive_kek(path, "")


@pytest.mark.real_kdf
def test_key_files_are_written_with_the_production_argon2id_cost(tmp_path):
    """The suite lowers the key-derivation cost for speed (see conftest).
    This one keeps the real cost, so weakening it in crypto.py is caught."""
    import nacl.pwhash
    path = tmp_path / "key.json"
    crypto.save_key(str(path), b"s" * 32, b"p" * 32, "passphrase")
    data = json.loads(path.read_text())
    assert data["ops"] == nacl.pwhash.argon2id.OPSLIMIT_MODERATE
    assert data["mem"] == nacl.pwhash.argon2id.MEMLIMIT_MODERATE
