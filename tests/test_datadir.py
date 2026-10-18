import os
import sys

import pytest

import datadir
from tests.legacy_key import save_key


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

    def test_a_chain_still_open_in_a_running_node_is_copied_whole(self, tmp_path):
        import sqlite3
        src, dst = str(tmp_path / "old"), str(tmp_path / "new")
        os.makedirs(src)
        _write(src, "lapsecoin_key.json")
        live = sqlite3.connect(os.path.join(src, "lapsecoin_chain.db"))
        live.execute("PRAGMA journal_mode=wal")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE blocks (height INTEGER)")
        live.executemany("INSERT INTO blocks VALUES (?)", [(i,) for i in range(500)])
        live.commit()
        assert os.path.getsize(os.path.join(src, "lapsecoin_chain.db-wal")) > 0
        got = datadir.migrate(src, dst)
        live.close()
        assert "lapsecoin_chain.db" in got and "lapsecoin_key.json" in got
        copy = sqlite3.connect(os.path.join(dst, "lapsecoin_chain.db"))
        assert copy.execute("SELECT count(*) FROM blocks").fetchone()[0] == 500
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        copy.close()
        assert not os.path.exists(os.path.join(dst, "lapsecoin_chain.db-wal"))

    def test_a_file_that_is_not_a_database_is_still_copied_as_is(self, tmp_path):
        src, dst = str(tmp_path / "old"), str(tmp_path / "new")
        _write(src, "lapsecoin_chain.db", "not sqlite")
        assert "lapsecoin_chain.db" in datadir.migrate(src, dst)
        assert open(os.path.join(dst, "lapsecoin_chain.db")).read() == "not sqlite"

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
        return {"LOCALAPPDATA": str(tmp_path / "Local"), "USERPROFILE": str(tmp_path / "Profile")}

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
        assert datadir.prepare({"LOCALAPPDATA": str(tmp_path), "USERPROFILE": str(tmp_path)}) is None
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
        _write(str(tmp_path / "Profile" / "LapseCoin"), "lapsecoin_key.json", "mine")
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


class TestOldInstallMovesToARecoveryPhrase:
    """The single .exe's folder is copied into the installed build's data
    folder; its lapsecoin_key.json there is what makes the node move to a
    recovery phrase at its first start."""

    def _old_install(self, folder):
        import crypto
        sk, pk = crypto.generate_keypair()
        save_key(os.path.join(folder, "lapsecoin_key.json"), sk, pk, "old passphrase")
        _write(folder, "base_gas.key", "gas")
        return crypto.public_key_to_address(pk)

    def test_the_old_key_is_found_and_copied_and_means_move_to_a_phrase(self, tmp_path, monkeypatch):
        import wallet
        old, new = tmp_path / "Downloads" / "lapsecoin", tmp_path / "data"
        old.mkdir(parents=True)
        self._old_install(str(old))
        assert datadir.find_legacy_folders(str(tmp_path)) == [str(old)]
        copied = datadir.migrate(str(old), str(new))
        assert {"lapsecoin_key.json", "base_gas.key"} <= set(copied)
        source = wallet.find_source(wallet_file=str(new / "lapsecoin_wallet.json"),
                                    legacy_file=str(new / "lapsecoin_key.json"))
        assert source.kind == "legacy"

    def test_a_folder_with_only_a_recovery_phrase_wallet_is_found_and_copied_too(self, tmp_path):
        old, new = tmp_path / "Desktop" / "ln", tmp_path / "data"
        old.mkdir(parents=True)
        _write(str(old), "lapsecoin_wallet.json", "{}")
        assert datadir.find_legacy_folders(str(tmp_path)) == [str(old)]
        assert "lapsecoin_wallet.json" in datadir.migrate(str(old), str(new))

    def test_the_old_folder_is_left_as_it_was(self, tmp_path):
        old, new = tmp_path / "old", tmp_path / "data"
        old.mkdir()
        self._old_install(str(old))
        datadir.migrate(str(old), str(new))
        assert (old / "lapsecoin_key.json").exists()      # a copy, never a move


class TestWhereTheDataLives:
    def _env(self, tmp_path):
        return {"USERPROFILE": str(tmp_path / "profile"), "LOCALAPPDATA": str(tmp_path / "local")}

    def test_the_profile_folder_is_used(self, tmp_path):
        env = self._env(tmp_path)
        assert datadir.user_data_dir(env) == str(tmp_path / "profile" / "LapseCoin")

    def test_the_old_folder_is_used_while_it_still_holds_the_data(self, tmp_path):
        env = self._env(tmp_path)
        _write(str(tmp_path / "local" / "LapseCoin"), "lapsecoin_wallet.json", "key")
        assert datadir.user_data_dir(env) == str(tmp_path / "local" / "LapseCoin")
        _write(str(tmp_path / "profile" / "LapseCoin"), "lapsecoin_wallet.json", "moved")
        assert datadir.user_data_dir(env) == str(tmp_path / "profile" / "LapseCoin")

    def test_not_packaged_off_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert datadir.is_packaged() is False


