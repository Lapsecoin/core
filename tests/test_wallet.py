import crypto
import tx as tx_mod
from wallet import Wallet


def _wallet(tmp_path):
    sk, pk = crypto.generate_keypair()
    path = str(tmp_path / "key.json")
    crypto.save_key(path, sk, pk, "pw")
    return Wallet(path, pk), path


def test_identity_matches_public_key(tmp_path):
    w, _ = _wallet(tmp_path)
    assert w.addr == crypto.public_key_to_address(w.pk)
    assert w.pk_hex == w.pk.hex()


def test_sign_tx_with_passphrase_verifies(tmp_path):
    w, _ = _wallet(tmp_path)
    outputs = [{"to": crypto.burn_address(), "amount": 1}]
    t, fee = w.sign_tx(outputs, nonce=1, fee=3, memo="hi", passphrase="pw")
    assert fee == 3
    assert t["from"] == w.addr and t["nonce"] == 1 and t["memo"] == "hi"
    ok, _ = tx_mod._check_signature(t)
    assert ok


def test_sign_tx_with_kek_verifies(tmp_path):
    w, path = _wallet(tmp_path)
    kek = crypto.derive_kek(path, "pw")
    outputs = [{"to": crypto.burn_address(), "amount": 1}]
    t, _ = w.sign_tx(outputs, nonce=2, fee=1, kek=kek)
    ok, _ = tx_mod._check_signature(t)
    assert ok


def test_wrong_passphrase_refused(tmp_path):
    import pytest
    w, _ = _wallet(tmp_path)
    with pytest.raises(ValueError):
        w.sign_tx([], nonce=1, fee=1, passphrase="nope")
