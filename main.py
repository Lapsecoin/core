# PYTHON_ARGCOMPLETE_OK
"""Entry point. Creates PeerPool, UDPTransport, Discovery, Gossip, Syncer, Node, API."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "src"))

# The uninstaller runs this before it removes the program. It only shows a
# notice, so it must not start the node or ask to migrate old data.
if "--uninstall-notice" in _sys.argv[1:]:
    import uninstall_notice as _uninstall_notice
    _uninstall_notice.run()
    _sys.exit(0)

# Leaves a trace for any way the program dies from here on, including before
# the log file exists (see crashlog.py). It has to come before anything that can fail.
import crashlog as _crashlog
_crashlog.install()

# An installed Windows build keeps its data under %USERPROFILE%\LapseCoin, not beside
# the program. This has to run before anything opens a relative path.
import datadir as _datadir
_datadir.prepare()
_crashlog.breadcrumb(f"data folder ready: {_os.getcwd()}")

import resources as _resources
if _resources.is_frozen():
    _resources.prepare_oqs()

import argparse
import collections
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

import argcomplete
from argcomplete.completers import FilesCompleter

import base_wallet
import block as block_mod
import gas_worker
import http_probe
import info_probe
import params
import upnp
from api import create_app, create_private_app
from discovery import Discovery
from gossip import Gossip
from node import Node
from params import DB_PATH, PEERS_PER_MESSAGE_LIMIT
from peer_udp import LAN_DISCOVERY_PORT, PORT_BIND_RETRIES, UDPTransport, probe_lan_ports
from peerpool import PeerPool
from public_url import parse_public_url
from syncer import Syncer
from singleton_lock import SingleInstanceLock
from update_check import DEFAULT_RELEASES_URL, DEFAULT_VERSION_URL, UpdateChecker
from updater import Updater
from version import LOCAL_VERSION
import decisions as decisions_mod
import startup
import wallet as wallet_mod

LOG_FILE = "lapsecoin.log"
LOCK_FILE = "lapsecoin.lock"

# LOG_FILE is capped at this size, past which it rolls into LOG_FILE.1,
# pushing .1 to .2 and so on up to LOG_FILE_BACKUPS, oldest dropped. Two
# things need bounding here, not one: a log that just keeps growing across
# restarts buries the current run under every previous one, and a single
# run left up for weeks (the normal, correct thing to do with a node)
# would otherwise grow LOG_FILE forever with nothing capping it at all.
# RotatingFileHandler's own maxBytes check handles the second; forcing one
# rollover at every startup, below, handles the first, so LOG_FILE always
# opens on the current run whether that run just started or has been up
# for a month.
#
# Total disk this can ever hold is bounded: at most
# LOG_FILE_SIZE_LIMIT * (LOG_FILE_BACKUPS + 1).
LOG_FILE_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB
LOG_FILE_BACKUPS = 4

# A second, much smaller file: just the newest LOG_TAIL_LINES lines,
# newest at the top. LOG_FILE is the complete record and has to stay
# oldest-first and append-only, since rewriting a growing multi-megabyte
# file to put new lines first would mean re-writing everything after
# them, on every single line logged, an ever-growing cost for no
# functional gain. This file is kept deliberately small instead, and its
# rewrites are coalesced (see _NewestFirstTailHandler), so the cost stays
# flat regardless of how long the node has been running or how much it is
# logging. That's what actually buys "open it and see what's happening now
# without scrolling."
LOG_TAIL_FILE = "lapsecoin.log.latest"
LOG_TAIL_LINES = 300
# How often the tail file is rewritten at most. Anything at WARNING or
# above still writes straight through, see _NewestFirstTailHandler.
LOG_TAIL_FLUSH_SECONDS = 1.0

# WSGI server sizing (see _serve). Both apps are read-mostly and their
# handlers are short; the dashboard polls /api/info on a timer and every
# peer's reachability prober hits it too, so this is about absorbing many
# small concurrent requests rather than long ones.
WSGI_THREADS = 8
WSGI_CHANNEL_TIMEOUT = 30

# The Windows build is console=False (no console window for the default GUI
# double-click experience). That leaves two things to handle before any
# logging or output happens: sys.stdout/stderr can be None for a windowed
# app with no console attached at all (a documented PyInstaller/Windows
# behavior, not a bug), which would crash a bare StreamHandler() on first
# write; and if the app *was* launched from an existing terminal (someone
# explicitly running --no-gui from cmd/PowerShell), attaching to that
# console is what makes prompts and log lines actually show up there
# instead of going nowhere.
if sys.platform.startswith("win"):
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # -1 = ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")
            sys.stdin = open("CONIN$", "r")
    except Exception:
        pass  # no parent console (double-clicked) or attach failed either way

_log_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=LOG_FILE_SIZE_LIMIT, backupCount=LOG_FILE_BACKUPS)
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
    # Force a rollover so this run starts LOG_FILE fresh even if the last
    # run never grew it anywhere near maxBytes, which is the ordinary
    # case: most runs end well short of 10MB. Without this, LOG_FILE would
    # keep appending across restarts and only ever roll on size, so a
    # short-lived previous run's tail would still be sitting at the top of
    # the file the next run opens.
    _log_file_handler.doRollover()
class _NewestFirstTailHandler(logging.Handler):
    """Keeps LOG_TAIL_FILE as just the newest LOG_TAIL_LINES lines,
    newest at the top.

    A deque(maxlen=...) does the actual capping: once full, appendleft
    silently drops whatever falls off the far end, which is the oldest
    line, exactly once per new one arriving, no separate trim step. The
    file itself is never read back to figure out what to keep, this
    handler's own deque is the only source of truth for its contents.

    Newest-first means the file has to be written whole, since there is no
    such thing as prepending to a file. What does not follow is writing it
    whole on every single record, which is what this did: about 30KB
    rewritten per line, synchronously, holding the logging lock, so every
    thread that logs waits on it. At --log-level DEBUG the UDP layer logs
    a line per datagram and this became the busiest writer in the process.
    Rewrites are coalesced instead: at most one every
    LOG_TAIL_FLUSH_SECONDS, plus one immediately for anything at WARNING
    or above, so the file a person opens to see what just went wrong is
    still current the moment it goes wrong.
    """

    def __init__(self, path, max_lines, flush_seconds):
        super().__init__()
        self.path = path
        self.lines = collections.deque(maxlen=max_lines)
        self._flush_seconds = flush_seconds
        self._last_write = 0.0

    def emit(self, record):
        try:
            self.lines.appendleft(self.format(record))
            now = time.monotonic()
            if (record.levelno >= logging.WARNING
                    or now - self._last_write >= self._flush_seconds):
                self._write(now)
        except Exception:
            self.handleError(record)

    def _write(self, now=None):
        with open(self.path, "w") as f:
            f.write("\n".join(self.lines))
            f.write("\n")
        self._last_write = time.monotonic() if now is None else now

    def flush(self):
        # Called by logging.shutdown() at exit, so whatever the coalescing
        # was still holding lands rather than being lost on the way out.
        try:
            if self.lines:
                self._write()
        except Exception:
            pass


_log_handlers = [
    _log_file_handler,
    _NewestFirstTailHandler(LOG_TAIL_FILE, LOG_TAIL_LINES, LOG_TAIL_FLUSH_SECONDS),
]
if sys.stderr is not None:
    _log_handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=_log_handlers,
)
logging.getLogger("ec").setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


class _WerkzeugFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return ("BitTorrent" not in msg and
                "Bad request version" not in msg and
                "Bad HTTP" not in msg)


logging.getLogger("werkzeug").addFilter(_WerkzeugFilter())
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("ec.main")
_crashlog.enable_faulthandler()      # loading liboqs switched Python's handler off
_crashlog.breadcrumb("modules loaded, log file open")


def _how(flag, env=None):
    """Where a value came from, for the Decisions page: the option, the
    variable, or the default."""
    if any(a == flag or a.startswith(flag + "=") for a in sys.argv[1:]):
        return flag
    if env and os.environ.get(env):
        return "$" + env
    return "the default"


def _env_port(name, fallback):
    """A port from the environment, or `fallback` if it isn't set.

    Checked here rather than left to argparse so a bad value says which
    variable is wrong and why. A container that mistypes this would
    otherwise either fail with a message naming --port, which was never
    set, or bind somewhere unintended.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")
    if not 1 <= port <= 65535:
        raise SystemExit(f"{name} must be between 1 and 65535, got {port}")
    return port


