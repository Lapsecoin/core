"""Reading other networks, including through the light client's proxy."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import evm
import gas
import gas_io
import relay

OP = gas.NETWORKS["optimism"]
PROXY = "socks5h://127.0.0.1:9050"


def test_reads_go_through_the_proxy_when_there_is_one(monkeypatch):
    seen = []
    monkeypatch.setattr(evm, "get_balance_wei", lambda url, a: seen.append(dict(evm._session.proxies)) or 1)
    monkeypatch.setattr(relay, "price_usd", lambda net, key="": seen.append(dict(relay._session.proxies)) or 2.0)
    monkeypatch.setattr(evm, "gas_price_wei", lambda url: seen.append(dict(evm._session.proxies)) or 3)
    io = gas_io.ChainIO(proxy=lambda: PROXY)
    io.dest_balance(OP, "0x" + "1" * 40)
    io.price(OP)
    io.gas_price(OP)
    assert seen == [{"http": PROXY, "https": PROXY}] * 3


def test_without_a_proxy_nothing_is_routed(monkeypatch):
    evm._session.proxies = {"http": PROXY}
    monkeypatch.setattr(evm, "get_balance_wei", lambda url, a: 1)
    gas_io.ChainIO().dest_balance(OP, "0x" + "1" * 40)
    assert evm._session.proxies == {}


def test_the_proxy_is_asked_each_time_so_tor_can_come_and_go(monkeypatch):
    urls = iter([PROXY, None])
    seen = []
    monkeypatch.setattr(evm, "get_balance_wei", lambda url, a: seen.append(dict(evm._session.proxies)) or 1)
    io = gas_io.ChainIO(proxy=lambda: next(urls))
    io.dest_balance(OP, "0x" + "1" * 40)
    io.dest_balance(OP, "0x" + "1" * 40)
    assert seen == [{"http": PROXY, "https": PROXY}, {}]


def test_base_uses_the_public_endpoint_and_relay_needs_no_key():
    io = gas_io.ChainIO()
    assert io.base_rpc() == evm.DEFAULT_BASE_RPC and io.relay_key() == ""


def test_only_evm_destinations_are_screened(monkeypatch):
    calls = []
    monkeypatch.setattr(gas_io.sanctions, "is_sanctioned", lambda a: calls.append(a) or True)
    io = gas_io.ChainIO()
    assert io.sanctioned(OP, "0x" + "1" * 40) is True
    assert io.sanctioned(gas.NETWORKS["solana"], "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK") is False
    assert calls == ["0x" + "1" * 40]


def test_solana_balance_comes_from_its_own_call(monkeypatch):
    calls = []
    monkeypatch.setattr(evm, "rpc", lambda url, m, p=None: calls.append((m, p)) or {"value": 7})
    assert gas_io.ChainIO().dest_balance(gas.NETWORKS["solana"], "addr") == 7
    assert calls == [("getBalance", ["addr"])]
