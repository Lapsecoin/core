"""scripts/virustotal.py scans a release file and returns a markdown line for
the release notes. A scan must never be able to block a release, so every
failure has to end as "print nothing and exit 0". These tests run it against a
local stand-in for the VirusTotal API."""
import hashlib
import http.server
import importlib.util
import json
import os
import threading

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "virustotal.py")


def load():
    spec = importlib.util.spec_from_file_location("virustotal_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeVT:
    """Answers like the API: upload -> analysis id, analysis -> stats."""

    def __init__(self, stats, polls_until_done=1, fail_upload=False):
        self.stats, self.polls_until_done, self.fail_upload = stats, polls_until_done, fail_upload
        self.requests, self.polls = [], 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("x-apikey")))
                if self.path.endswith("/files/upload_url"):
                    return self.reply({"data": f"http://127.0.0.1:{outer.port}/_upload/one"})
                outer.polls += 1
                done = outer.polls >= outer.polls_until_done
                self.reply({"data": {"attributes": {
                    "status": "completed" if done else "queued",
                    "stats": outer.stats if done else {}}}})

            def do_POST(self):
                outer.requests.append(("POST", self.path, self.headers.get("x-apikey")))
                self.rfile.read(int(self.headers["Content-Length"]))
                if outer.fail_upload:
                    return self.reply({"error": "quota"}, 429)
                self.reply({"data": {"type": "analysis", "id": "abc"}})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/v3"


@pytest.fixture
def vt(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "POLL_SECONDS", 0)
    monkeypatch.setenv("VT_API_KEY", "secret-key")
    servers = []

    def make(stats, **kw):
        fake = FakeVT(stats, **kw)
        servers.append(fake)
        monkeypatch.setattr(mod, "API", fake.url)
        return fake
    mod.fake = make
    yield mod
    for s in servers:
        s.server.shutdown()
        s.server.server_close()


def run(mod, monkeypatch, path, capsys, badge=None):
    monkeypatch.setattr("sys.argv", ["virustotal.py", str(path)] + ([str(badge)] if badge else []))
    mod.main()
    out = capsys.readouterr()
    return out.out, out.err


@pytest.fixture
def msi(tmp_path):
    path = tmp_path / "lapsecoin.msi"
    path.write_bytes(b"installer bytes")
    return path


CLEAN = {"malicious": 0, "suspicious": 0, "harmless": 0, "undetected": 70}


def test_a_clean_scan_links_the_report_and_keeps_the_badge_out_of_the_body(vt, msi, monkeypatch, capsys):
    fake = vt.fake(CLEAN)
    out, _ = run(vt, monkeypatch, msi, capsys)
    sha = hashlib.sha256(b"installer bytes").hexdigest()
    assert "0 of 70 engines flagged lapsecoin.msi" in out
    assert f"https://www.virustotal.com/gui/file/{sha}" in out
    assert f"SHA256: `{sha}`" in out
    assert "shields.io" not in out, "the badge belongs in the heading of the notes"
    assert all(key == "secret-key" for _, _, key in fake.requests)
    assert fake.requests[0][:2] == ("POST", "/api/v3/files")


def test_the_badge_is_written_for_the_heading_when_a_file_is_named(vt, msi, tmp_path, monkeypatch, capsys):
    vt.fake(CLEAN)
    badge = tmp_path / "badge.md"
    run(vt, monkeypatch, msi, capsys, badge=badge)
    assert badge.read_text() == ("![VirusTotal](https://img.shields.io/badge/"
                                 "VirusTotal-0%20%2F%2070-brightgreen)")


def test_the_badge_colour_follows_how_many_engines_flag_it(vt, msi, tmp_path, monkeypatch, capsys):
    badge = tmp_path / "badge.md"
    vt.fake({"malicious": 2, "suspicious": 1, "harmless": 0, "undetected": 67})
    out, _ = run(vt, monkeypatch, msi, capsys, badge=badge)
    assert "3 of 70" in out and badge.read_text().endswith("VirusTotal-3%20%2F%2070-yellow)")
    vt.fake({"malicious": 9, "suspicious": 0, "harmless": 0, "undetected": 61})
    out, _ = run(vt, monkeypatch, msi, capsys, badge=badge)
    assert "9 of 70" in out and badge.read_text().endswith("VirusTotal-9%20%2F%2070-red)")


def test_no_badge_file_means_none_is_written(vt, msi, tmp_path, monkeypatch, capsys):
    vt.fake(CLEAN)
    run(vt, monkeypatch, msi, capsys)
    assert list(tmp_path.glob("*.md")) == []


def test_a_failed_scan_leaves_no_badge_so_the_heading_stays_plain(vt, msi, tmp_path, monkeypatch, capsys):
    vt.fake(CLEAN, fail_upload=True)
    badge = tmp_path / "badge.md"
    run(vt, monkeypatch, msi, capsys, badge=badge)
    assert not badge.exists()


def test_it_waits_while_the_analysis_is_queued(vt, msi, monkeypatch, capsys):
    fake = vt.fake(CLEAN, polls_until_done=3)
    out, _ = run(vt, monkeypatch, msi, capsys)
    assert fake.polls == 3 and "0 of 70" in out


def test_a_file_over_the_direct_limit_goes_through_the_upload_url(vt, msi, monkeypatch, capsys):
    monkeypatch.setattr(vt, "DIRECT_LIMIT", 5)
    fake = vt.fake(CLEAN)
    out, _ = run(vt, monkeypatch, msi, capsys)
    assert [r[:2] for r in fake.requests][:2] == [("GET", "/api/v3/files/upload_url"),
                                                  ("POST", "/_upload/one")]
    assert "0 of 70" in out


def test_without_a_key_it_prints_nothing_and_never_calls_out(vt, msi, monkeypatch, capsys):
    fake = vt.fake(CLEAN)
    monkeypatch.delenv("VT_API_KEY")
    out, err = run(vt, monkeypatch, msi, capsys)
    assert out == "" and "skipping" in err and fake.requests == []


def test_a_rejected_upload_prints_nothing(vt, msi, monkeypatch, capsys):
    vt.fake(CLEAN, fail_upload=True)
    out, err = run(vt, monkeypatch, msi, capsys)
    assert out == "" and "virustotal:" in err


def test_a_scan_that_never_finishes_gives_up_quietly(vt, msi, monkeypatch, capsys):
    monkeypatch.setattr(vt, "GIVE_UP_AFTER", 0.1)
    monkeypatch.setattr(vt, "POLL_SECONDS", 0.02)
    vt.fake(CLEAN, polls_until_done=10**6)
    out, err = run(vt, monkeypatch, msi, capsys)
    assert out == "" and "gave up" in err


def test_a_missing_file_or_server_prints_nothing(vt, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vt, "API", "http://127.0.0.1:9/api/v3")
    out, _ = run(vt, monkeypatch, tmp_path / "nothing.msi", capsys)
    assert out == ""


def test_the_key_is_never_printed(vt, msi, monkeypatch, capsys):
    vt.fake(CLEAN)
    out, err = run(vt, monkeypatch, msi, capsys)
    assert "secret-key" not in out + err
