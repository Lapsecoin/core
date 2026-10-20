"""A trace for every way the program can die, including before it has a log.

The Windows build is a windowed program: it has no console, so an exception
before the log file exists, a sys.exit("Error: ...") or a crash in native code
used to leave nothing at all. This is installed as the first thing main.py
does and leaves two small files in a folder that can be written even when the
data folder cannot:

  lapsecoin-start.log   one line per start-up step of the latest run, so a
                        silent death shows where it got to
  lapsecoin-crash.log   every fatal exception and native crash, appended

It also shows the error in a message box with an offer to open a GitHub issue
pre-filled with the report. Nothing is sent anywhere unless the person clicks
Yes, and the issue form shows the whole text first to read and edit.

Only the Windows installer builds use it (the MSI and the Store package). A
source checkout, pip, the AppImage and a console run keep printing tracebacks.
"""

import os
import re
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import quote

CRASH_LOG = "lapsecoin-crash.log"
START_LOG = "lapsecoin-start.log"
ISSUE_URL = "https://github.com/Lapsecoin/core/issues/new"
# A GitHub link carries the whole issue text, and long links are refused.
MAX_BODY_CHARS = 5000
DIALOG_TITLE = "LapseCoin stopped unexpectedly"

_state = {"dir": None, "crash": None, "installed": False}


def active():
    """True for an installed Windows build. LAPSECOIN_CRASHLOG=1 turns it on
    elsewhere, for tests."""
    if os.environ.get("LAPSECOIN_CRASHLOG"):
        return True
    if not sys.platform.startswith("win"):
        return False
    import resources
    return resources.is_frozen()


def candidate_dirs(environ=None):
    environ = os.environ if environ is None else environ
    home = environ.get("USERPROFILE") or os.path.expanduser("~")
    return [os.path.join(home, "LapseCoin"), home, tempfile.gettempdir()]


def pick_dir(candidates=None):
    """The first folder that can be written, or None."""
    for d in (candidates or candidate_dirs()):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".crashlog-probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return d
        except OSError:
            continue
    return None


def sanitize(text, environ=None):
    """The report with the person's account name taken out of file paths."""
    environ = os.environ if environ is None else environ
    # the longer paths first, or the profile folder would swallow them
    for key in ("LOCALAPPDATA", "APPDATA", "TEMP", "USERPROFILE"):
        value = environ.get(key)
        if value:
            text = text.replace(value, "%" + key + "%")
    user = environ.get("USERNAME")
    if user and len(user) > 2:
        text = re.sub(re.escape(user), "<user>", text, flags=re.IGNORECASE)
    return text


def environment_lines():
    try:
        import resources
        version = open(os.path.join(resources.bundle_dir(), "VERSION")).read().strip()
    except Exception:
        version = "unknown"
    import platform
    return [f"LapseCoin {version}",
            f"Windows {platform.version()} {platform.machine()}",
            f"Python {platform.python_version()}",
            f"packaged={_packaged()}  argv={' '.join(sys.argv[1:])}"]


def _packaged():
    try:
        import datadir
        return datadir.is_packaged()
    except Exception:
        return "unknown"


def issue_url(title, body):
    """A GitHub new-issue link carrying the report, cut to fit."""
    body = sanitize(body)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS // 2] + "\n[...cut...]\n" + body[-MAX_BODY_CHARS // 2:]
    return f"{ISSUE_URL}?title={quote(title)}&body={quote(body)}"


def _stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def breadcrumb(message):
    """One line in lapsecoin-start.log. Never raises."""
    if not _state["installed"] or not _state["dir"]:
        return
    try:
        with open(os.path.join(_state["dir"], START_LOG), "a", encoding="utf-8") as f:
            f.write(f"{_stamp()} {message}\n")
    except OSError:
        pass


def write_report(text):
    """Appends to lapsecoin-crash.log and returns its path, or None."""
    if not _state["dir"]:
        return None
    path = os.path.join(_state["dir"], CRASH_LOG)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {_stamp()} =====\n" + "\n".join(environment_lines()) + "\n" + text + "\n")
        return path
    except OSError:
        return None


NO_WINDOW_FLAGS = ("--no-gui", "--export", "--update", "-h", "--help", "--check-restart-exe")


def interactive():
    """False for a run that must never wait for a click: headless, an update
    check, help, or LAPSECOIN_NO_DIALOGS set."""
    return not (os.environ.get("LAPSECOIN_NO_DIALOGS")
                or any(a in sys.argv for a in NO_WINDOW_FLAGS))


def _message_box(text, title, ask):
    """Shows a Windows message box. Returns True when Yes was chosen."""
    if not interactive():
        return False
    try:
        import ctypes
        flags = 0x10 | 0x40000                      # error icon, topmost
        flags |= 0x4 if ask else 0                  # Yes and No
        return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == 6
    except Exception:
        return False


def report_fatal(exc_type, exc, tb, where="start-up"):
    """Logs a fatal exception, shows it, and offers a GitHub issue."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    path = write_report(f"fatal exception during {where}:\n{text}")
    breadcrumb(f"fatal: {exc_type.__name__}: {exc}")
    summary = f"{exc_type.__name__}: {exc}"
    saved = f"\n\nThe details were saved to:\n{path}" if path else ""
    shown = _message_box(
        f"LapseCoin could not continue.\n\n{summary}{saved}\n\n"
        "Open a GitHub issue with this report? You can read and edit it "
        "before anything is sent.",
        DIALOG_TITLE, ask=True)
    if shown:
        try:
            body = "\n".join(environment_lines()) + f"\n\n```\n{text}\n```\n"
            os.startfile(issue_url(f"Crash: {summary}"[:100], body))
        except Exception:
            pass


def notify(text):
    """Shows a message that would otherwise go to a console that is not
    there (an Error: message from sys.exit, a failed data move)."""
    breadcrumb(f"notice: {text.splitlines()[0] if text else ''}")
    if active():
        _message_box(text, "LapseCoin", ask=False)


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc, tb)
        return
    report_fatal(exc_type, exc, tb)


def _thread_excepthook(args):
    if issubclass(args.exc_type, SystemExit):
        return
    # A background thread failing is logged, not shown: the node goes on.
    write_report("exception in thread "
                 f"{getattr(args.thread, 'name', '?')}:\n"
                 + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def enable_faulthandler():
    """Python's traceback for a native crash goes to the crash log. Loading
    liboqs switches the handler off, so main.py calls this again after it."""
    if not _state["crash"]:
        return
    try:
        import faulthandler
        faulthandler.enable(file=_state["crash"], all_threads=True)
    except Exception:
        pass


def install():
    """Called first thing by main.py. Does nothing outside an installed
    Windows build."""
    if _state["installed"] or not active():
        return
    _state["installed"] = True
    _state["dir"] = pick_dir()
    if _state["dir"]:
        try:
            # A fresh start log for every run, so it only ever holds this one.
            open(os.path.join(_state["dir"], START_LOG), "w").close()
            _state["crash"] = open(os.path.join(_state["dir"], CRASH_LOG), "a", buffering=1)
        except OSError:
            _state["crash"] = None
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    enable_faulthandler()
    breadcrumb("start " + " | ".join(environment_lines()))
    if "--crash-test" in sys.argv:
        # Proves the whole path in a real package: the CI test starts the
        # program with this flag and looks for the report it must leave.
        raise RuntimeError("crash test requested with --crash-test")
