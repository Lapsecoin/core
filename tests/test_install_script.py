"""The retry in scripts/install.sh: a pip failure caused by a missing compiler
installs the build tools and tries again; any other failure does not. The
shell functions are run for real, with pip and the tool installer replaced by
stubs that record what they were asked to do."""

import os
import shutil
import subprocess
import textwrap

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "install.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(pip_outputs, tools_ready=0):
    """pip_outputs: what each successive `pip` call prints and whether it fails,
    as (text, exit_code). Returns (stdout, the list of calls made)."""
    stubs = "\n".join(
        f'  {i}) printf %s {text!r}; echo; return {code} ;;' for i, (text, code) in enumerate(pip_outputs))
    harness = textwrap.dedent(f"""
        set -uo pipefail
        INSTALL_LOG="$(mktemp)"
        TOOLS_READY={tools_ready}
        have() {{ command -v "$1" >/dev/null 2>&1; }}
        eval "$(sed -n '/^pip_install()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^needs_compiler()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^pip_install_with_tools()/,/^}}/p' "{SCRIPT}")"
        STATE="$(mktemp -d)"
        # pip runs in a pipeline subshell, so its calls are kept in files
        pip() {{
          echo pip >> "$STATE/calls"
          local n; n=$(grep -c '^pip$' "$STATE/calls"); n=$((n-1))
          case $n in
        {stubs}
            *) return 0 ;;
          esac
        }}
        ensure_build_tools() {{ echo tools >> "$STATE/calls"; TOOLS_READY=1; }}
        pip_install_with_tools lapsecoin; rc=$?
        echo "rc=$rc calls=$(tr '\n' ' ' < "$STATE/calls" | sed 's/ $//')"
    """)
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False).stdout
    return out.strip().splitlines()[-1]


def test_a_compiler_error_installs_the_tools_and_retries():
    last = _run([("error: subprocess-exited-with-error: gcc failed", 1)])
    assert last == "rc=0 calls=pip tools pip"


def test_a_missing_wheel_is_not_a_reason_to_install_tools():
    last = _run([("ERROR: No matching distribution found for libtorrent", 1)])
    assert last == "rc=1 calls=pip"


def test_the_tools_are_not_installed_twice():
    last = _run([("error: subprocess-exited-with-error", 1)], tools_ready=1)
    assert last == "rc=1 calls=pip"


def test_a_retry_that_fails_again_still_fails():
    last = _run([("error: subprocess-exited-with-error", 1), ("error: subprocess-exited-with-error", 1)])
    assert last == "rc=1 calls=pip tools pip"


def test_a_clean_install_never_touches_the_tools():
    assert _run([("ok", 0)]) == "rc=0 calls=pip"
