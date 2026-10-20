import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import crashlog  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setitem(crashlog._state, "installed", False)
    monkeypatch.setitem(crashlog._state, "dir", None)
    monkeypatch.setitem(crashlog._state, "crash", None)
    monkeypatch.setenv("LAPSECOIN_NO_DIALOGS", "1")


class TestWhereItWrites:
    def test_the_first_writable_folder_wins(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        assert crashlog.pick_dir([str(a), str(b)]) == str(a)
        assert a.is_dir()

    def test_an_unwritable_folder_is_skipped(self, tmp_path):
        blocked = tmp_path / "file"
        blocked.write_text("x")                      # a file where a folder must go
        good = tmp_path / "good"
        assert crashlog.pick_dir([str(blocked / "sub"), str(good)]) == str(good)

    def test_nowhere_writable_is_none(self, tmp_path):
        blocked = tmp_path / "file"
        blocked.write_text("x")
        assert crashlog.pick_dir([str(blocked / "sub")]) is None

    def test_the_candidates_start_with_the_profile_folder(self, tmp_path):
        got = crashlog.candidate_dirs({"USERPROFILE": str(tmp_path)})
        assert got[0] == str(tmp_path / "LapseCoin") and got[1] == str(tmp_path)


class TestNothingChangesOutsideAnInstalledBuild:
    def test_install_does_nothing_on_linux(self, monkeypatch):
        monkeypatch.delenv("LAPSECOIN_CRASHLOG", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        before = sys.excepthook
        crashlog.install()
        assert sys.excepthook is before and not crashlog._state["installed"]


class TestTheReport:
    def _install(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LAPSECOIN_CRASHLOG", "1")
        monkeypatch.setattr(crashlog, "candidate_dirs", lambda environ=None: [str(tmp_path / "out")])
        before = (sys.excepthook, crashlog.threading.excepthook)
        crashlog.install()
        return before

    def test_install_makes_the_start_log_and_the_hooks(self, monkeypatch, tmp_path):
        before = self._install(monkeypatch, tmp_path)
        try:
            assert (tmp_path / "out" / crashlog.START_LOG).exists()
            assert (tmp_path / "out" / crashlog.CRASH_LOG).exists()
            assert sys.excepthook is crashlog._excepthook
            crashlog.breadcrumb("data folder ready")
            assert "data folder ready" in (tmp_path / "out" / crashlog.START_LOG).read_text()
        finally:
            sys.excepthook, crashlog.threading.excepthook = before
            crashlog._state["crash"].close()

    def test_a_fatal_exception_is_written_with_the_environment(self, monkeypatch, tmp_path):
        before = self._install(monkeypatch, tmp_path)
        try:
            try:
                raise PermissionError("cannot open lapsecoin.log")
            except PermissionError as e:
                crashlog.report_fatal(type(e), e, e.__traceback__)
            text = (tmp_path / "out" / crashlog.CRASH_LOG).read_text()
            assert "PermissionError: cannot open lapsecoin.log" in text
            assert "fatal exception during start-up" in text and "LapseCoin " in text
        finally:
            sys.excepthook, crashlog.threading.excepthook = before
            crashlog._state["crash"].close()

    def test_the_crash_test_flag_leaves_a_report(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "--crash-test"])
        monkeypatch.setenv("LAPSECOIN_CRASHLOG", "1")
        monkeypatch.setattr(crashlog, "candidate_dirs", lambda environ=None: [str(tmp_path / "out")])
        before = (sys.excepthook, crashlog.threading.excepthook)
        try:
            with pytest.raises(RuntimeError, match="crash test"):
                crashlog.install()
            sys.excepthook(RuntimeError, RuntimeError("crash test requested"), None)
            assert "crash test" in (tmp_path / "out" / crashlog.CRASH_LOG).read_text()
        finally:
            sys.excepthook, crashlog.threading.excepthook = before
            crashlog._state["crash"].close()

    def test_a_thread_failure_is_logged_and_not_shown(self, monkeypatch, tmp_path):
        before = self._install(monkeypatch, tmp_path)
        try:
            import threading
            def boom():
                raise ValueError("worker failed")
            t = threading.Thread(target=boom, name="worker")
            t.start(); t.join()
            assert "worker failed" in (tmp_path / "out" / crashlog.CRASH_LOG).read_text()
        finally:
            sys.excepthook, crashlog.threading.excepthook = before
            crashlog._state["crash"].close()


class TestNeverWaitsForAClick:
    @pytest.mark.parametrize("flag", crashlog.NO_WINDOW_FLAGS)
    def test_headless_and_help_runs_are_not_interactive(self, monkeypatch, flag):
        monkeypatch.delenv("LAPSECOIN_NO_DIALOGS", raising=False)
        monkeypatch.setattr(sys, "argv", ["lapsecoin", flag])
        assert crashlog.interactive() is False

    def test_a_plain_start_is_interactive(self, monkeypatch):
        monkeypatch.delenv("LAPSECOIN_NO_DIALOGS", raising=False)
        monkeypatch.setattr(sys, "argv", ["lapsecoin"])
        assert crashlog.interactive() is True


class TestTheGitHubIssueLink:
    def test_the_link_carries_the_report(self):
        url = crashlog.issue_url("Crash: X", "line one\nline two")
        assert url.startswith(crashlog.ISSUE_URL + "?title=Crash%3A%20X&body=line%20one%0Aline%20two")

    def test_a_long_report_is_cut_in_the_middle(self):
        body = "A" * 20000 + "THE-END"
        url = crashlog.issue_url("t", body)
        assert len(url) < 12000 and "THE-END" in url and "cut" in url

    def test_the_account_name_is_taken_out_of_paths(self, monkeypatch):
        env = {"USERPROFILE": r"C:\Users\Vic", "USERNAME": "Vic", "LOCALAPPDATA": r"C:\Users\Vic\AppData\Local"}
        out = crashlog.sanitize(r"open C:\Users\Vic\AppData\Local\x failed for Vic", env)
        assert "Vic" not in out and "%LOCALAPPDATA%" in out
