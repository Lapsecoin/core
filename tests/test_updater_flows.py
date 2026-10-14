"""The self-update paths other than the Windows installer (which
test_updater.py covers): how the install type is told apart, the Linux AppImage
replacement, the pip upgrade, and the rule that only one update runs at a time.
An update replaces the running program, so what it refuses to do matters as
much as what it does."""
import os
import stat
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import updater  # noqa: E402

ELF = b"\x7fELF" + b"\0" * 1_100_000


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("LAPSECOIN_DOCKER", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)


class TestInstallType:
    def test_docker_is_declared_and_wins_over_everything(self, monkeypatch, clean_env):
        monkeypatch.setenv("LAPSECOIN_DOCKER", "1")
        monkeypatch.setenv("APPIMAGE", "/x/app.AppImage")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert updater.detect_install_type() == "docker"
        assert not updater.Updater().can_self_update()

    def test_a_frozen_build_inside_an_appimage(self, monkeypatch, clean_env):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setenv("APPIMAGE", "/x/app.AppImage")
        assert updater.detect_install_type() == "linux-appimage"
        assert updater.Updater().can_self_update()

    def test_a_frozen_linux_build_outside_an_appimage_is_left_alone(self, monkeypatch, clean_env):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        assert updater.detect_install_type() == "source"

    def test_a_pip_install_is_found_through_its_metadata(self, monkeypatch, clean_env):
        import importlib.metadata as md
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(md, "distribution", lambda name: object())
        assert updater.detect_install_type() == "pip"

    def test_anything_else_is_source_and_never_self_updates(self, monkeypatch, clean_env):
        import importlib.metadata as md
        monkeypatch.setattr(sys, "frozen", False, raising=False)

        def missing(name):
            raise md.PackageNotFoundError(name)
        monkeypatch.setattr(md, "distribution", missing)
        assert updater.detect_install_type() == "source"
        assert not updater.Updater().can_self_update()

    def test_only_the_three_safe_types_may_update_themselves(self):
        assert updater.SELF_UPDATABLE == {"windows-msi", "linux-appimage", "pip"}


class TestDownloadChecks:
    def test_a_truncated_appimage_is_refused(self, tmp_path):
        f = tmp_path / "a"
        f.write_bytes(b"\x7fELF" + b"\0" * 100)
        with pytest.raises(RuntimeError, match="only"):
            updater._verify_appimage(str(f))

    def test_an_error_page_saved_as_the_download_is_refused(self, tmp_path):
        f = tmp_path / "a"
        f.write_bytes(b"<html>" + b" " * 2_000_000)
        with pytest.raises(RuntimeError, match="ELF"):
            updater._verify_appimage(str(f))

    def test_a_real_looking_appimage_passes(self, tmp_path):
        f = tmp_path / "a"
        f.write_bytes(ELF)
        updater._verify_appimage(str(f))

    def test_the_checksum_line_for_a_file_is_found_by_name(self):
        sums = f"{'AB' * 32}  lapsecoin.msi\n{'cd' * 32} *lapsecoin\n"
        assert updater._expected_sha256(sums, "lapsecoin.msi") == "ab" * 32
        assert updater._expected_sha256(sums, "lapsecoin") == "cd" * 32
        assert updater._expected_sha256(sums, "other") is None

    class _Response:
        def __init__(self, chunks, length=None, status_error=None):
            self._chunks, self.headers, self._err = chunks, {}, status_error
            if length is not None:
                self.headers["Content-Length"] = str(length)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            if self._err:
                raise self._err

        def iter_content(self, chunk_size):
            yield from self._chunks

    def test_progress_is_reported_as_a_percentage_when_the_size_is_known(self, monkeypatch, tmp_path):
        monkeypatch.setattr("requests.get", lambda *a, **k: self._Response([b"a" * 50, b"b" * 50], 100))
        seen = []
        updater._download("https://x/y", str(tmp_path / "f"), on_progress=seen.append)
        assert seen == ["50%", "100%"]
        assert (tmp_path / "f").read_bytes() == b"a" * 50 + b"b" * 50

    def test_progress_falls_back_to_kib_without_a_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr("requests.get", lambda *a, **k: self._Response([b"a" * 2048]))
        seen = []
        updater._download("https://x/y", str(tmp_path / "f"), on_progress=seen.append)
        assert seen == ["2 KiB"]

    def test_an_http_error_is_raised_and_nothing_is_kept_as_complete(self, monkeypatch, tmp_path):
        monkeypatch.setattr("requests.get", lambda *a, **k: self._Response([], status_error=OSError("404")))
        with pytest.raises(OSError):
            updater._download("https://x/y", str(tmp_path / "f"))


