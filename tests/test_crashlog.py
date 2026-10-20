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


class _Relay:
    """A stand-in for the relay on localhost, so nothing real is contacted."""

    def __enter__(self):
        import http.server
        import json
        import threading
        received = self.received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(h):
                body = h.rfile.read(int(h.headers["Content-Length"]))
                received.append(json.loads(body))
                h.send_response(201)
                h.end_headers()

            def log_message(h, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/report"
        return self

    def __exit__(self, *args):
        self.server.shutdown()


@pytest.fixture
def reporting(monkeypatch, tmp_path):
    """Installed crash logging in a temp folder, to send to a relay."""
    out = tmp_path / "out"
    monkeypatch.setenv("LAPSECOIN_CRASHLOG", "1")
    monkeypatch.setattr(crashlog, "candidate_dirs", lambda environ=None: [str(out)])
    monkeypatch.setattr(crashlog, "_message_box", lambda *a, **k: pytest.fail("a crash must show no window"))
    monkeypatch.delenv("LAPSECOIN_CRASH_REPORTS", raising=False)
    before = (sys.excepthook, crashlog.threading.excepthook)
    with monkeypatch.context() as m:
        # the background send of earlier crashes is called by hand in these tests
        m.setattr(crashlog.threading, "Thread", lambda **k: type("T", (), {"start": lambda self: None})())
        crashlog.install()
    yield out
    sys.excepthook, crashlog.threading.excepthook = before
    crashlog._state["crash"].close()


def _raise():
    try:
        raise PermissionError("cannot open lapsecoin.log")
    except PermissionError as e:
        return type(e), e, e.__traceback__


class TestSending:
    def test_nothing_is_sent_without_a_relay_address(self, reporting, monkeypatch):
        monkeypatch.delenv("LAPSECOIN_CRASH_REPORT_URL", raising=False)
        monkeypatch.setattr(crashlog, "endpoint", lambda: "")
        assert crashlog.send({"v": 1}) is False

    def test_a_critical_crash_is_sent_once_with_no_window(self, reporting, monkeypatch):
        with _Relay() as relay:
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", relay.url)
            crashlog.report_fatal(*_raise())
            assert len(relay.received) == 1
            sent = relay.received[0]
            assert sent["kind"] == "fatal" and sent["v"] == 1
            assert "PermissionError: cannot open lapsecoin.log" in sent["trace"]
            assert len(sent["sig"]) == 10
            assert (reporting / crashlog.SENT_MARK).exists()
            assert "report sent" in (reporting / crashlog.START_LOG).read_text()

    def test_it_is_switched_off_by_the_environment_and_by_a_file(self, reporting, monkeypatch):
        with _Relay() as relay:
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", relay.url)
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORTS", "0")
            assert crashlog.send({"v": 1}) is False
            monkeypatch.delenv("LAPSECOIN_CRASH_REPORTS")
            (reporting / crashlog.OPT_OUT_FILE).write_text("")
            assert crashlog.send({"v": 1}) is False
            assert relay.received == []

    def test_offline_the_crash_is_kept_and_the_next_start_sends_it(self, reporting, monkeypatch):
        monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", "http://127.0.0.1:9/report")   # nothing listens
        crashlog.report_fatal(*_raise())                        # must not raise or hang
        assert not (reporting / crashlog.SENT_MARK).exists()
        assert "PermissionError" in (reporting / crashlog.CRASH_LOG).read_text()
        with _Relay() as relay:                                 # the next start, now online
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", relay.url)
            assert crashlog.send_pending() is True
            assert [r["kind"] for r in relay.received] == ["earlier-run"]
            assert "PermissionError" in relay.received[0]["trace"]
            assert crashlog.send_pending() is False             # not twice
            assert len(relay.received) == 1

    def test_a_native_crash_recorded_by_an_earlier_run_is_sent_once(self, reporting, monkeypatch):
        with open(reporting / crashlog.CRASH_LOG, "a") as f:
            f.write("\nFatal Python error: Segmentation fault\n\nCurrent thread 0x1234 (most recent call first):\n"
                    "  File \"main.py\", line 10 in <module>\n")
        with _Relay() as relay:
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", relay.url)
            assert crashlog.send_pending() is True
            assert len(relay.received) == 1 and relay.received[0]["kind"] == "earlier-run"
            assert "Segmentation fault" in relay.received[0]["trace"]
            assert crashlog.send_pending() is False
            assert len(relay.received) == 1

    def test_a_log_with_nothing_critical_sends_nothing(self, reporting, monkeypatch):
        with open(reporting / crashlog.CRASH_LOG, "a") as f:
            f.write("a note that is not a crash\n")
        with _Relay() as relay:
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORT_URL", relay.url)
            assert crashlog.send_pending() is False
            assert relay.received == []


class TestWhatIsSent:
    def test_the_account_name_paths_and_addresses_are_taken_out(self, reporting, monkeypatch):
        monkeypatch.setenv("USERNAME", "Vic")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\Vic")
        trace = ("File C:\\Users\\Vic\\LapseCoin\\x.py failed for Vic, "
                 "address bid.catch.flower.smart.eyebrow.polar.staff.view.diesel.pioneer.horn.couple")
        payload = crashlog.build_payload("fatal", "start-up", trace, "abc1234567")
        flat = repr(payload)
        assert "Vic" not in flat and "bid.catch" not in flat
        assert "<address>" in payload["trace"]

    def test_the_text_is_cut_to_a_size_the_relay_accepts(self, reporting):
        payload = crashlog.build_payload("fatal", "start-up", "x" * 50000, "abc1234567")
        assert len(payload["trace"]) == crashlog.MAX_TRACE
        import json
        assert len(json.dumps(payload)) < 8000

    def test_one_fault_has_one_signature(self):
        a, b = _raise(), _raise()
        assert crashlog.signature(a[0], a[2]) != "" 
        assert crashlog.signature(a[0], a[2]) == crashlog.signature(a[0], a[2])

    def test_the_account_name_is_taken_out_of_paths(self, monkeypatch):
        env = {"USERPROFILE": r"C:\Users\Vic", "USERNAME": "Vic", "LOCALAPPDATA": r"C:\Users\Vic\AppData\Local"}
        out = crashlog.sanitize(r"open C:\Users\Vic\AppData\Local\x failed for Vic", env)
        assert "Vic" not in out and "%LOCALAPPDATA%" in out
