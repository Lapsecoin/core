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
        eval "$(sed -n '/^unremovable_packages()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^pip_install()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^needs_compiler()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^pip_install_with_tools()/,/^}}/p' "{SCRIPT}")"
        STATE="$(mktemp -d)"
        # pip runs in a pipeline subshell, so its calls are kept in files
        pip() {{
          if [ "${{2:-}}" = "--ignore-installed" ]; then echo "ignore:${{*:3}}" >> "$STATE/calls"; return 0; fi
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


# The system-installed package pip cannot replace (the log is what pip 25 prints
# on Debian when Flask needs a newer blinker than python3-blinker)
DEBIAN_BLINKER = """\
Installing collected packages: libtorrent, werkzeug, waitress, blinker, flask, lapsecoin
  Attempting uninstall: blinker
    Found existing installation: blinker 1.7.0
error: uninstall-no-record-file

\u00d7 Cannot uninstall blinker 1.7.0
\u2570\u2500> The package's contents are unknown: no RECORD file was found for blinker.

hint: The package was installed by debian. You should check if it can uninstall the package.
"""


def test_a_system_package_pip_cannot_replace_is_installed_over_and_the_install_retried():
    last = _run([(DEBIAN_BLINKER, 1), ("Successfully installed lapsecoin", 0)])
    assert last == "rc=0 calls=pip ignore:blinker pip"


def test_the_retry_is_made_once_not_forever():
    last = _run([(DEBIAN_BLINKER, 1), (DEBIAN_BLINKER, 1), (DEBIAN_BLINKER, 1)])
    assert last == "rc=1 calls=pip ignore:blinker pip"


def _functions(snippet):
    harness = textwrap.dedent(f"""
        set -uo pipefail
        eval "$(sed -n '/^unremovable_packages()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^libtorrent_failed()/,/^}}/p' "{SCRIPT}")"
        LOG="$(mktemp)"; INSTALL_LOG="$LOG"
        {snippet}
    """)
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("log, names", [
    (DEBIAN_BLINKER, "blinker"),
    ("ERROR: Cannot uninstall 'blinker'. It is a distutils installed project and thus we cannot "
     "accurately determine which files belong to it", "blinker"),
    ("ERROR: Cannot uninstall 'PyYAML'. RECORD file not found. You might be able to recover", "PyYAML"),
    ("error: uninstall-no-record-file\n\u00d7 Cannot uninstall a 1.0\n\u00d7 Cannot uninstall b 2.0", "a b"),
    ("ERROR: No matching distribution found for libtorrent", ""),
    ("Successfully installed lapsecoin", ""),
])
def test_it_names_what_pip_could_not_uninstall_in_every_wording(log, names):
    out = _functions(f"printf '%b' {log!r} > \"$LOG\"; unremovable_packages \"$LOG\"").stdout
    assert out.split() == names.split()


# pip lists libtorrent on every run; only an error about it means it failed
LISTED_NOT_FAILED = """\
Collecting libtorrent>=2.0.0 (from lapsecoin)
  Downloading libtorrent-2.1.1-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (8.2 MB)
Installing collected packages: libtorrent, werkzeug, blinker, lapsecoin
""" + DEBIAN_BLINKER


@pytest.mark.parametrize("log, failed", [
    (LISTED_NOT_FAILED, False),
    ("Collecting libtorrent>=2.0.0\nERROR: Could not find a version that satisfies the requirement libtorrent>=2.0.0", True),
    ("ERROR: No matching distribution found for libtorrent>=2.0.0", True),
    ("  Failed building wheel for libtorrent\nFailed to build libtorrent", True),
    ("ERROR: Could not build wheels for libtorrent, which is required to install", True),
    ("Collecting libtorrent\nerror: subprocess-exited-with-error", False),
])
def test_libtorrent_is_blamed_only_when_pips_errors_name_it(log, failed):
    r = _functions(f"printf '%b' {log!r} > \"$LOG.last\"; libtorrent_failed")
    assert (r.returncode == 0) is failed


def _fallback(pip_outputs, curl_ok=True):
    stubs = "\n".join(
        f'  {i}) printf %s {text!r}; echo; return {code} ;;' for i, (text, code) in enumerate(pip_outputs))
    # Built here because an f-string expression cannot hold a backslash before Python 3.12.
    curl_body = (r'printf "flask>=3.0\nlibtorrent>=2.0.0\npeewee>=3.17\n" > "$4"'
                 if curl_ok else 'return 22')
    harness = textwrap.dedent(f"""
        set -uo pipefail
        INSTALL_LOG="$(mktemp)"; TOOLS_READY=1; REPO_RAW=x
        have() {{ command -v "$1" >/dev/null 2>&1; }}
        eval "$(sed -n '/^unremovable_packages()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^pip_install()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^needs_compiler()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^pip_install_with_tools()/,/^}}/p' "{SCRIPT}")"
        eval "$(sed -n '/^install_without_libtorrent()/,/^}}/p' "{SCRIPT}")"
        STATE="$(mktemp -d)"
        curl() {{
          {curl_body}
        }}
        pip() {{
          echo "pip ${{*:2}}" | sed 's/ -r .*/ -r LIST/' >> "$STATE/calls"
          [ "${{2:-}}" = "-r" ] && cat "$3" >> "$STATE/listed"
          local n; n=$(grep -c '^pip' "$STATE/calls"); n=$((n-1))
          case $n in
        {stubs}
            *) return 0 ;;
          esac
        }}
        install_without_libtorrent; rc=$?
        echo "rc=$rc calls=$(tr '\n' ',' < "$STATE/calls") listed=$(tr '\n' ',' < "$STATE/listed" 2>/dev/null)"
    """)
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False).stdout.strip().splitlines()[-1]


def test_without_libtorrent_installs_the_rest_then_the_app_on_top():
    out = _fallback([("ok", 0), ("ok", 0)])
    assert out.startswith("rc=0 calls=pip -r LIST,pip lapsecoin --no-deps,")
    assert "libtorrent" not in out.split("listed=")[1] and "flask>=3.0" in out


def test_without_libtorrent_a_failed_dependency_install_is_a_failed_install():
    out = _fallback([("ERROR: something broke", 1)])
    assert out.startswith("rc=1 calls=pip -r LIST,") and "lapsecoin --no-deps" not in out


def test_without_libtorrent_a_failed_app_install_is_a_failed_install():
    assert _fallback([("ok", 0), ("ERROR: no", 1)]).startswith("rc=1 ")


def test_without_libtorrent_no_dependency_list_is_a_failed_install():
    assert _fallback([], curl_ok=False).startswith("rc=1 calls= ")
