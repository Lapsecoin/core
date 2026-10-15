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


def test_a_library_that_does_not_reproduce_the_published_key_is_refused(monkeypatch):
    monkeypatch.setattr(falcon_seeded, "_KAT_PUBLIC_KEY_SHA256", "0" * 64)
    lib = falcon_seeded._load()
    with pytest.raises(RuntimeError, match="published key"):
        falcon_seeded._self_test(lib)


def test_it_builds_itself_from_the_source_when_no_library_is_found(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("cc") and not shutil.which("gcc"):
        pytest.skip("no C compiler")
    monkeypatch.setattr(falcon_seeded, "_search_dirs", lambda: [str(tmp_path / "nothing")])
    monkeypatch.setattr(falcon_seeded, "_cache_dir", lambda: str(tmp_path / "cache"))
    monkeypatch.setattr(falcon_seeded, "_lib", None)
    # the search finds nothing, so it compiles into the cache and then needs the cache searched
    monkeypatch.setattr(falcon_seeded, "_search_dirs", lambda: [str(tmp_path / "nothing"), str(tmp_path / "cache")])
    assert falcon_seeded.available()
    assert (tmp_path / "cache").is_dir() and any((tmp_path / "cache").iterdir())
