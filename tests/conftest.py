"""Shared pytest fixtures for the whole suite."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _fee_locks_active(monkeypatch):
    """Fee request locks apply from block 0 in tests, so each test reads the
    rules without building a chain up to the real activation height. The
    activation tests set their own."""
    import params
    monkeypatch.setattr(params, "GAS_LOCK_ACTIVATION_HEIGHT", 0)


@pytest.fixture(autouse=True)
def _cheap_key_derivation(request, monkeypatch):
    """Key files are written and unlocked with the production Argon2id
    cost, about 0.75s a time, which dominated the suite: most tests only
    need a key file, not a slow one. The cost is stored in the file, so
    what is written here still loads either way. Tests that check the
    production cost itself are marked real_kdf and keep it."""
    if request.node.get_closest_marker("real_kdf"):
        return
    import nacl.pwhash
    import crypto
    monkeypatch.setattr(crypto, "_OPS", nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE)
    monkeypatch.setattr(crypto, "_MEM", nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE)
