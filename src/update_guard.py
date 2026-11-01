"""Keeps a self-update of the AppImage from leaving the user with a program
that will not start.

Before the running file is replaced, the downloaded one is started with
--self-test, which loads what a real start needs (the libraries that can fail
to load on a given system) and says so. A build that cannot do that is never
installed, and the update reports why.

A build can still pass that and then fail later, so the old one is kept beside
the new one (<program>.previous) and a marker records that the new one has not
yet proved itself. The new build starts the marker's clock when it starts. If
it is still running CONFIRM_AFTER_SECONDS later the marker is removed. If it
was not, the next start finds the marker still there and puts the previous
build back before doing anything else.

Plain standard library only: main.py runs check_at_start before it imports
anything heavy.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading

log = logging.getLogger("ec.updater")

PREVIOUS_SUFFIX = ".previous"
MARKER_SUFFIX = ".update-pending"
SELF_TEST_FLAG = "--self-test"
SELF_TEST_OK = "self-test ok"
SELF_TEST_TIMEOUT = 90
CONFIRM_AFTER_SECONDS = 30

# What a frozen parent leaves in the environment that would make a child
# started from it reuse the parent's unpacked files instead of its own.
_INHERITED_PREFIXES = ("_MEI", "_PYI", "PYINSTALLER", "TCL", "TK", "APPDIR", "APPIMAGE", "ARGV0", "OWD")


def paths(appimage):
    """(previous copy, marker) next to the program."""
    return appimage + PREVIOUS_SUFFIX, appimage + MARKER_SUFFIX


def clean_env(environ=None):
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if not k.upper().startswith(_INHERITED_PREFIXES)}


def smoke_test(path, target_version=None, timeout=SELF_TEST_TIMEOUT):
    """Starts the downloaded program with --self-test and raises RuntimeError,
    with what it printed, unless it exits 0 and says it is fine."""
    try:
        done = subprocess.run([path, SELF_TEST_FLAG], capture_output=True, text=True,
                              timeout=timeout, env=clean_env(), cwd=os.path.dirname(path) or None)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the downloaded program did not finish its self-test in {timeout}s; not installing it")
    except OSError as e:
        raise RuntimeError(f"the downloaded program could not be started ({e}); not installing it")
    said = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0 or SELF_TEST_OK not in said:
        tail = "\n".join(said.strip().splitlines()[-6:]) or "no output"
        raise RuntimeError(f"the downloaded program failed its self-test (exit {done.returncode}), "
                           f"so it was not installed:\n{tail}")
    if target_version and f" {target_version} " not in f" {said} ":
        log.warning("[updater] the downloaded program reports a version other than %s: %s",
                    target_version, said.strip()[:200])


def prepare(appimage, target_version):
    """Keeps the build about to be replaced and records that the new one is
    unproven. Call it right before replacing the program."""
    previous, marker = paths(appimage)
    tmp = previous + ".tmp"
    shutil.copy2(appimage, tmp)
    os.replace(tmp, previous)
    _write_marker(marker, {"version": target_version, "attempts": 0})


def _write_marker(marker, data):
    tmp = marker + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, marker)


def clear(appimage):
    """The update is proven, or was undone: the marker has done its job."""
    _, marker = paths(appimage)
    try:
        os.remove(marker)
    except OSError:
        pass


def check_at_start(environ=None, execv=None, timer=threading.Timer):
    """Called first thing by main.py. Returns "rolled-back" only if it could
    not exec the previous build (the real one never returns), "armed" when a
    freshly updated build starts its proving period, else None."""
    environ = os.environ if environ is None else environ
    execv = execv or os.execv
    appimage = environ.get("APPIMAGE")
    if not appimage:
        return None
    appimage = os.path.realpath(appimage)
    previous, marker = paths(appimage)
    try:
        with open(marker, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    if data.get("attempts", 0) >= 1:
        # The build installed by the update started and was not still running
        # CONFIRM_AFTER_SECONDS later, so it is not trusted.
        if os.path.isfile(previous):
            try:
                os.replace(previous, appimage)
            except OSError as e:
                log.error("[updater] could not put the previous version back: %s", e)
                clear(appimage)
                return None
            clear(appimage)
            msg = ("[updater] version %s did not stay up after the update, so the previous "
                   "version was put back" % data.get("version", "?"))
            log.error(msg)
            print(msg, file=sys.stderr)
            execv(appimage, [appimage] + sys.argv[1:])
            return "rolled-back"
        clear(appimage)
        return None

    data["attempts"] = 1
    try:
        _write_marker(marker, data)
    except OSError:
        return None
    t = timer(CONFIRM_AFTER_SECONDS, clear, args=(appimage,))
    t.daemon = True
    t.start()
    return "armed"


def _liboqs_outside(bundle, maps="/proc/self/maps"):
    """Problems, if the liboqs this process mapped is not the one in `bundle`.
    Read from what is mapped, not from the name oqs asked for: that can be a
    bare soname that resolves to either."""
    try:
        with open(maps, encoding="utf-8") as f:
            mapped = {line.split(None, 5)[5].strip() for line in f
                      if "liboqs" in line and len(line.split(None, 5)) == 6}
    except OSError:
        return []                  # no /proc: nothing to check against
    paths = {os.path.realpath(p) for p in mapped}
    if not paths:
        return ["oqs: no liboqs is loaded"]
    outside = sorted(p for p in paths if not p.startswith(bundle + os.sep))
    return [f"oqs: using {', '.join(outside)}, not the one shipped in the build"] if outside else []


def run_self_test():
    """What --self-test does inside the program being tested. By the time it
    runs, main.py has already imported everything a start imports, so a
    library that will not load has failed the run. What is left is the one
    thing main.py only loads when it opens the window: tkinter, whose Tcl/Tk
    libraries are the ones a build on a newer system can leave unloadable.
    Returns the exit code."""
    try:
        import tkinter  # noqa: F401
    except Exception as e:      # an ImportError, or an OSError from a library that will not load
        problems = [f"tkinter: {e}"]
    else:
        problems = []
    try:
        import resources
        with open(os.path.join(resources.bundle_dir(), "VERSION"), encoding="utf-8") as f:
            version = f.read().strip()
        frozen = resources.is_frozen()
        bundle = os.path.realpath(resources.bundle_dir())
    except Exception:
        version, frozen, bundle = "unknown", False, ""
    if frozen:
        # oqs falls back to a liboqs installed on the machine when the one in
        # the build will not load, so a build that is broken here still passes
        # on any machine that happens to have its own. Check which one it is.
        try:
            import oqs  # noqa: F401
            problems += _liboqs_outside(bundle)
        except Exception as e:
            problems.append(f"oqs: {e}")
    if problems:
        print("lapsecoin %s self-test FAILED\n%s" % (version, "\n".join(problems)))
        return 1
    print("lapsecoin %s %s" % (version, SELF_TEST_OK))
    return 0
