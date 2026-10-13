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
        monkeypatch.setattr(sys, "argv", [str(exe_path), "--no-gui", "--port", "9000"])
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             open(dest, "wb").write(b"MZ" + b"\0" * 2_000_000))
        monkeypatch.setattr(os, "access", lambda path, mode: True)

        # Inject fake environment variables to verify case-insensitive scrubbing
        monkeypatch.setenv("_meipass2", "fake_path")
        monkeypatch.setenv("_PYI_PROGNAME", "fake_prog")

        seen = {}

        def fake_popen(args, creationflags=0, close_fds=True, env=None, cwd=None):
            seen["args"] = args
            seen["creationflags"] = creationflags
            seen["close_fds"] = close_fds
            seen["env"] = env
            seen["cwd"] = cwd
            return None

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

        session = updater.UpdateSession("windows-exe", "9.9.9")
        try:
            session._run_windows_exe()
        except SystemExit:
            pass

        assert seen["args"][0:3] == ["cmd.exe", "/D", "/C"]
        assert len(seen["args"]) == 4
        helper = open(seen["args"][-1], encoding="ascii").read()
        assert "taskkill /PID" in helper
        assert "for /L %%N in (1,1,120)" in helper
        assert "move /Y" in helper
        assert "goto moved" in helper
        assert "timeout /T 2 /NOBREAK >nul" in helper
        assert 'start "" "' + str(exe_path) + '" --no-gui --port 9000' in helper
        assert '(goto) 2>nul & del "%~f0"' in helper

        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        assert seen["creationflags"] == detached | new_group | breakaway

        # Verify rigorous case-insensitive environment scrubbing
        env_dict = seen.get("env") or {}
        for key in env_dict.keys():
            assert not key.upper().startswith(("_MEI", "_PYI", "PYINSTALLER", "TCL", "TK"))

        # Verify execution isolation
        assert seen["cwd"] == str(exe_path.parent)

class TestWindowsMsiUpdate:
    OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    def test_a_marked_install_is_the_msi_type(self, monkeypatch, tmp_path):
        (tmp_path / "installed-by-msi").write_bytes(b"")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(tmp_path / "lapsecoin.exe"))
        monkeypatch.delenv("LAPSECOIN_DOCKER", raising=False)
        monkeypatch.delenv("APPIMAGE", raising=False)
        assert updater.detect_install_type() == "windows-msi"
        assert "windows-msi" in updater.SELF_UPDATABLE

    def test_an_unmarked_windows_build_stays_the_exe_type(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(tmp_path / "lapsecoin.exe"))
        monkeypatch.delenv("LAPSECOIN_DOCKER", raising=False)
        monkeypatch.delenv("APPIMAGE", raising=False)
        assert updater.detect_install_type() == "windows-exe"

    def test_only_a_real_installer_passes(self, tmp_path):
        good = tmp_path / "a.msi"
        good.write_bytes(self.OLE + b"\0" * 2_000_000)
        updater._verify_msi(str(good))
        for name, body in (("tiny.msi", self.OLE), ("exe.msi", b"MZ" + b"\0" * 2_000_000)):
            bad = tmp_path / name
            bad.write_bytes(body)
            try:
                updater._verify_msi(str(bad))
            except RuntimeError:
                continue
            raise AssertionError(f"{name} should have been refused")

    def test_the_checksum_is_read_and_enforced(self, tmp_path):
        f = tmp_path / "lapsecoin.msi"
        f.write_bytes(b"abc")
        import hashlib
        good = hashlib.sha256(b"abc").hexdigest()
        sums = f"{good}  lapsecoin.msi\n{'0' * 64}  lapsecoin.exe\n"
        assert updater._expected_sha256(sums, "lapsecoin.msi") == good
        assert updater._expected_sha256(sums, "other") is None
        updater._verify_sha256(str(f), good)
        try:
            updater._verify_sha256(str(f), "0" * 64)
        except RuntimeError:
            return
        raise AssertionError("a wrong checksum should have been refused")

    def test_update_hands_off_to_a_helper_that_runs_the_installer(self, monkeypatch, tmp_path):
        exe_path = tmp_path / "app" / "lapsecoin.exe"
        exe_path.parent.mkdir()
        exe_path.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "executable", str(exe_path))
        monkeypatch.setattr(sys, "argv", [str(exe_path), "--no-gui"])
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             open(dest, "wb").write(self.OLE + b"\0" * 2_000_000))
        monkeypatch.setattr("requests.get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
        seen = {}
        monkeypatch.setattr(subprocess, "Popen",
                            lambda args, **kw: seen.update(args=args, cwd=kw.get("cwd")))
        monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
        session = updater.UpdateSession("windows-msi", "9.9.9")
        try:
            session._run_windows_msi()
        except SystemExit:
            pass
        helper = open(seen["args"][-1], encoding="ascii").read()
        assert "taskkill /PID" in helper
        assert "msiexec /i" in helper and "/qn" in helper
        assert 'start "" "' + str(exe_path) + '" --no-gui' in helper
        assert str(tmp_path / "Local" / "LapseCoin" / "update") in seen["cwd"]
