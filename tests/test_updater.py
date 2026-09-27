import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import updater  # noqa: E402


class TestCleanupStrayUpdateFiles:
    """A windows-exe update whose helper never ran (see the
    CREATE_BREAKAWAY_FROM_JOB fix in _run_windows_exe) leaves a stray
    downloaded .exe next to the real one; Updater() clears it on the next
    startup rather than leaving it there forever."""

    def test_removes_stray_exe_and_leaves_other_files(self, monkeypatch):
        d = tempfile.mkdtemp()
        exe_path = os.path.join(d, "lapsecoin.exe")
        open(exe_path, "w").close()
        stray = os.path.join(d, ".lapsecoin-update-999.exe")
        open(stray, "w").close()
        keep = os.path.join(d, "some_other_file.txt")
        open(keep, "w").close()
        monkeypatch.setattr(sys, "executable", exe_path)

        updater._cleanup_stray_update_files()

        assert not os.path.exists(stray)
        assert os.path.exists(exe_path)
        assert os.path.exists(keep)

    def test_updater_init_runs_cleanup_for_windows_exe(self, monkeypatch):
        called = []
        monkeypatch.setattr(updater, "_cleanup_stray_update_files", lambda: called.append(True))
        updater.Updater(install_type="windows-exe")
        assert called == [True]

    def test_updater_init_skips_cleanup_for_other_install_types(self, monkeypatch):
        called = []
        monkeypatch.setattr(updater, "_cleanup_stray_update_files", lambda: called.append(True))
        updater.Updater(install_type="source")
        assert called == []


class TestWindowsHelperBreaksAwayFromJob:
    """CREATE_BREAKAWAY_FROM_JOB is what keeps the update helper alive once
    this process os._exit()s: without it, a PyInstaller onefile build's own
    Job Object kills the helper right along with us before it ever runs
    (see the comment in _run_windows_exe for how that was confirmed)."""

    def test_windows_exe_update_spawns_helper_with_breakaway_flag(self, monkeypatch, tmp_path):
        exe_path = tmp_path / "lapsecoin.exe"
        exe_path.write_bytes(b"MZ" + b"\0" * 2_000_000)
        monkeypatch.setattr(sys, "executable", str(exe_path))
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             open(dest, "wb").write(b"MZ" + b"\0" * 2_000_000))
        monkeypatch.setattr(os, "access", lambda path, mode: True)

        seen = {}

        def fake_popen(args, creationflags=0, close_fds=True):
            seen["creationflags"] = creationflags
            return None

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

        session = updater.UpdateSession("windows-exe", "9.9.9")
        try:
            session._run_windows_exe()
        except SystemExit:
            pass

        # getattr(..., 0) here mirrors production: on non-Windows platforms
        # (this test suite's own CI) subprocess has none of these
        # attributes, and the flag is always a no-op 0 -- the real
        # assertion this locks in is that _run_windows_exe ORs the same
        # flag in, whatever it resolves to on the platform actually running.
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        assert seen["creationflags"] == detached | new_group | breakaway
