import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import crashlog  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setitem(crashlog._state, "installed", False)
    monkeypatch.setitem(crashlog._state, "dir", None)
    monkeypatch.setitem(crashlog._state, "crash", None)
    monkeypatch.setenv("LAPSECOIN_NO_DIALOGS", "1")
    for name in ("LAPSECOIN_CRASH_REPO", "LAPSECOIN_CRASH_TOKEN", "LAPSECOIN_CRASH_API", "LAPSECOIN_CRASH_REPORTS"):
        monkeypatch.delenv(name, raising=False)


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


class _FakeGitHub:
    """Stands in for api.github.com on localhost. `existing` is the issue number
    the search finds, or None."""

    def __init__(self, existing=None, accept=True):
        self.existing, self.accept, self.requests = existing, accept, []

    def __enter__(self):
        import http.server
        import threading
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(h, status, body):
                data = json.dumps(body).encode()
                h.send_response(status)
                h.send_header("Content-Length", str(len(data)))
                h.end_headers()
                h.wfile.write(data)

            def do_GET(h):
                outer.requests.append(("GET", h.path, None, h.headers.get("Authorization")))
                items = [{"number": outer.existing}] if outer.existing else []
                h._reply(200, {"items": items})

            def do_POST(h):
                body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
                outer.requests.append(("POST", h.path, body, h.headers.get("Authorization")))
                h._reply(201 if outer.accept else 403, {})

            def log_message(h, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()

    @property
    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def _use(monkeypatch, fake, repo="Lapsecoin/crash-reports"):
    monkeypatch.setenv("LAPSECOIN_CRASH_REPO", repo)
    monkeypatch.setenv("LAPSECOIN_CRASH_TOKEN", "test-token")
    monkeypatch.setenv("LAPSECOIN_CRASH_API", fake.api)


@pytest.fixture
def reporting(monkeypatch, tmp_path):
    """Installed crash logging in a temp folder."""
    out = tmp_path / "out"
    monkeypatch.setenv("LAPSECOIN_CRASHLOG", "1")
    monkeypatch.setattr(crashlog, "candidate_dirs", lambda environ=None: [str(out)])
    monkeypatch.setattr(crashlog, "_message_box", lambda *a, **k: pytest.fail("a crash must show no window"))
    before = (sys.excepthook, crashlog.threading.excepthook)
    with monkeypatch.context() as m:
        # the background send of earlier crashes is exercised on its own
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


class TestTheReportFiles:
    def test_install_makes_the_start_log_and_the_hooks(self, reporting):
        assert (reporting / crashlog.START_LOG).exists()
        assert (reporting / crashlog.CRASH_LOG).exists()
        assert sys.excepthook is crashlog._excepthook
        crashlog.breadcrumb("data folder ready")
        assert "data folder ready" in (reporting / crashlog.START_LOG).read_text()

    def test_a_fatal_exception_is_written_with_the_environment(self, reporting):
        crashlog.report_fatal(*_raise())
        text = (reporting / crashlog.CRASH_LOG).read_text()
        assert "PermissionError: cannot open lapsecoin.log" in text
        assert "fatal exception during start-up" in text and "LapseCoin " in text

    def test_the_crash_test_flag_fails_on_purpose_and_leaves_a_report(self, monkeypatch, tmp_path):
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

    def test_a_thread_failure_is_logged_and_not_sent(self, reporting, monkeypatch):
        import threading
        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)

            def boom():
                raise ValueError("worker failed")
            t = threading.Thread(target=boom, name="worker")
            t.start(); t.join()
            assert "worker failed" in (reporting / crashlog.CRASH_LOG).read_text()
            assert gh.requests == []


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