def _run_console_update(args):
    """--update: checks once, offers to update if this install can do so
    safely, and exits either way. Deliberately doesn't start the node,
    servers, or GUI -- a plain, fast, separate path from a normal launch,
    for someone who just wants to update from a terminal without opening
    a browser."""
    checker = UpdateChecker(
        local_version=LOCAL_VERSION,
        version_url=args.update_check_url,
        releases_url=args.releases_url,
    )
    checker.check_once()
    if not checker.severity:
        print(f"Already up to date (v{LOCAL_VERSION}).")
        return

    print(f"\nA newer version is available: v{checker.latest_version} "
          f"(current: v{LOCAL_VERSION}, severity: {checker.severity}).\n")

    updater = Updater()
    if not updater.can_self_update():
        print(f"This is a {updater.install_type!r} install; it can't update itself.")
        print(f"Get the new version from: {updater.update_url(checker.releases_url)}")
        return

    answer = input("Update now? [y/N] ").strip().lower()
    if answer != "y":
        print(f"Not updating. Releases page: {checker.releases_url}")
        return

    def on_progress(stage, detail):
        print(f"[{stage}] {detail}")

    # A successful attempt replaces this process image or hands off to a
    # helper and exits from inside run_sync() itself.
    session = updater.run_sync(checker.latest_version, on_progress=on_progress)
    print(f"\nUpdate failed: {session.error}")
    print(f"You can update manually from: {checker.releases_url}")
    sys.exit(1)


