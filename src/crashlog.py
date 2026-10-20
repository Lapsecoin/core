"""A trace for every way the program can die, including before it has a log.

The Windows build is a windowed program: it has no console, so an exception
before the log file exists, a sys.exit("Error: ...") or a crash in native code
used to leave nothing at all. This is installed as the first thing main.py
does and leaves two small files in a folder that can be written even when the
data folder cannot:

  lapsecoin-start.log   one line per start-up step of the latest run, so a
                        silent death shows where it got to
  lapsecoin-crash.log   every fatal exception and native crash, appended

It also sends the report itself, so nobody has to relay it. Only a critical
crash is sent: an exception that ends the program, at start-up or while it runs,
and a native crash that killed the process, which the next start sends. The text
is cut to the exception, the last start-up steps, the version, the Windows build
and the architecture, with the account name, folder paths and any LapseCoin
address taken out. It goes to a small relay (tools/crash-relay) that files it as
a GitHub issue, once per kind of crash. Nothing is sent when no relay address was
baked into the build, when LAPSECOIN_CRASH_REPORTS=0 is set, or when a file named
crash-reports-off is in the data folder. No window is shown for a crash.

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

CRASH_LOG = "lapsecoin-crash.log"
START_LOG = "lapsecoin-start.log"

SENT_MARK = "lapsecoin-crash.sent"      # how much of the crash log was already sent
OPT_OUT_FILE = "crash-reports-off"
URL_FILE = "CRASH_URL"                  # written into the build from CRASH_REPORT_URL
SEND_TIMEOUT = 4
MAX_TRACE = 3500
MAX_STEPS = 1200
# A LapseCoin address is twelve dot-separated words.
ADDRESS_RE = re.compile(r"\b(?:[a-z]{3,10}\.){11}[a-z]{3,10}\b")
PENDING_RE = re.compile(r"Fatal Python error|Windows fatal exception|fatal exception during")

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
    return ADDRESS_RE.sub("<address>", text)


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


def _message_box(text, title):
    """A plain Windows message box, for a notice that has no other way out."""
    if not interactive():
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40 | 0x40000)
    except Exception:
        pass


def endpoint():
    """The relay's address, baked into the build (empty: nothing is sent)."""
    url = os.environ.get("LAPSECOIN_CRASH_REPORT_URL", "").strip()
    if url:
        return url
    try:
        import resources
        with open(os.path.join(resources.bundle_dir(), URL_FILE)) as f:
            return f.read().strip()
    except OSError:
        return ""


def sending_enabled():
    if not endpoint() or os.environ.get("LAPSECOIN_CRASH_REPORTS") == "0":
        return False
    return not any(os.path.exists(os.path.join(d, OPT_OUT_FILE)) for d in candidate_dirs())


def signature(exc_type, tb):
    """Short id of a kind of crash: the exception and where it was raised, so
    one fault is one issue however many people hit it."""
    import hashlib
    where = ""
    while tb is not None:
        where = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
        tb = tb.tb_next
    return hashlib.sha1(f"{exc_type.__name__}@{where}".encode()).hexdigest()[:10]


def _start_log_tail(lines=15):
    try:
        with open(os.path.join(_state["dir"], START_LOG), encoding="utf-8") as f:
            return "".join(f.readlines()[-lines:])
    except (OSError, TypeError):
        return ""


def build_payload(kind, where, trace, sig):
    """What is sent: nothing beyond this."""
    import platform
    try:
        import resources
        version = open(os.path.join(resources.bundle_dir(), "VERSION")).read().strip()
    except Exception:
        version = "unknown"
    return {"v": 1, "kind": kind, "where": where, "sig": sig,
            "app": version, "windows": platform.version(), "arch": platform.machine(),
            "python": platform.python_version(), "packaged": _packaged(),
            "trace": sanitize(trace)[-MAX_TRACE:],
            "steps": sanitize(_start_log_tail())[-MAX_STEPS:]}


def send(payload):
    """POSTs the report to the relay. True when it was accepted. Never raises."""
    url = endpoint()
    if not url or not sending_enabled():
        return False
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "LapseCoin-crash/1"})
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _mark_sent():
    """Everything in the crash log so far has been sent."""
    if not _state["dir"]:
        return
    try:
        size = os.path.getsize(os.path.join(_state["dir"], CRASH_LOG))
        with open(os.path.join(_state["dir"], SENT_MARK), "w") as f:
            f.write(str(size))
    except OSError:
        pass


def _short_hash(text):
    import hashlib
    # the crash text without times and addresses, so a repeat gets the same id
    stable = re.sub(r"0x[0-9a-fA-F]+|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "", text)
    return hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()[:4]


def send_pending():
    """Sends what an earlier run recorded and could not send: a native crash
    that ended the process, or a report made while offline."""
    if not _state["dir"] or not sending_enabled():
        return False
    crash = os.path.join(_state["dir"], CRASH_LOG)
    try:
        offset = int(open(os.path.join(_state["dir"], SENT_MARK)).read().strip() or 0)
    except (OSError, ValueError):
        offset = 0
    try:
        with open(crash, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            text = f.read()
    except OSError:
        return False
    if not PENDING_RE.search(text):
        _mark_sent()
        return False
    ok = send(build_payload("earlier-run", "an earlier run", text, "run" + _short_hash(text)))
    if ok:
        _mark_sent()
    return ok


def report_fatal(exc_type, exc, tb, where="start-up"):
    """Records a critical crash and sends it. Shows nothing."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    write_report(f"fatal exception during {where}:\n{text}")
    breadcrumb(f"fatal: {exc_type.__name__}: {exc}")
    sent = send(build_payload("fatal", where, text, signature(exc_type, tb)))
    if sent:
        _mark_sent()
    breadcrumb("report sent" if sent else "report not sent, it is sent by the next start")


def report_current(where):
    """For an except block that catches a critical failure itself."""
    exc_type, exc, tb = sys.exc_info()
    if exc_type is not None:
        report_fatal(exc_type, exc, tb, where)


def notify(text):
    """Shows a message that would otherwise go to a console that is not
    there (an Error: message from sys.exit, a failed data move)."""
    breadcrumb(f"notice: {text.splitlines()[0] if text else ''}")
    if active():
        _message_box(text, "LapseCoin")


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
    # What an earlier run left behind goes out in the background, so start-up
    # does not wait for the network.
    threading.Thread(target=send_pending, name="crash-pending", daemon=True).start()
    if "--crash-test" in sys.argv:
        # Proves the whole path in a real package: the CI test starts the
        # program with this flag and looks for the report it must leave.
        raise RuntimeError("crash test requested with --crash-test")
