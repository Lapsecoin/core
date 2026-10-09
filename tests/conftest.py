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