def _serve(app, host, port):
    """Serve a Flask app on its own daemon thread.

    waitress rather than app.run(), which starts Werkzeug's development
    server. Flask's own documentation says not to put that on a network
    others can reach, and one of these two apps is bound to whatever
    interface the operator gave and is the externally reachable port.
    waitress is pure Python with no build step, so it bundles under
    PyInstaller the same as everything else here (see lapsecoin.spec).

    Threads rather than processes because both apps read node.view, the
    snapshot the node loop publishes in-process; there is nothing to serve
    from another one.
    """
    from waitress import serve

    threading.Thread(
        target=lambda: serve(app, host=host, port=port,
                             threads=WSGI_THREADS, ident=None,
                             # An unresponsive or slow client should not be
                             # able to hold a worker indefinitely.
                             channel_timeout=WSGI_CHANNEL_TIMEOUT),
        daemon=True,
    ).start()






def main():
    import settings as settings_mod
    setting_vars = "\n".join("  " + x.env_name for x in settings_mod.ALL)
    parser = argparse.ArgumentParser(
        description="LapseCoin node: mines, relays and serves the explorer and your wallet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Who the node runs as (the command line wins over the environment, which wins\n"
            "over what is in the folder):\n"
            "  1. --address, --key or --account on the command line (one of them)\n"
            "  2. $LAPSECOIN_ADDRESS or $LAPSECOIN_KEY (one of them)\n"
            "  3. lapsecoin_wallet.json, then an old lapsecoin_key.json to move, then a new wallet\n"
            "\n"
            "When it asks for the passphrase: never with --address; with --key before it starts;\n"
            "with a wallet, on its private pages (window or no terminal) or at the prompt\n"
            "(terminal). A new wallet, an old key file to move and an unconfirmed recovery\n"
            "phrase ask first. Without a window or a terminal, set LAPSECOIN_PASSPHRASE.\n"
            "\n"
            "Environment variables:\n"
            "  LAPSECOIN_ADDRESS            same as --address\n"
            "  LAPSECOIN_KEY                same as --key\n"
            "  LAPSECOIN_PASSPHRASE         the passphrase of the wallet or key, for starts nobody\n"
            "                               can type at\n"
            "  LAPSECOIN_EXPORT_PASSPHRASE  the new passphrase --export seals a key under\n"
            "  LAPSECOIN_PORT               same as --port\n"
            "  LAPSECOIN_LOG_LEVEL          same as --log-level\n"
            "  LAPSECOIN_PUBLIC_URL         same as --public-url\n"
            "\n"
            "Settings (each can also be set on the Settings page, which shows which are set from\n"
            "the environment):\n"
            + setting_vars + "\n"
        ),
    )

    who = parser.add_argument_group(
        "who this node runs as (give at most one)",
        "Each of these says who the node is. With none, it uses the wallet in this folder.")
    who.add_argument("--address", dest="address", default=None, metavar="ADDRESS",
                     help="Mine for this address and hold no key: its 12 words (dots, spaces or "
                          "commas between them). No passphrase and no signing; the node builds "
                          "blocks and the reward goes to the address. Give each machine its own "
                          "address. (env: LAPSECOIN_ADDRESS)")
    who.add_argument("--key", dest="key", default=None, metavar="KEY",
                     help="Run as one exported account: the lc1- text that --export prints. It "
                          "asks for that key's passphrase before starting, and the wallet file "
                          "is not used. (env: LAPSECOIN_KEY)")
    who.add_argument("--account", default=None, metavar="N|NAME",
                     help="Run as this address of the wallet file here: its number, or the name "
                          "you gave it on the Addresses page. Default: address 0.")

    cmds = parser.add_argument_group(
        "wallet commands (work on the wallet file, do one thing and exit)")
    cmds.add_argument("--export", nargs="?", const="", default=None, metavar="N|NAME",
                      help="Print one address's key as short lc1- text, sealed under a new "
                           "passphrase, and exit: its number or name (default: --account, else "
                           "0). A name no address has yet creates that address, so --export rig1 "
                           "and --export rig2 give two machines an address each. Use the text as "
                           "--key on that machine; it never holds the recovery phrase.")
    cmds.add_argument("--new-address", action="store_true",
                      help="Add the wallet's next address, print it and exit.")
    cmds.add_argument("--restore", action="store_true",
                      help="Rebuild the wallet from a recovery phrase (typed hidden, or piped "
                           "in) into an empty folder and exit.")

    net = parser.add_argument_group("network")
    net.add_argument("--host", default="0.0.0.0", metavar="HOST",
                     help="Interface the public explorer and API listen on (default 0.0.0.0, all "
                          "of them). The private wallet port only ever listens on 127.0.0.1.")
    # An explicit --port still wins: the flag is the more specific of the
    # two, and a unit file setting the variable should not override what
    # someone typed on the command line to override it.
    net.add_argument("--port", type=int, default=_env_port("LAPSECOIN_PORT", 8333), metavar="PORT",
                     help="Peer (UDP) and public explorer (TCP) port, the same number "
                          "(default 8333). (env: LAPSECOIN_PORT)")
    net.add_argument("--private-port", type=int, default=None, metavar="PORT",
                     help="Port of the private wallet pages, on 127.0.0.1 (default: --port + 2).")
    net.add_argument("--peer", action="append", default=[], metavar="HOST:PORT",
                     help="A node to try first. Repeat for several.")
    net.add_argument("--max-peers", type=int, default=params.MAX_PEERS, metavar="N",
                     help=f"Most peers to keep (default {params.MAX_PEERS}).")
    net.add_argument("--public-url", default=os.environ.get("LAPSECOIN_PUBLIC_URL"), metavar="URL",
                     help="The https:// address this node is reached at, when a TLS-terminating "
                          "reverse proxy (Caddy, nginx) with a domain sits in front of its web "
                          "port. The link previews of its pages use it for their addresses, since "
                          "a proxy often hides the scheme from the node. (env: LAPSECOIN_PUBLIC_URL)")

    data = parser.add_argument_group("data and logs")
    data.add_argument("--db", default=DB_PATH, metavar="FILE",
                      help=f"Chain database (default {DB_PATH}, in the current folder)."
                      ).completer = FilesCompleter()
    data.add_argument("--log-level", default=os.environ.get("LAPSECOIN_LOG_LEVEL", "INFO"),
                      choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                      help="How much to log (default INFO). (env: LAPSECOIN_LOG_LEVEL)")

    app = parser.add_argument_group("window and updates")
    app.add_argument("--no-gui", action="store_true",
                     help="Always use the console (a passphrase prompt if it needs one, Ctrl+C to "
                          "stop) instead of the desktop status window.")
    app.add_argument("--update", action="store_true",
                     help="Check for a newer release and, if this install is one that can safely "
                          "update itself (a prebuilt binary/AppImage, or a `pip install "
                          "lapsecoin`), offer to do so, then exit. Does nothing to a source "
                          "checkout beyond printing the releases URL: never touches a git "
                          "working tree.")
    app.add_argument("--no-update-check", action="store_true",
                     help="Disable the periodic check for newer releases entirely.")
    app.add_argument("--update-check-url", default=DEFAULT_VERSION_URL, metavar="URL",
                     help="Raw VERSION file URL to poll for newer releases. Point this at your "
                          "own fork's VERSION file if you maintain one.")
    app.add_argument("--releases-url", default=DEFAULT_RELEASES_URL, metavar="URL",
                     help="Where the UI's 'new version available' link points.")
    parser.add_argument("--check-restart-exe", metavar="FILE", help=argparse.SUPPRESS)
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    if args.check_restart_exe:
        # CI only: checks the update restart target, starts nothing.
        import updater as _updater
        sys.exit(_updater.restart_check(args.check_restart_exe))
    account_given = args.account is not None     # "--account 0" is not the same as nothing
    if account_given and (args.restore or args.new_address):
        # Only --export NAME may make an address from a name; here a name nobody
        # has is a mistake to report, as it always was.
        try:
            wallet_mod.resolve_account(args.account)
        except ValueError as e:
            sys.exit(f"Error: --account: {e}")
    if args.address and (args.restore or args.new_address or args.export is not None):
        sys.exit("Error: --address runs a node that holds no key; --restore, --new-address "
                 "and --export work on the recovery-phrase wallet. Run them without it.")
    if args.restore:
        wallet_mod.cmd_restore()
        sys.exit(0)
    if args.new_address:
        wallet_mod.cmd_new_address(args.key)
        sys.exit(0)
    if args.export is not None:
        wallet_mod.cmd_export(args.key, args.export or (args.account if account_given else 0))
        sys.exit(0)
    public_url = None
    if args.public_url:
        try:
            public_url = parse_public_url(args.public_url)
        except ValueError as e:
            sys.exit(f"Error: --public-url: {e}")
    logging.getLogger("ec").setLevel(getattr(logging, args.log_level))
    # The HTTP access log is one line per request, and requests arrive on a
    # timer: the dashboard polls /api/info, and so does every peer's
    # reachability prober. At INFO it buries the node's own output, so it
    # belongs with the rest of the detail rather than in the running
    # commentary. Asking for DEBUG asks for it.
    if args.log_level == "DEBUG":
        logging.getLogger("werkzeug").setLevel(logging.INFO)

    # Who the node runs as, settled before anything starts (ports, window,
    # lock) so a conflict between what was given is reported at once. The rules
    # are in wallet.find_source; when it asks for a passphrase is startup.plan.
    notes = []
    try:
        source = wallet_mod.find_source(args.key, address_text=args.address,
                                        account_given=account_given, notes=notes)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    try:
        args.account = wallet_mod.resolve_account(args.account) if source.kind == "wallet" else 0
    except ValueError as e:
        sys.exit(f"Error: --account: {e}")
    wallet_notes = []     # what matched the wallet file here, told apart from what was passed over
    if source.kind == "address":
        wallet_notes += wallet_mod.notes_about_local_wallet("address", source.value)
        if os.environ.get(wallet_mod.PASSPHRASE_ENV):
            notes.append(f"${wallet_mod.PASSPHRASE_ENV} is ignored: an address has no key to unlock")
    for note in notes + wallet_notes:
        log.info("[startup] %s", note)

    # Single-instance guard, checked before anything else (ports, GUI,
    # passphrase prompt, tray icon) so a second launch exits fast and
    # cleanly. Uses an OS-level advisory file lock rather than a PID
    # file: the OS releases it automatically the instant the owning
    # process exits for any reason, including a crash, so there's no
    # stale state to misdetect and nothing to clean up on the way out.
    # A genuine normal start is never blocked by this: it only refuses
    # to start while another instance is *actually still alive and
    # holding the lock*, and if the lock check itself fails for some
    # unrelated reason (permissions, filesystem quirk), it fails open.
    instance_lock = SingleInstanceLock(LOCK_FILE)
    if not instance_lock.acquire():
        pid = instance_lock.holder_pid()
        msg = (
            "LapseCoin is already running"
            + (f" (pid {pid})" if pid else "")
            + ".\n\nOnly one instance can run at a time, since it holds "
              "the same wallet key and chain database. Check your system "
              "tray, it's likely already there."
        )
        print(msg)
        try:
            # Best-effort GUI popup if tkinter happens to be available,
            # so a person who launched a second copy by double-clicking
            # sees something even without a console attached (relevant
            # on Windows/macOS app launches).
            import tkinter as _tk
            from tkinter import messagebox as _messagebox
            _root = _tk.Tk()
            _root.withdraw()
            _messagebox.showinfo("LapseCoin", msg)
            _root.destroy()
        except Exception:
            pass
        sys.exit(0)

    if args.update:
        # Checked after the single-instance lock (so this never races a
        # currently-running copy over the same files) but before anything
        # else -- GUI, passphrase, node startup -- since none of that is
        # needed just to update and exit.
        try:
            _run_console_update(args)
        finally:
            instance_lock.release()
        sys.exit(0)

    use_gui = not args.no_gui
    if use_gui and sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        log.info("[startup] no display found, running without the window")
        use_gui = False
    gui = None
    if use_gui:
        try:
            import gui
        except ImportError:
            # tkinter isn't a pip package, can't be listed in
            # requirements.txt or fixed by `pip install`. It's bundled
            # automatically in the prebuilt binary releases (PyInstaller)
            # and in the standard Windows/macOS Python installers, but is
            # commonly stripped from Linux distro python3 packages when
            # running from source. Rather than silently continuing without
            # the GUI the person asked for, offer to install it on the
            # spot, or stop and point at --no-gui as the explicit opt-out.
            import shutil as _shutil
            import subprocess as _subprocess

            installers = [
                ("apt-get", ["sudo", "apt-get", "install", "-y", "python3-tk"]),
                ("dnf",     ["sudo", "dnf", "install", "-y", "python3-tkinter"]),
                ("pacman",  ["sudo", "pacman", "-S", "--noconfirm", "tk"]),
            ]
            cmd = next((c for name, c in installers if _shutil.which(name)), None)

            print(
                "\ntkinter (needed for the desktop window) isn't installed.\n"
                + (f"Install command detected: {' '.join(cmd)}\n" if cmd else "")
                + "Options:\n"
                "  [y] install it now\n"
                "  [n] cancel, re-run with --no-gui to run headless instead\n"
            )
            answer = input("Install tkinter now? [y/N] ").strip().lower()
            if answer == "y" and cmd:
                result = _subprocess.run(cmd)
                if result.returncode == 0:
                    import gui  # retry now that it's installed
                else:
                    print("Install failed. Re-run with --no-gui, or install tkinter manually.")
                    sys.exit(1)
            elif answer == "y":
                print("No known install command for this system. Install tkinter manually, or re-run with --no-gui.")
                sys.exit(1)
            else:
                print("Not installing. Re-run with --no-gui to start headless.")
                sys.exit(1)
    # Who this node runs as, and whether it asks for a passphrase before
    # starting, after (on the private pages, see unlock_ui.py) or never. The
    # rules are in startup.py; every case is a line in tests/test_startup.py.
    env_passphrase = os.environ.get(wallet_mod.PASSPHRASE_ENV) or None
    try:
        start = startup.plan(source, args.account, gui=use_gui, tty=sys.stdin.isatty(),
                             env_passphrase=bool(env_passphrase))
    except ValueError as e:
        sys.exit(f"Error: {e}")
    phrase_unconfirmed = False       # read before the dialog shows it and marks it confirmed
    if source.kind == "wallet":
        try:
            phrase_unconfirmed = not wallet_mod.WalletFile.load(source.value).phrase_shown
        except ValueError:
            pass
    if start.how in (startup.WATCH, startup.DEFER):
        wallet, kek, passphrase = start.wallet, None, None
        if start.how == startup.WATCH:
            log.info("[startup] mining for %s and holding no key", wallet.addr)
        else:
            log.info("[startup] running as %s until the passphrase is entered", wallet.addr)
    elif use_gui:
        wallet, kek, passphrase = gui.startup_gui(
            args.key, args.account, env_passphrase, source=source)
    else:
        wallet, kek, passphrase = wallet_mod.startup_console(
            args.key, args.account, source=source)
    if source.kind == "key":
        wallet_notes += wallet_mod.notes_about_local_wallet("key", wallet.addr)
        for note in wallet_notes:
            log.info("[startup] %s", note)
    genesis  = block_mod.create_genesis()

    pool     = PeerPool(max_peers=args.max_peers)
    # Bounded so a flood can't grow memory without limit while the node
    # loop is busy. Sized from what can actually arrive between drains: the
    # loop drains about once a second, and peer_udp rate-limits each source
    # to RATE_LIMIT_PER_SEC, so a fully hostile peer table at MAX_PEERS can
    # offer roughly 50 * 125 = 6250 messages a second. This holds over a
    # second of that, which is well past any honest burst (a network-wide
    # block is one message per peer, a couple of minutes apart).
    net_in_q = queue.Queue(maxsize=10_000)

    # ------------------------------------------------------------------
    # UDP transport: single socket for all peer communication
    # ------------------------------------------------------------------
    def _offer(msg, kind):
        """Hand a peer message to the node loop, dropping it if the queue is
        already full rather than blocking.

        Blocking here would stall a UDP worker thread, which is how a flood
        turns into a transport-wide outage instead of some lost messages.
        Dropping is safe for everything that comes in this way: a block or
        tx we miss is re-offered by ordinary propagation, and the sender's
        own rework re-sends it if the loss mattered."""
        try:
            net_in_q.put_nowait(msg)
        except queue.Full:
            log.debug("[net] inbound queue full, dropping %s", kind)

    def on_block(block, sender_addr, stemming=False):
        _offer({"type": "block", "block": block,
                "sender": sender_addr, "stemming": stemming}, "block")

    def on_tx(tx, sender_addr, stemming=False):
        _offer({"type": "tx", "tx": tx,
                "sender": sender_addr, "stemming": stemming}, "tx")

    def on_peers(peer_list, sender_addr):
        # MT_PEERS reaches here from anyone who can put a UDP packet on
        # the wire with our (public, not secret) genesis hash attached,
        # admitted peer or not: the genesis check upstream in peer_udp.py
        # gates the protocol version, not who's allowed to speak. Without
        # this, one packet from an address we've never pinged could queue
        # an arbitrary number of ping/punch attempts against addresses it
        # chose, at our expense and toward whoever it named. Requiring the
        # sender to already be in our own pool means it already passed
        # ping/pong admission and the subnet diversity cap (PeerPool.add)
        # to get there, a far smaller set to abuse than "anyone". The
        # length cap matches what send_peers ever sends, so a compromised
        # or misbehaving admitted peer still can't hand us more than that.
        if not pool.is_known(sender_addr):
            return
        # Recorded as a claim regardless of whether we can reach every
        # address in it, wholesale-replacing whatever sender_addr claimed
        # last time: this is what lets the network page show a
        # relationship this node has real evidence of even toward an
        # address it never itself connected to (see PeerPool.record_claim).
        pool.record_claim(sender_addr, peer_list)
        for p in peer_list[:PEERS_PER_MESSAGE_LIMIT]:
            if isinstance(p, str) and ":" in p:
                discovery.enqueue_candidate(p, learned_from=sender_addr)
        pool.touch(sender_addr)

    # Ask the local network who's already running a node before claiming a
    # data port. Otherwise two machines behind the same router could both
    # default onto 8333, and only one of them can ever have that port
    # forwarded through the router at a time. Best-effort and bounded
    # (1.5s): no reply just means bind normally, same as always.
    log.info("[startup] checking this network for other LapseCoin nodes...")
    claimed_ports = probe_lan_ports(genesis["hash"])
    if claimed_ports:
        log.info("[startup] found node(s) already using port(s): %s",
                 sorted(claimed_ports))
    else:
        log.info("[startup] no other nodes found on this network")
    data_port = args.port
    while ((data_port in claimed_ports or data_port == LAN_DISCOVERY_PORT)
           and data_port < args.port + PORT_BIND_RETRIES):
        data_port += 1
    if data_port != args.port:
        log.info("[startup] port %d already active on this network, using %d instead",
                 args.port, data_port)

    udp = UDPTransport(
        port=data_port,
        genesis_hash=genesis["hash"],
        on_block=on_block,
        on_tx=on_tx,
        on_peers=on_peers,
        pool=pool,
    )
    udp.start()
    # udp.start() falls back further still if data_port itself turns out to
    # be taken on this machine (e.g. a second node instance, or a race with
    # the LAN probe above), everything downstream that needs to know our
    # port uses the port actually bound, not the one originally requested.
    port = udp.port
    log.info("[startup] UDP transport on port %d", port)

    # ------------------------------------------------------------------
    # Gossip, Syncer, Discovery, Node
    # ------------------------------------------------------------------
    gossip    = Gossip(pool, udp)
    syncer    = Syncer(pool, udp)
    discovery = Discovery(udp, pool, genesis["hash"], port, wallet.addr)
    private_port = args.private_port if args.private_port else port + 2
    del passphrase   # nothing past startup needs it; the kek is what signs

    # What was decided from what was given, for the Decisions page.
    decisions = decisions_mod.Decisions()
    wf = getattr(wallet, "walletfile", None)
    account_name = next((a.get("label", "") for a in wf.accounts if a["index"] == args.account),
                        "") if wf is not None and source.kind == "wallet" else ""
    decisions.add("Who this node is", decisions_mod.who(
        source, wallet.addr, args.account, account_name, account_given))
    decisions.add("Passphrase and signing", decisions_mod.passphrase(
        source, start.how, bool(env_passphrase), window=use_gui, phrase_unconfirmed=phrase_unconfirmed))
    decisions.add("Passed over", decisions_mod.passed_over(notes))
    decisions.add("About the wallet here", decisions_mod.about_local_wallet(wallet_notes))
    decisions.add("Network", decisions_mod.network(
        public_port=port, requested_port=args.port, port_how=_how("--port", "LAPSECOIN_PORT"),
        private_port=private_port, private_how="--private-port" if args.private_port else "",
        peers=args.peer,
        public_url=public_url, public_url_how=_how("--public-url", "LAPSECOIN_PUBLIC_URL")))
    decisions.add("Window and updates", decisions_mod.window_and_updates(
        window=use_gui, no_gui=args.no_gui, update_check=not args.no_update_check))
    decisions.add("Data and logs", decisions_mod.data(
        db_path=os.path.abspath(args.db), db_how=_how("--db"),
        log_level=args.log_level, log_how=_how("--log-level", "LAPSECOIN_LOG_LEVEL")))

    class NodeWrapper:
        def __init__(self):
            self.real_node = None
            self.update_checker = None   # set once start-up has made it; the window reads it
        def get_info(self):
            if self.real_node:
                return self.real_node.get_info()
            raise Exception("loading blockchain database...")
        def stop(self):
            if self.real_node:
                self.real_node.stop()

    node_wrapper = NodeWrapper()

    def background_startup():
        node = Node(wallet, gossip, syncer, pool, net_in_q, db_path=args.db)
        node.background_jobs = [j for j in (getattr(wallet, "migration", None),
                                            getattr(wallet, "discovery", None)) if j]
        node.needs_unlock = kek is None and wallet.can_sign
        node.decisions = decisions
        node_wrapper.real_node = node

        def _chain_provider(from_h, to_h):
            chain = node.view.chain
            end   = (to_h + 1) if to_h is not None else None
            return chain[from_h:end]

        def _tip_provider():
            chain = node.view.chain
            tip   = chain[-1]
            return (tip.get("height", 0), tip.get("hash", ""),
                    LOCAL_VERSION, node.cs.cumulative_iterations)

        udp.set_chain_provider(_chain_provider)
        udp.set_tip_provider(_tip_provider)

        for peer in args.peer:
            parts = peer.split(":")
            if len(parts) == 2:
                discovery.add_bootstrap_peer(f"{parts[0]}:{parts[1]}")

        threading.Thread(target=discovery.run, daemon=True).start()
        threading.Thread(target=http_probe.run, args=(pool,), daemon=True).start()
        threading.Thread(target=info_probe.run, args=(pool, udp), daemon=True).start()
        # Best-effort only: doesn't block startup, and the node works the same
        # whether or not this succeeds (see upnp.py).
        upnp.try_map_port(port)

        update_checker = UpdateChecker(
            local_version=LOCAL_VERSION,
            version_url="" if args.no_update_check else args.update_check_url,
            releases_url=args.releases_url,
        )
        update_checker.start()
        node_wrapper.update_checker = update_checker
        updater = Updater()

        # The gas wallet: one Base key sealed under the node's kek, created
        # on first start. It holds nothing until funded, so a fresh node is
        # not made to sit locked for lack of an address nobody has used.
        node.base_wallet_path = base_wallet.default_path()
        if kek is not None:
            base_wallet.ensure(node.base_wallet_path, kek)
        # Answers fee requests while the gas wallet holds enough to pay for
        # one; an operator who does not want to simply does not fund it.
        gas_worker.GasWorker(node, gas_worker.LiveIO(node)).start()

        # ------------------------------------------------------------------
        # HTTP servers: browser UI only, no peer routes
        # ------------------------------------------------------------------
        app = create_app(node, pool, private_port=private_port,
                         public_port=port, update_checker=update_checker,
                         updater=updater, public_url=public_url)
        _serve(app, args.host, port)
        log.info("[startup] public API on http://%s:%d", args.host, port)

        private_app = create_private_app(node, pool, private_port=private_port,
                                         public_port=port,
                                         update_checker=update_checker,
                                         updater=updater, public_url=public_url)
        _serve(private_app, "127.0.0.1", private_port)
        log.info("[startup] private API on http://127.0.0.1:%d (send/burn)", private_port)
        log.info("[startup] genesis=%s", genesis["hash"][:12])

        if pool.count() > 0:
            syncer.check_and_sync(
                node.cs.chain,
                node._sync_page_outcome,
            )

        if use_gui:
            threading.Thread(target=node.start, kwargs={"kek": kek}, daemon=True).start()
        else:
            try:
                node.start(kek=kek)
            except KeyboardInterrupt:
                log.info("[shutdown] stopped")
                node.stop()
                udp.stop()

    if use_gui:
        threading.Thread(target=background_startup, daemon=True).start()
        gui.run_status_window(node_wrapper, udp, private_port, LOG_FILE)
    else:
        background_startup()




