import os
import sys

import pytest

import datadir


def _write(directory, name, text="x"):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w") as f:
        f.write(text)


class TestMigrate:
    def test_copies_the_data_and_leaves_the_originals(self, tmp_path):
        src, dst = str(tmp_path / "old"), str(tmp_path / "new")
        _write(src, "lapsecoin_key.json", "key")
        _write(src, "lapsecoin_chain.db", "chain")
        _write(src, "unrelated.txt")
        got = datadir.migrate(src, dst)
        assert set(got) == {"lapsecoin_key.json", "lapsecoin_chain.db"}
        assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "key"
        assert os.path.exists(os.path.join(src, "lapsecoin_key.json"))
        assert not os.path.exists(os.path.join(dst, "unrelated.txt"))

    def test_never_overwrites_what_is_already_there(self, tmp_path):
        src, dst = str(tmp_path / "old"), str(tmp_path / "new")
        _write(src, "lapsecoin_key.json", "old key")
        _write(dst, "lapsecoin_key.json", "new key")
        assert datadir.migrate(src, dst) == []
        assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "new key"

    def test_a_chain_with_unflushed_writes_is_left_behind_but_the_key_comes(self, tmp_path):
        src, dst = str(tmp_path / "old"), str(tmp_path / "new")
        _write(src, "lapsecoin_key.json")
        _write(src, "lapsecoin_chain.db")
        _write(src, "lapsecoin_chain.db-wal", "pending")
        assert datadir.migrate(src, dst) == ["lapsecoin_key.json"]

    def test_same_folder_or_missing_source_is_a_no_op(self, tmp_path):
        _write(str(tmp_path), "lapsecoin_key.json")
        assert datadir.migrate(str(tmp_path), str(tmp_path)) == []
        assert datadir.migrate(str(tmp_path / "nope"), str(tmp_path / "x")) == []


class TestPrepare:
    def _installed(self, monkeypatch, tmp_path):
        exe_dir = tmp_path / "Programs"
        _write(str(exe_dir), datadir.MSI_MARKER)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(exe_dir / "lapsecoin.exe"))
        return {"LOCALAPPDATA": str(tmp_path / "Local")}

    def test_not_an_installed_build_changes_nothing(self, tmp_path):
        before = os.getcwd()
        assert datadir.prepare({"LOCALAPPDATA": str(tmp_path)}) is None
        assert os.getcwd() == before

    def test_an_installed_build_works_in_its_data_folder(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        before = os.getcwd()
        try:
            got = datadir.prepare(env, home=str(tmp_path / "home"))
            assert got == str(tmp_path / "Local" / "LapseCoin")
            assert os.path.samefile(os.getcwd(), got)
        finally:
            os.chdir(before)

    def test_an_empty_data_folder_picks_up_a_key_left_in_downloads(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        home = tmp_path / "home"
        _write(str(home / "Downloads"), "lapsecoin_key.json", "key")
        before = os.getcwd()
        try:
            dst = datadir.prepare(env, home=str(home))
            assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "key"
        finally:
            os.chdir(before)

    def test_a_data_folder_with_a_key_is_not_touched_again(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        home = tmp_path / "home"
        _write(str(home / "Downloads"), "lapsecoin_key.json", "stale")
        _write(str(tmp_path / "Local" / "LapseCoin"), "lapsecoin_key.json", "mine")
        before = os.getcwd()
        try:
            dst = datadir.prepare(env, home=str(home))
            assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "mine"
        finally:
            os.chdir(before)