class TestSending:
    def test_nothing_is_sent_without_a_token_in_the_build(self, reporting):
        assert crashlog.target() is None
        assert crashlog.send({"v": 1, "sig": "abc"}) is False

    def test_a_first_crash_opens_an_issue_with_the_token(self, reporting, monkeypatch):
        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)
            crashlog.report_fatal(*_raise())
            assert [r[0] for r in gh.requests] == ["GET", "POST"]
            method, path, body, auth = gh.posts[0]
            assert path == "/repos/Lapsecoin/crash-reports/issues" and auth == "Bearer test-token"
            assert body["title"].startswith("Crash ") and "PermissionError: cannot open lapsecoin.log" in body["title"]
            assert "PermissionError" in body["body"]
            assert body["labels"] == ["crash-report"]
            assert (reporting / crashlog.SENT_MARK).exists()

    def test_the_same_crash_again_adds_a_comment_not_an_issue(self, reporting, monkeypatch):
        with _FakeGitHub(existing=42) as gh:
            _use(monkeypatch, gh)
            crashlog.report_fatal(*_raise())
            method, path, body, _ = gh.posts[0]
            assert path == "/repos/Lapsecoin/crash-reports/issues/42/comments"
            assert body["body"].startswith("Again: LapseCoin")

    def test_a_refusal_from_github_is_not_counted_as_sent(self, reporting, monkeypatch):
        with _FakeGitHub(accept=False) as gh:
            _use(monkeypatch, gh)
            crashlog.report_fatal(*_raise())
            assert not (reporting / crashlog.SENT_MARK).exists()

    def test_it_is_switched_off_by_the_environment_and_by_a_file(self, reporting, monkeypatch):
        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)
            monkeypatch.setenv("LAPSECOIN_CRASH_REPORTS", "0")
            assert crashlog.send({"v": 1, "sig": "abc"}) is False
            monkeypatch.delenv("LAPSECOIN_CRASH_REPORTS")
            (reporting / crashlog.OPT_OUT_FILE).write_text("")
            assert crashlog.send({"v": 1, "sig": "abc"}) is False
            assert gh.requests == []

    def test_offline_the_crash_is_kept_and_the_next_start_sends_it(self, reporting, monkeypatch):
        monkeypatch.setenv("LAPSECOIN_CRASH_REPO", "Lapsecoin/crash-reports")
        monkeypatch.setenv("LAPSECOIN_CRASH_TOKEN", "t")
        monkeypatch.setenv("LAPSECOIN_CRASH_API", "http://127.0.0.1:9")      # nothing listens
        crashlog.report_fatal(*_raise())                        # must not raise or hang
        assert not (reporting / crashlog.SENT_MARK).exists()
        assert "PermissionError" in (reporting / crashlog.CRASH_LOG).read_text()
        with _FakeGitHub() as gh:                               # the next start, now online
            _use(monkeypatch, gh)
            assert crashlog.send_pending() is True
            assert len(gh.posts) == 1 and "earlier-run" in gh.posts[0][2]["body"]
            assert crashlog.send_pending() is False             # not twice
            assert len(gh.posts) == 1

    def test_a_native_crash_recorded_by_an_earlier_run_is_sent_once(self, reporting, monkeypatch):
        with open(reporting / crashlog.CRASH_LOG, "a") as f:
            f.write("\nFatal Python error: Segmentation fault\n\nCurrent thread 0x1234 (most recent call first):\n"
                    "  File \"main.py\", line 10 in <module>\n")
        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)
            assert crashlog.send_pending() is True
            assert "Segmentation fault" in gh.posts[0][2]["body"]
            assert crashlog.send_pending() is False

    def test_the_start_up_resend_does_not_file_a_crash_that_is_being_sent(self, reporting, monkeypatch):
        # The resend thread starts with the program, so a crash right at start-up raced it
        # and was filed twice (once live, once as an earlier run).
        import threading
        real_send = crashlog.send
        resend = []

        def send_while_resending(payload):
            t = threading.Thread(target=crashlog.send_pending)
            t.start()
            resend.append(t)
            time.sleep(0.3)                  # the resend gets its chance to read the log
            return real_send(payload)

        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)
            monkeypatch.setattr(crashlog, "send", send_while_resending)
            crashlog.report_fatal(*_raise())
            resend[0].join(10)
            assert len(gh.posts) == 1

    def test_a_log_with_nothing_critical_sends_nothing(self, reporting, monkeypatch):
        with open(reporting / crashlog.CRASH_LOG, "a") as f:
            f.write("a note that is not a crash\n")
        with _FakeGitHub() as gh:
            _use(monkeypatch, gh)
            assert crashlog.send_pending() is False
            assert gh.requests == []


