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


class TestFindLegacyFolders:
    def test_finds_keys_on_the_desktop_and_in_downloads_newest_first(self, tmp_path):
        old = tmp_path / "Downloads" / "lapsecoin"
        new = tmp_path / "Desktop"
        _write(str(old), "lapsecoin_key.json")
        _write(str(new), "lapsecoin_key.json")
        os.utime(old / "lapsecoin_key.json", (1000, 1000))
        assert datadir.find_legacy_folders(str(tmp_path)) == [str(new), str(old)]

    def test_goes_a_few_folders_deep_but_not_forever(self, tmp_path):
        _write(str(tmp_path / "Downloads" / "a" / "b" / "c"), "lapsecoin_key.json")
        _write(str(tmp_path / "Downloads" / "a" / "b" / "c" / "d" / "e"), "lapsecoin_key.json")
        assert datadir.find_legacy_folders(str(tmp_path)) == [
            str(tmp_path / "Downloads" / "a" / "b" / "c")]

    def test_nothing_found_is_an_empty_list(self, tmp_path):
        assert datadir.find_legacy_folders(str(tmp_path)) == []

    def test_the_search_is_bounded(self, tmp_path):
        for i in range(30):
            (tmp_path / "Downloads" / f"d{i}").mkdir(parents=True)
        _write(str(tmp_path / "Downloads" / "d29"), "lapsecoin_key.json")
        assert datadir.find_legacy_folders(str(tmp_path), max_dirs=5) == []

    def test_the_same_folder_is_not_listed_twice(self, tmp_path):
        _write(str(tmp_path / "Downloads"), "lapsecoin_key.json")
        assert datadir.find_legacy_folders(str(tmp_path)) == [str(tmp_path / "Downloads")]


class TestPrepare:
    def _installed(self, monkeypatch, tmp_path):
        exe_dir = tmp_path / "Programs"
        _write(str(exe_dir), datadir.MSI_MARKER)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(exe_dir / "lapsecoin.exe"))
        return {"LOCALAPPDATA": str(tmp_path / "Local")}

    def _run(self, env, home, answer, monkeypatch=None):
        asked = []
        before = os.getcwd()
        try:
            def ask(suggestions):
                asked.append(list(suggestions))
                return answer(suggestions) if callable(answer) else answer
            return datadir.prepare(env, home=home, ask=ask), asked
        finally:
            os.chdir(before)

    def test_not_an_installed_build_changes_nothing(self, tmp_path):
        before = os.getcwd()
        assert datadir.prepare({"LOCALAPPDATA": str(tmp_path)}) is None
        assert os.getcwd() == before

    def test_the_first_run_offers_what_it_found_and_copies_the_choice(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        home = tmp_path / "home"
        _write(str(home / "Downloads"), "lapsecoin_key.json", "key")
        dst, asked = self._run(env, str(home), lambda s: ("use", s[0]))
        assert asked == [[str(home / "Downloads")]]
        assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "key"
        assert os.path.exists(home / "Downloads" / "lapsecoin_key.json")

    def test_a_folder_the_user_browsed_to_is_copied_too(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        other = tmp_path / "somewhere" / "else"
        _write(str(other), "lapsecoin_key.json", "key")
        _write(str(other), "lapsecoin_chain.db", "chain")
        dst, _ = self._run(env, str(tmp_path / "home"), ("use", str(other)))
        assert open(os.path.join(dst, "lapsecoin_chain.db")).read() == "chain"

    def test_starting_fresh_copies_nothing(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        home = tmp_path / "home"
        _write(str(home / "Downloads"), "lapsecoin_key.json", "key")
        dst, _ = self._run(env, str(home), ("fresh", None))
        assert not os.path.exists(os.path.join(dst, "lapsecoin_key.json"))

    def test_closing_the_question_exits_without_making_a_key(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        before = os.getcwd()
        try:
            with pytest.raises(SystemExit):
                datadir.prepare(env, home=str(tmp_path), ask=lambda s: ("cancel", None))
        finally:
            os.chdir(before)

    def test_asking_for_help_does_not_ask_or_copy(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        home = tmp_path / "home"
        _write(str(home / "Downloads"), "lapsecoin_key.json", "key")
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "-h"])
        dst, asked = self._run(env, str(home), ("use", str(home / "Downloads")))
        assert asked == []
        assert not os.path.exists(os.path.join(dst, "lapsecoin_key.json"))

    def test_a_data_folder_with_a_key_is_never_asked_about(self, monkeypatch, tmp_path):
        env = self._installed(monkeypatch, tmp_path)
        _write(str(tmp_path / "Local" / "LapseCoin"), "lapsecoin_key.json", "mine")
        dst, asked = self._run(env, str(tmp_path / "home"), ("fresh", None))
        assert asked == []
        assert open(os.path.join(dst, "lapsecoin_key.json")).read() == "mine"


class TestTheQuestionWithoutAWindow:
    def test_no_gui_takes_the_newest_folder_found(self, monkeypatch):
        import migrate_ui
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "--no-gui"])
        assert migrate_ui.ask(["/new", "/old"]) == ("use", "/new")

    def test_no_gui_and_nothing_found_starts_fresh(self, monkeypatch):
        import migrate_ui
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "--no-gui"])
        assert migrate_ui.ask([]) == ("fresh", None)
