"""The sanctions oracle call."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import evm
import sanctions

ADDR = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"


def test_it_calls_is_sanctioned_on_the_oracle_with_the_padded_address(monkeypatch):
    seen = []
    monkeypatch.setattr(evm, "rpc", lambda url, m, p=None: seen.append((url, m, p)) or "0x" + "0" * 63 + "1")
    assert sanctions.is_sanctioned(ADDR) is True
    url, method, params = seen[0]
    assert (url, method) == (sanctions.RPC, "eth_call")
    assert params[0]["to"] == sanctions.ORACLE and params[1] == "latest"
    assert params[0]["data"] == "0xdf592f7d" + "0" * 24 + ADDR[2:].lower()


def test_zero_means_not_listed(monkeypatch):
    monkeypatch.setattr(evm, "rpc", lambda *a, **k: "0x" + "0" * 64)
    assert sanctions.is_sanctioned(ADDR) is False


@pytest.mark.parametrize("junk", [None, "", "nope", "0x"])
def test_an_unreadable_answer_is_an_error_not_a_pass(monkeypatch, junk):
    monkeypatch.setattr(evm, "rpc", lambda *a, **k: junk)
    with pytest.raises(evm.EVMUnreachable):
        sanctions.is_sanctioned(ADDR)


def test_an_unreachable_oracle_raises(monkeypatch):
    def down(*a, **k):
        raise evm.EVMUnreachable("down")
    monkeypatch.setattr(evm, "rpc", down)
    with pytest.raises(evm.EVMUnreachable):
        sanctions.is_sanctioned(ADDR)
