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
        stray_bat = os.path.join(tempfile.gettempdir(), "lapsecoin-update-999.bat")
        open(stray_bat, "w").close()
        keep = os.path.join(d, "some_other_file.txt")
        open(keep, "w").close()
        monkeypatch.setattr(sys, "executable", exe_path)

        updater._cleanup_stray_update_files()

        assert not os.path.exists(stray)
        assert not os.path.exists(stray_bat)
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
    """The helper must outlive the original process and retry the file swap
    until the EXE is no longer locked; otherwise the update leaves the new
    binary next to the original and never relaunches the app."""

    def test_windows_exe_update_spawns_retrying_batch_with_breakaway_flag(self, monkeypatch, tmp_path):
        exe_path = tmp_path / "lapsecoin.exe"
        exe_path.write_bytes(b"MZ" + b"\0" * 2_000_000)
        monkeypatch.setattr(sys, "executable", str(exe_path))
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             open(dest, "wb").write(b"MZ" + b"\0" * 2_000_000))
        monkeypatch.setattr(os, "access", lambda path, mode: True)

        seen = {}

        def fake_popen(args, creationflags=0, close_fds=True):
            seen["args"] = args
            seen["creationflags"] = creationflags
            seen["close_fds"] = close_fds
            return None

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        session = updater.UpdateSession("windows-exe", "9.9.9")
        session._run_windows_exe()

        assert session.stage == "ready"
        helper = open(tmp_path / "update.bat", encoding="ascii").read()
        assert "taskkill /PID" in helper
        assert "for /L %%N in (1,1,120)" in helper
        assert "move /Y" in helper
        assert "goto moved" in helper
        assert "start \"\"" not in helper
        assert "pause" in helper
        assert "Starting LapseCoin" not in helper
        assert "LapseCoin is closed" in helper
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        assert session.open_update_file()
        assert seen["args"][0:2] == ["explorer.exe", "/select," + str(tmp_path / "update.bat")]
        assert len(seen["args"]) == 2
        assert seen["creationflags"] == detached