class TestMovingTheOldAppDataFolder:
    def _setup(self, monkeypatch, tmp_path):
        env = {"USERPROFILE": str(tmp_path / "profile"), "LOCALAPPDATA": str(tmp_path / "local")}
        old = tmp_path / "local" / "LapseCoin"
        _write(str(old), "lapsecoin_wallet.json", "key")
        _write(str(old), "lapsecoin_chain.db", "chain")
        _write(str(old), "settings.json", "{}")
        monkeypatch.setattr(datadir, "is_packaged", lambda: True)
        return env, old, tmp_path / "profile" / "LapseCoin"

    def _run(self, env, answer, tmp_path):
        asked = []
        before = os.getcwd()
        try:
            def ask_move(old, new):
                asked.append((old, new))
                return answer
            got = datadir.prepare(env, home=str(tmp_path / "home"), ask_move=ask_move,
                                  ask=lambda s: pytest.fail("the old-program question must not be asked"))
            return got, asked, os.getcwd()
        finally:
            os.chdir(before)

    def test_moving_puts_everything_in_the_profile_folder_and_removes_the_old_one(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        got, asked, cwd = self._run(env, "move", tmp_path)
        assert asked == [(str(old), str(new))]
        assert got == cwd == str(new)
        assert {p.name for p in new.iterdir()} == {"lapsecoin_wallet.json", "lapsecoin_chain.db", "settings.json"}
        assert (new / "lapsecoin_wallet.json").read_text() == "key"
        assert not old.exists()

    def test_not_now_runs_from_the_old_folder_and_moves_nothing(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        got, _, cwd = self._run(env, "later", tmp_path)
        assert got == cwd == str(old)
        assert (old / "lapsecoin_wallet.json").exists()
        assert not (new / "lapsecoin_wallet.json").exists()

    def test_closing_the_question_exits_without_making_a_key(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        with pytest.raises(SystemExit):
            self._run(env, "cancel", tmp_path)
        assert (old / "lapsecoin_wallet.json").exists()

    def test_a_move_that_fails_part_way_is_undone_and_the_old_folder_is_used(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        real = os.replace
        calls = []

        def flaky(s, d):
            calls.append(s)
            if len(calls) == 2 and str(s).startswith(str(old)):
                raise PermissionError("held open by a running node")
            return real(s, d)
        monkeypatch.setattr(os, "replace", flaky)
        got, _, cwd = self._run(env, "move", tmp_path)
        monkeypatch.setattr(os, "replace", real)
        assert got == cwd == str(old)
        assert {p.name for p in old.iterdir()} == {"lapsecoin_wallet.json", "lapsecoin_chain.db", "settings.json"}
        assert not any(new.iterdir()) if new.exists() else True

    def test_nothing_is_asked_once_the_new_folder_has_the_data(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        _write(str(new), "lapsecoin_wallet.json", "mine")
        got, asked, _ = self._run(env, "move", tmp_path)
        assert asked == [] and got == str(new)
        assert (old / "lapsecoin_wallet.json").exists()      # left alone

    def test_asking_for_help_neither_asks_nor_moves(self, monkeypatch, tmp_path):
        env, old, new = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "-h"])
        got, asked, _ = self._run(env, "move", tmp_path)
        assert asked == [] and got == str(old)
        assert (old / "lapsecoin_wallet.json").exists()

    def test_nothing_in_either_folder_goes_on_to_the_old_program_question(self, monkeypatch, tmp_path):
        env = {"USERPROFILE": str(tmp_path / "profile"), "LOCALAPPDATA": str(tmp_path / "local")}
        monkeypatch.setattr(datadir, "is_packaged", lambda: True)
        asked = []
        before = os.getcwd()
        try:
            got = datadir.prepare(env, home=str(tmp_path / "home"),
                                  ask_move=lambda o, n: pytest.fail("nothing to move"),
                                  ask=lambda s: asked.append(s) or ("fresh", None))
        finally:
            os.chdir(before)
        assert asked == [[]] and got == str(tmp_path / "profile" / "LapseCoin")

    def test_without_a_window_the_data_is_moved(self, monkeypatch):
        import migrate_ui
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "--no-gui"])
        assert migrate_ui.ask_move("/old", "/new") == "move"