class TestLongDumps:
    def test_a_long_dump_keeps_the_crashing_thread_and_the_end(self):
        dump = "Windows fatal exception: access violation\n" + "x" * 9000 + "\nTHE END"
        kept = crashlog._head_and_tail(dump, crashlog.MAX_TRACE)
        assert kept.startswith("Windows fatal exception") and kept.endswith("THE END")
        assert len(kept) == crashlog.MAX_TRACE


class TestWhatIsSent:
    def test_the_account_name_paths_and_addresses_are_taken_out(self, reporting, monkeypatch):
        monkeypatch.setenv("USERNAME", "Vic")
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\Vic")
        trace = ("File C:\\Users\\Vic\\LapseCoin\\x.py failed for Vic, "
                 "address bid.catch.flower.smart.eyebrow.polar.staff.view.diesel.pioneer.horn.couple")
        payload = crashlog.build_payload("fatal", "start-up", trace, "abc1234567")
        assert "Vic" not in repr(payload) and "bid.catch" not in repr(payload)
        assert "<address>" in payload["trace"]

    def test_the_text_is_cut_to_size(self, reporting):
        payload = crashlog.build_payload("fatal", "start-up", "x " * 25000, "abc1234567")
        assert len(payload["trace"]) == crashlog.MAX_TRACE

    def test_text_cannot_close_the_code_fence_or_carry_a_folder_name(self):
        assert crashlog._clean("a" + crashlog.FENCE + "b", 50) == "a'''b"
        assert crashlog._clean("open C:\\Users\\Vic\\x", 50) == "open %USERPROFILE%\\x"
        body = crashlog.issue_body({"trace": "boom " + crashlog.FENCE + "\n## injected", "steps": "C:\\Users\\Vic\\LapseCoin"})
        assert (crashlog.FENCE + "\n## injected") not in body and "Vic" not in body
        assert len(crashlog.issue_title({"sig": "abc", "trace": "line\n" + "x" * 500})) <= 120

    def test_one_fault_has_one_signature(self):
        a, b = _raise(), _raise()
        assert crashlog.signature(a[0], a[2]) == crashlog.signature(a[0], a[2]) != ""
        assert len(crashlog.signature(a[0], a[2])) == 10

    def test_the_account_name_is_taken_out_of_paths(self):
        env = {"USERPROFILE": "C:\\Users\\Vic", "USERNAME": "Vic", "LOCALAPPDATA": "C:\\Users\\Vic\\AppData\\Local"}
        out = crashlog.sanitize("open C:\\Users\\Vic\\AppData\\Local\\x failed for Vic", env)
        assert "Vic" not in out and "%LOCALAPPDATA%" in out


SCRUB = json.loads((os.path.join(os.path.dirname(__file__), "scrub_cases.json") and
                    open(os.path.join(os.path.dirname(__file__), "scrub_cases.json"), encoding="utf-8").read()))