class TestAppImageUpdate:
    @pytest.fixture
    def setup(self, monkeypatch, tmp_path, clean_env):
        app = tmp_path / "bin" / "lapsecoin"
        app.parent.mkdir()
        app.write_bytes(b"old build")
        monkeypatch.setenv("APPIMAGE", str(app))
        monkeypatch.setattr(sys, "argv", [str(app), "--no-gui"])
        execs = []
        monkeypatch.setattr(os, "execv", lambda path, args: execs.append((path, args)))
        return app, execs

    def test_a_good_download_replaces_the_program_and_restarts_it(self, monkeypatch, setup):
        app, execs = setup
        urls = []
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             (urls.append(url), open(dest, "wb").write(ELF)))
        session = updater.UpdateSession("linux-appimage", "9.9.9")
        session.run()
        assert session.snapshot()["stage"] == "restarting" and session.error is None
        assert urls == ["https://github.com/Lapsecoin/core/releases/download/v9.9.9/lapsecoin"]
        assert app.read_bytes() == ELF
        assert app.stat().st_mode & stat.S_IXUSR, "the new program must be runnable"
        assert execs == [(str(app), [str(app), "--no-gui"])]
        assert [p.name for p in app.parent.iterdir()] == ["lapsecoin"], "no temp file left behind"

    def test_a_bad_download_changes_nothing_and_leaves_no_trace(self, monkeypatch, setup):
        app, execs = setup
        monkeypatch.setattr(updater, "_download", lambda url, dest, on_progress=None:
                             open(dest, "wb").write(b"<html>not a program</html>"))
        session = updater.UpdateSession("linux-appimage", "9.9.9")
        session.run()
        snap = session.snapshot()
        assert snap["stage"] == "failed" and "refusing" in snap["error"]
        assert app.read_bytes() == b"old build"
        assert execs == []
        assert [p.name for p in app.parent.iterdir()] == ["lapsecoin"]

    def test_a_failed_download_is_reported_not_raised(self, monkeypatch, setup):
        app, execs = setup

        def boom(url, dest, on_progress=None):
            raise OSError("network down")
        monkeypatch.setattr(updater, "_download", boom)
        session = updater.UpdateSession("linux-appimage", "9.9.9")
        session.run()
        assert session.snapshot()["stage"] == "failed"
        assert app.read_bytes() == b"old build" and execs == []

    def test_a_folder_it_cannot_write_to_stops_before_downloading(self, monkeypatch, setup):
        app, execs = setup
        monkeypatch.setattr(os, "access", lambda *a: False)
        monkeypatch.setattr(updater, "_download", lambda *a, **k: pytest.fail("must not download"))
        session = updater.UpdateSession("linux-appimage", "9.9.9")
        session.run()
        assert "isn't writable" in session.snapshot()["error"]

    def test_without_the_appimage_variable_it_refuses(self, monkeypatch, setup):
        monkeypatch.delenv("APPIMAGE")
        session = updater.UpdateSession("linux-appimage", "9.9.9")
        session.run()
        assert session.snapshot()["stage"] == "failed"


class TestPipUpdate:
    class _Proc:
        def __init__(self, lines, code):
            self.stdout, self._code = iter(lines), code

        def wait(self):
            return self._code

    def test_a_successful_upgrade_relaunches_in_place(self, monkeypatch):
        calls = []
        monkeypatch.setattr(updater.subprocess, "Popen",
                            lambda cmd, **kw: calls.append(cmd) or self._Proc(["Collecting\n", "Done\n"], 0))
        relaunched = []
        monkeypatch.setattr(updater.UpdateSession, "_relaunch_in_place", lambda self: relaunched.append(1))
        session = updater.UpdateSession("pip", "9.9.9")
        session.run()
        assert calls[0][-2:] == ["--no-cache-dir", "lapsecoin"]
        assert "install" in calls[0] and "--upgrade" in calls[0]
        assert relaunched == [1] and session.snapshot()["stage"] == "restarting"

    def test_a_failed_upgrade_is_not_followed_by_a_restart(self, monkeypatch):
        monkeypatch.setattr(updater.subprocess, "Popen",
                            lambda cmd, **kw: self._Proc(["ERROR: no matching distribution\n"], 1))
        monkeypatch.setattr(updater.UpdateSession, "_relaunch_in_place",
                            lambda self: pytest.fail("must not restart after a failed upgrade"))
        session = updater.UpdateSession("pip", "9.9.9")
        session.run()
        snap = session.snapshot()
        assert snap["stage"] == "failed" and "no matching distribution" in snap["error"]


class TestUpdaterObject:
    def test_an_unsupported_install_says_so_and_does_nothing(self):
        session = updater.UpdateSession("source", "9.9.9")
        session.run()
        snap = session.snapshot()
        assert snap["stage"] == "unsupported" and snap["error"] is None

    def test_idle_status_reports_what_the_ui_needs(self):
        status = updater.Updater(install_type="pip").status()
        assert status == {"stage": "idle", "detail": "", "error": None,
                          "install_type": "pip", "can_self_update": True}

    def test_a_second_update_is_refused_while_the_first_runs(self, monkeypatch):
        gate, started = threading.Event(), threading.Event()

        def slow(self):
            self._set("downloading", "x")
            started.set()
            gate.wait(5)
            self._set("failed", "stopped by the test")
        monkeypatch.setattr(updater.UpdateSession, "run", slow)
        up = updater.Updater(install_type="pip")
        assert up.start("9.9.9") is True
        assert started.wait(5)
        assert up.start("9.9.9") is False, "two updates must never run at once"
        gate.set()

    def test_after_a_failure_a_new_attempt_may_start(self, monkeypatch):
        done = threading.Event()

        def fail(self):
            self._set("failed", "no")
            done.set()
        monkeypatch.setattr(updater.UpdateSession, "run", fail)
        up = updater.Updater(install_type="pip")
        assert up.start("9.9.9") is True
        assert done.wait(5)
        done.clear()
        assert up.start("9.9.9") is True

    def test_run_sync_runs_on_the_calling_thread_and_returns_the_session(self, monkeypatch):
        seen = []
        monkeypatch.setattr(updater.UpdateSession, "run",
                            lambda self: seen.append(threading.current_thread()))
        session = updater.Updater(install_type="pip").run_sync("9.9.9")
        assert seen == [threading.current_thread()]
        assert session.target_version == "9.9.9"

    def test_progress_callback_errors_never_break_an_update(self):
        session = updater.UpdateSession("source", "9.9.9",
                                        on_progress=lambda *a: 1 / 0)
        session.run()
        assert session.snapshot()["stage"] == "unsupported"
