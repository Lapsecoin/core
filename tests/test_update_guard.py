"""update_guard: a downloaded build is started and checked before it replaces
the working one, and a build that does not stay up is replaced by the one before
it. Both are about not leaving the user with a program that will not start."""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import update_guard as guard  # noqa: E402


def _program(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestSmokeTest:
    def test_a_build_that_says_it_is_fine_passes(self, tmp_path):
        p = _program(tmp_path / "app", 'echo "lapsecoin 9.9.9 self-test ok"')
        guard.smoke_test(p, "9.9.9")

    def test_a_build_that_exits_nonzero_is_refused_with_what_it_said(self, tmp_path):
        p = _program(tmp_path / "app", 'echo "libtcl8.6.so: GLIBC_2.42 not found"; exit 1')
        with pytest.raises(RuntimeError) as e:
            guard.smoke_test(p, "9.9.9")
        assert "GLIBC_2.42" in str(e.value) and "not installed" in str(e.value)

    def test_exiting_zero_without_saying_so_is_not_enough(self, tmp_path):
        p = _program(tmp_path / "app", "exit 0")
        with pytest.raises(RuntimeError):
            guard.smoke_test(p)

    def test_a_build_that_hangs_is_refused(self, tmp_path):
        p = _program(tmp_path / "app", "sleep 30")
        with pytest.raises(RuntimeError, match="did not finish"):
            guard.smoke_test(p, timeout=1)

    def test_a_file_that_cannot_be_run_is_refused(self, tmp_path):
        f = tmp_path / "app"
        f.write_bytes(b"\x7fELF not really")
        f.chmod(0o755)
        with pytest.raises(RuntimeError):
            guard.smoke_test(str(f))

    def test_the_parents_unpacked_files_are_not_handed_down(self):
        env = guard.clean_env({"PATH": "/bin", "_MEIPASS2": "/tmp/x", "TCL_LIBRARY": "/t", "APPIMAGE": "/a", "HOME": "/h"})
        assert env == {"PATH": "/bin", "HOME": "/h"}


class TestRollback:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        a = tmp_path / "lapsecoin"
        a.write_bytes(b"new build")
        monkeypatch.setattr(sys, "argv", [str(a), "--no-gui"])
        return a

    def _timer(self, started):
        class T:
            def __init__(self, delay, fn, args=()):
                self.delay, self.fn, self.args, self.daemon = delay, fn, args, False

            def start(self):
                started.append(self)
        return T

    def test_prepare_keeps_the_old_build_and_marks_the_new_one_unproven(self, tmp_path):
        a = tmp_path / "lapsecoin"
        a.write_bytes(b"old build")
        guard.prepare(str(a), "9.9.9")
        prev, marker = guard.paths(str(a))
        assert open(prev, "rb").read() == b"old build"
        assert json.load(open(marker)) == {"version": "9.9.9", "attempts": 0}

    def test_nothing_happens_outside_an_appimage_or_without_a_marker(self, app):
        assert guard.check_at_start({}) is None
        assert guard.check_at_start({"APPIMAGE": str(app)}) is None

    def test_the_first_start_of_an_updated_build_starts_its_proving_period(self, app):
        guard.prepare(str(app), "9.9.9")
        started = []
        got = guard.check_at_start({"APPIMAGE": str(app)}, execv=lambda *a: pytest.fail("no rollback yet"),
                                   timer=self._timer(started))
        assert got == "armed"
        assert json.load(open(guard.paths(str(app))[1]))["attempts"] == 1
        (t,) = started
        assert t.delay == guard.CONFIRM_AFTER_SECONDS and t.daemon
        t.fn(*t.args)    # still running when it fires: the marker goes
        assert not os.path.exists(guard.paths(str(app))[1])

    def test_a_build_that_did_not_stay_up_is_replaced_by_the_previous_one(self, tmp_path, app):
        app.write_bytes(b"old build")
        guard.prepare(str(app), "9.9.9")
        app.write_bytes(b"new build")         # the update replaced it
        guard.check_at_start({"APPIMAGE": str(app)}, execv=lambda *a: None, timer=self._timer([]))   # first start
        execs = []
        got = guard.check_at_start({"APPIMAGE": str(app)}, execv=lambda p, a: execs.append((p, a)))   # second
        assert got == "rolled-back"
        assert app.read_bytes() == b"old build"
        assert execs == [(str(app), [str(app), "--no-gui"])]
        assert not os.path.exists(guard.paths(str(app))[1]), "it is not tried again"

    def test_with_no_previous_copy_there_is_nothing_to_roll_back_to(self, app):
        _, marker = guard.paths(str(app))
        json.dump({"version": "9.9.9", "attempts": 1}, open(marker, "w"))
        assert guard.check_at_start({"APPIMAGE": str(app)}, execv=lambda *a: pytest.fail("nothing to run")) is None
        assert app.read_bytes() == b"new build" and not os.path.exists(marker)

    def test_a_marker_that_is_not_json_is_ignored(self, app):
        open(guard.paths(str(app))[1], "w").write("{not json")
        assert guard.check_at_start({"APPIMAGE": str(app)}) is None


class TestRunSelfTest:
    def test_says_ok_when_tkinter_loads(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "tkinter", type(sys)("tkinter"))
        assert guard.run_self_test() == 0
        assert guard.SELF_TEST_OK in capsys.readouterr().out

    def test_fails_and_says_why_when_tkinter_cannot_load(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "tkinter", None)      # makes the import raise ImportError
        assert guard.run_self_test() == 1
        out = capsys.readouterr().out
        assert "FAILED" in out and "tkinter" in out and guard.SELF_TEST_OK not in out