def _show_fatal_error(exc):
    """Last-resort visibility for a crash that happens before (or instead
    of) the GUI window ever appears, with console=False on Windows,
    there's no console to print a traceback to, so silence here would mean
    the app just vanishes with zero explanation. Always logged to LOG_FILE
    first (that part can't fail silently), then tries progressively more
    primitive ways to actually show something on screen."""
    import traceback
    tb = traceback.format_exc()
    try:
        logging.getLogger("ec.main").error("[fatal] unhandled exception:\n%s", tb)
    except Exception:
        pass
    _crashlog.write_report("fatal exception in main():\n" + tb)

    message = f"LapseCoin failed to start.\n\n{exc}\n\nFull details in {LOG_FILE}."
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("LapseCoin - Error", message)
        root.destroy()
        return
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "LapseCoin - Error", 0x10)
            return
        except Exception:
            pass

    # Console/no-gui path, or every other fallback above failed: a normal
    # traceback on stderr is the expected and sufficient behavior there.
    print(tb, file=sys.stderr)


if __name__ == "__main__":
    _crashlog.breadcrumb("starting main()")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        # A message passed to sys.exit() goes to a console that a windowed
        # build does not have, so it is shown instead.
        if isinstance(e.code, str):
            _crashlog.notify(e.code)
        raise
    except Exception as e:
        _show_fatal_error(e)
        sys.exit(1)
