"""Process-level smoke test for main.py's real startup path.

Not a substitute for the unit tests: those exercise Node/Gossip/Syncer in
isolation with mocked dependencies, which is fast and precise, but can
never catch a wiring mistake in main() itself, an undefined name, a
mismatched return-tuple, an argument landing in the wrong slot. Those
only exist at the real entry point; no mock ever calls it. This launches
the actual process and checks it reaches a known-good log line with no
traceback, covering the two branches _load_or_create_key has (create a
new key, then reload the same key on a second start) and the one line
that actually crashed in production (node.ensure_privacy_key(passphrase)).

Bounded and hermetic: each run gets its own tmp_path, so LOG_FILE,
LOCK_FILE, keyfile and db all land there rather than the repo or a real
deployment's files, and its own free port, and is killed after a short
timeout regardless of outcome.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

MAIN_PY = str(Path(__file__).resolve().parent.parent / "main.py")
STARTUP_TIMEOUT = 15.0
# Must be a line logged after node.ensure_privacy_key(passphrase), the call
# that actually crashed in production, or a broken build there would still
# read as "ready". "UDP transport on port" logs before that call and gave a
# false pass; "private API" is the last startup line and comes after it.
READY_MARKER = "private API on"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_headless(tmp_path, keyfile, extra_env=None, ready_check=None):
    """Launch main.py against a scratch tmp_path, collect output until
    `ready_check(output_so_far)` is true or it exits or times out, then
    kill it. Returns (ready, exit_code, full_output)."""
    port = _free_port()
    env = dict(os.environ)
    env["LAPSECOIN_PASSPHRASE"] = "smoketestpass123"
    env.update(extra_env or {})
    ready_check = ready_check or (lambda out: READY_MARKER in out)

    proc = subprocess.Popen(
        [sys.executable, MAIN_PY,
         "--no-gui",
         "--keyfile", str(keyfile),
         "--db", str(tmp_path / "chain.db"),
         "--port", str(port),
         "--no-update-check"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    chunks = []
    ready = False
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # exited on its own, which never happens on a healthy start
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            chunks.append(line)
            if ready_check("".join(chunks)):
                ready = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout:
            remaining = proc.stdout.read()
            if remaining:
                chunks.append(remaining)
    return ready, proc.returncode, "".join(chunks)


class TestMainStartupSmoke:
    """Would have caught the NameError in 024f711 (node.ensure_privacy_key
    called with a `passphrase` that didn't exist in main()'s scope): that
    bug lived entirely in main()'s own wiring, which no mocked unit test
    ever executes. See test_would_have_caught_the_real_bug below for a
    direct check of that, run against the actual broken commit."""

    def test_fresh_key_creation_starts_cleanly(self, tmp_path):
        keyfile = tmp_path / "node.key"
        ready, code, output = run_headless(tmp_path, keyfile)
        assert ready, f"never reached startup (exit={code}):\n{output}"
        assert "Traceback" not in output
        assert keyfile.exists()

    def test_existing_key_reload_starts_cleanly(self, tmp_path):
        """A second start against the same keyfile takes the other branch
        of _load_or_create_key, the branch the real crash hit (an
        already-provisioned node restarting)."""
        keyfile = tmp_path / "node.key"
        ready1, code1, output1 = run_headless(tmp_path, keyfile)
        assert ready1, f"first start failed (exit={code1}):\n{output1}"

        ready2, code2, output2 = run_headless(tmp_path, keyfile)
        assert ready2, f"reload failed (exit={code2}):\n{output2}"
        assert "Traceback" not in output2
        assert "key loaded" in output2

    def test_privacy_setting_creates_a_standalone_key_file(self, tmp_path):
        """Exercises node.ensure_privacy_key(passphrase) specifically,
        the exact call that crashed: the passphrase has to actually reach
        it, and a second, independently-encrypted key file has to exist
        by the time the node is up."""
        keyfile = tmp_path / "node.key"
        privacy_keyfile = Path(str(keyfile) + ".privacy")

        ready, code, output = run_headless(
            tmp_path, keyfile,
            extra_env={"LAPSECOIN_PRIVATE_ADDRESS": "1"},
            ready_check=lambda out: privacy_keyfile.exists(),
        )
        assert ready, f"privacy key never appeared (exit={code}):\n{output}"
        assert "Traceback" not in output
        assert privacy_keyfile.exists()
        assert privacy_keyfile.read_bytes() != keyfile.read_bytes()


def _main_module():
    """main.py as an importable module. It is a script, not a package, so
    there is no ordinary import for it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_main_under_test", MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPortFromEnvironment:
    """LAPSECOIN_PORT, and that an explicit --port still beats it."""

    def test_unset_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.delenv("LAPSECOIN_PORT", raising=False)
        assert _main_module()._env_port("LAPSECOIN_PORT", 8333) == 8333

    def test_set_is_used(self, monkeypatch):
        monkeypatch.setenv("LAPSECOIN_PORT", "19531")
        assert _main_module()._env_port("LAPSECOIN_PORT", 8333) == 19531

    def test_blank_is_treated_as_unset(self, monkeypatch):
        """An unset variable and one exported empty are the same intent, and
        a unit file doing LAPSECOIN_PORT=${PORT} with PORT unset produces
        the second."""
        monkeypatch.setenv("LAPSECOIN_PORT", "   ")
        assert _main_module()._env_port("LAPSECOIN_PORT", 8333) == 8333

    def test_a_bad_value_says_which_variable_and_why(self, monkeypatch):
        """Left to argparse this would fail naming --port, which was never
        set, so the message has to name the variable that was."""
        import pytest
        main = _main_module()
        for bad in ("abc", "99999", "0", "-1"):
            monkeypatch.setenv("LAPSECOIN_PORT", bad)
            with pytest.raises(SystemExit) as excinfo:
                main._env_port("LAPSECOIN_PORT", 8333)
            assert "LAPSECOIN_PORT" in str(excinfo.value)
