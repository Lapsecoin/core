import os
import subprocess
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import updater  # noqa: E402


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

    def test_an_unmarked_windows_build_cannot_update_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(tmp_path / "lapsecoin.exe"))
        monkeypatch.delenv("LAPSECOIN_DOCKER", raising=False)
        monkeypatch.delenv("APPIMAGE", raising=False)
        assert updater.detect_install_type() == "source"
        assert not updater.Updater(install_type="source").can_self_update()

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
        # A Nuitka build reports a python.exe that does not exist beside the
        # real program, so the helper must not start sys.executable.
        monkeypatch.setattr(sys, "executable", str(exe_path.parent / "python.exe"))
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


    def test_update_stops_before_closing_the_app_when_the_program_is_missing(self, monkeypatch, tmp_path):
        (tmp_path / "app").mkdir()
        monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "python.exe"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not hand off"))
        monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("must not exit"))
        with pytest.raises(RuntimeError, match="cannot find the installed program"):
            updater._installed_exe()


    def test_restart_check_passes_for_the_running_program(self, monkeypatch, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        exe = app / "lapsecoin.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "executable", str(app / "python.exe"))
        monkeypatch.setattr(sys, "argv", [str(exe)])
        report = tmp_path / "r.txt"
        assert updater.restart_check(str(report)) == 0
        assert "ok" in report.read_text()

    def test_restart_check_fails_when_the_target_is_not_the_running_program(self, monkeypatch, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        (app / "lapsecoin.exe").write_bytes(b"MZ")
        other = tmp_path / "other.exe"
        other.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "executable", str(app / "python.exe"))
        monkeypatch.setattr(sys, "argv", [str(other)])
        report = tmp_path / "r.txt"
        assert updater.restart_check(str(report)) == 1
        assert "WRONG" in report.read_text()

    def test_restart_check_reports_a_missing_program(self, monkeypatch, tmp_path):
        (tmp_path / "app").mkdir()
        monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "python.exe"))
        report = tmp_path / "r.txt"
        assert updater.restart_check(str(report)) == 1
        assert "cannot find the installed program" in report.read_text()