class TestNothingPrivateIsSent:
    """tests/scrub_cases.json is also run through the launcher on Windows (launcher-probe.yml)."""

    @pytest.mark.parametrize("case", SCRUB["cases"], ids=[c["name"] for c in SCRUB["cases"]])
    def test_the_shared_cases(self, case):
        assert crashlog.sanitize(case["in"], SCRUB["environment"]) == case["out"]

    def test_the_apps_own_version_is_not_an_ip_address(self, monkeypatch):
        monkeypatch.setattr(crashlog, "_app_version", lambda: "1.1.6")
        out = crashlog.sanitize("package vicnas.x_1.1.6.0_x64 version: 1.1.6.0 and 1.1.6.1", {})
        assert "version: 1.1.6.0" in out and "1.1.6.1" not in out

    def test_options_are_listed_without_their_values(self):
        assert crashlog.argument_names(["--key", "lc1-SECRET", "--peer=203.0.113.7:8333", "--no-gui", "C:\\a\\b.json"]) \
            == "--key --peer --no-gui"

    def test_the_start_log_line_has_no_values(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["lapsecoin", "--address", "abandon.ability.able", "--peer", "203.0.113.7:8333"])
        text = "\n".join(crashlog.environment_lines())
        assert "options=--address --peer" in text
        assert "abandon" not in text and "203.0.113.7" not in text

    def test_the_crash_log_on_disk_is_sanitized_too(self, reporting, monkeypatch):
        monkeypatch.setenv("USERNAME", "victor")
        crashlog.write_report("peer 203.0.113.7 refused for victor, passphrase=hunter2")
        text = (reporting / crashlog.CRASH_LOG).read_text()
        assert "203.0.113.7" not in text and "victor" not in text and "hunter2" not in text

    def test_dates_and_times_become_seconds_since_the_first(self):
        a, b = crashlog.relative_times("2026-10-21 07:10:51 start\n2026-10-21 07:11:01 later",
                                       "===== 2026-10-21 07:10:55 =====")
        assert a == "+0s start\n+10s later" and b == "===== +4s ====="

    def test_a_whole_report_holds_nothing_private(self, reporting, monkeypatch):
        for k, v in SCRUB["environment"].items():
            monkeypatch.setenv(k, v)
        secret_trace = ("Traceback (most recent call last):\n"
                        '  File "C:\\Users\\victor\\LapseCoin\\gui.py", line 9, in unlock\n'
                        "ValueError: bad key lc1-" + "A" * 114 + " for abandon.ability.able.about.above.absent.absorb."
                        "abstract.absurd.abuse.access.accident at 203.0.113.7:8333 passphrase=hunter2 victor@example.com "
                        "phrase abandon ability able about above absent absorb abstract absurd abuse access accident\n")
        crashlog.breadcrumb("opened 203.0.113.7 for victor")
        payload = crashlog.build_payload("fatal", "start-up", secret_trace, "sig")
        body = crashlog.issue_body(payload, "Lapsecoin/core") + crashlog.issue_title(payload)
        for leaked in ("victor", "VICPC", "203.0.113.7", "hunter2", "lc1-A", "abandon", "example.com"):
            assert leaked not in body, leaked
        assert "ValueError" in body and "gui.py" in body and "line 9" in body        # what was wrong is still there


class TestTheCodeInTheTrace:
    MODULES = {"gui.py": "src/gui.py", "main.py": "main.py"}

    def test_own_code_frames_link_to_the_release_tag(self):
        trace = ('Traceback (most recent call last):\n'
                 '  File "C:\\Program Files\\WindowsApps\\p\\main.py", line 17, in <module>\n'
                 '  File "C:\\Program Files\\WindowsApps\\p\\threading.py", line 982, in run\n'
                 '  File "C:\\Program Files\\WindowsApps\\p\\gui.py", line ??? in run_status_window\n'
                 'Thread 0x1 (most recent call first):\n  File "p\\gui.py", line 1504 in mainloop\n')
        links = crashlog.code_links(trace, "Lapsecoin/core", "1.1.6", self.MODULES)
        assert links == [
            "- [main.py:17 in <module>](https://github.com/Lapsecoin/core/blob/v1.1.6/main.py#L17)",
            "- [gui.py in run_status_window](https://github.com/Lapsecoin/core/blob/v1.1.6/src/gui.py)",
            "- [gui.py:1504 in mainloop](https://github.com/Lapsecoin/core/blob/v1.1.6/src/gui.py#L1504)"]

    def test_no_links_without_the_module_list_or_a_clean_version_and_repo(self):
        trace = 'File "p\\gui.py", line 3, in f'
        assert crashlog.code_links(trace, "Lapsecoin/core", "1.1.6", {}) == []
        assert crashlog.code_links(trace, "bad repo", "1.1.6", self.MODULES) == []
        assert crashlog.code_links(trace, "Lapsecoin/core", "1.1.6)(http://evil", self.MODULES) == []

    def test_the_issue_shows_them(self, monkeypatch):
        monkeypatch.setattr(crashlog, "own_modules", lambda: self.MODULES)
        body = crashlog.issue_body({"app": "1.1.6", "trace": 'File "p\\gui.py", line 3, in f'}, "Lapsecoin/core")
        assert "Code in the trace, at this release:" in body and "blob/v1.1.6/src/gui.py#L3" in body
