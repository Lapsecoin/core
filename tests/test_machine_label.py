"""What a node declares in its blocks about the machine that built them."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import hardware_info
import node as node_mod
import settings as settings_mod


class _Meta:
    def __init__(self):
        self.data = {}

    def get_meta(self, key, default=None):
        return self.data.get(key, default)

    def set_meta(self, key, value):
        self.data[key] = str(value)


def _node(share, addr="addr1", meta=None, monkeypatch=None):
    meta = meta if meta is not None else _Meta()
    fake = SimpleNamespace(
        addr=addr, storage=meta,
        settings=SimpleNamespace(get=lambda s: share if s is settings_mod.PUBLISH_MACHINE else None),
        _MACHINE_SALT_META_KEY=node_mod.Node._MACHINE_SALT_META_KEY)
    fake._machine_salt = lambda: node_mod.Node._machine_salt(fake)
    fake.label = lambda: node_mod.Node.machine_label(fake)
    return fake


def _cpu(monkeypatch, model):
    monkeypatch.setattr(hardware_info, "machine_label", lambda max_chars=120: model)


def test_sharing_declares_the_cpu_model(monkeypatch):
    _cpu(monkeypatch, "AMD Ryzen 9 7950X")
    assert _node(True).label() == "AMD Ryzen 9 7950X"


def test_not_sharing_declares_an_opaque_id_never_the_model(monkeypatch):
    _cpu(monkeypatch, "AMD Ryzen 9 7950X")
    label = _node(False).label()
    assert label.startswith(block_mod.PRIVATE_MACHINE_PREFIX)
    assert "Ryzen" not in label and "AMD" not in label


def test_the_id_is_stable_for_this_machine_across_restarts(monkeypatch):
    _cpu(monkeypatch, "AMD Ryzen 9 7950X")
    meta = _Meta()
    assert _node(False, meta=meta).label() == _node(False, meta=meta).label()


def test_two_machines_with_the_same_cpu_and_address_get_different_ids(monkeypatch):
    _cpu(monkeypatch, "AMD Ryzen 9 7950X")
    assert _node(False).label() != _node(False).label()      # each has its own salt


def test_the_id_follows_the_address_and_the_cpu(monkeypatch):
    meta = _Meta()
    _cpu(monkeypatch, "CPU A")
    a = _node(False, addr="addr1", meta=meta).label()
    assert _node(False, addr="addr2", meta=meta).label() != a
    _cpu(monkeypatch, "CPU B")
    assert _node(False, addr="addr1", meta=meta).label() != a


def test_an_unreadable_cpu_still_gets_an_id_so_blocks_are_never_unattributable(monkeypatch):
    _cpu(monkeypatch, "")
    assert _node(True).label().startswith(block_mod.PRIVATE_MACHINE_PREFIX)


def test_a_damaged_salt_is_replaced_not_trusted(monkeypatch):
    _cpu(monkeypatch, "CPU")
    meta = _Meta()
    meta.set_meta("machine_salt", "not hex")
    first = _node(False, meta=meta).label()
    assert first == _node(False, meta=meta).label()          # replaced once, then stable
    assert len(bytes.fromhex(meta.get_meta("machine_salt"))) >= 16
