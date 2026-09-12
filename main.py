# PYTHON_ARGCOMPLETE_OK
"""Entry point. Creates PeerPool, UDPTransport, Discovery, Gossip, Syncer, Node, API."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "src"))

import argparse
import collections
import getpass
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

import argcomplete
from argcomplete.completers import FilesCompleter

import block as block_mod
import crypto
import http_probe
import info_probe
import params
import upnp
from api import create_app, create_private_app
from discovery import Discovery
from gossip import Gossip
from node import Node
from params import DB_PATH
from peer_udp import LAN_DISCOVERY_PORT, PORT_BIND_RETRIES, UDPTransport, probe_lan_ports
from peerpool import PeerPool
from syncer import Syncer
from singleton_lock import SingleInstanceLock
from update_check import DEFAULT_RELEASES_URL, DEFAULT_VERSION_URL, UpdateChecker
from uptime_rewarder import UptimeRewarder
from version import LOCAL_VERSION

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


def _resolve_passphrase(prompt):
    env_pass = os.environ.get("LAPSECOIN_PASSPHRASE")
    if env_pass:
        return env_pass
    return getpass.getpass(prompt)


def _load_or_create_key(keyfile):
    """Returns (pk, kek, passphrase). The passphrase is handed back for
    symmetry with the GUI path; main discards it immediately, since nothing
    past startup needs it. There is one key file and one address."""
    if not os.path.exists(keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = _resolve_passphrase("New passphrase: ")
        if not os.environ.get("LAPSECOIN_PASSPHRASE"):
            passphrase = _prompt_new_passphrase(passphrase)
        sk, pk = crypto.generate_keypair()
        crypto.save_key(keyfile, sk, pk, passphrase)
        kek = crypto.derive_kek(keyfile, passphrase)
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key created  file=%s", keyfile)
        log.info("[startup] address=%s", addr)
        del sk
        return pk, kek, passphrase
    passphrase = _resolve_passphrase("Passphrase: ")
    try:
        pk = crypto.load_pubkey(keyfile)
        kek = crypto.derive_kek(keyfile, passphrase)
        sk_test = crypto.decrypt_secret_key(keyfile, kek=kek)
        del sk_test
    except ValueError as e:
        sys.exit(f"Error: {e}")
    log.info("[startup] key loaded  file=%s", keyfile)
    return pk, kek, passphrase


def main():
    parser = argparse.ArgumentParser(description="LapseCoin node")
    parser.add_argument("--host",    default="0.0.0.0")
    # An explicit --port still wins: the flag is the more specific of the
    # two, and a unit file setting the variable should not override what
    # someone typed on the command line to override it.
    parser.add_argument("--port",    type=int,
                        default=_env_port("LAPSECOIN_PORT", 8333))
    parser.add_argument("--keyfile", default="lapsecoin_key.json"
                        ).completer = FilesCompleter()
    parser.add_argument("--db",      default=DB_PATH
                        ).completer = FilesCompleter()
    parser.add_argument("--peer",       action="append", default=[])
    parser.add_argument(
        "--private-port", type=int, default=None,
        help="Port for private API (send/burn). Defaults to --port+2.",
    )
    parser.add_argument(
        "--max-peers", type=int, default=params.MAX_PEERS,
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--update-check-url", default=DEFAULT_VERSION_URL,
        help="Raw VERSION file URL to poll for newer releases. "
             "Point this at your own fork's VERSION file if you maintain one.",
    )
    parser.add_argument(
        "--releases-url", default=DEFAULT_RELEASES_URL,
        help="Where the UI's 'new version available' link points.",
    )
    parser.add_argument(
        "--no-update-check", action="store_true",
        help="Disable the periodic check for newer releases entirely.",
    )
    parser.add_argument(
        "--no-gui", action="store_true",
        help="Always use the console (passphrase prompt, Ctrl+C to stop) "
             "instead of the desktop status window. Implied automatically "
             "when LAPSECOIN_PASSPHRASE is set (headless/server runs).",
    )
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    logging.getLogger("ec").setLevel(getattr(logging, args.log_level))
    # The HTTP access log is one line per request, and requests arrive on a
    # timer: the dashboard polls /api/info, and so does every peer's
    # reachability prober. At INFO it buries the node's own output, so it
    # belongs with the rest of the detail rather than in the running
    # commentary. Asking for DEBUG asks for it.
    if args.log_level == "DEBUG":
        logging.getLogger("werkzeug").setLevel(logging.INFO)

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

    use_gui = not args.no_gui and not os.environ.get("LAPSECOIN_PASSPHRASE")
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
    if use_gui:
        pk, kek, passphrase = gui.load_or_create_key_gui(args.keyfile)
    else:
        pk, kek, passphrase = _load_or_create_key(args.keyfile)
    genesis  = block_mod.create_genesis()
    pk_hex   = pk.hex()

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

    def on_alive(note, sender_addr, stemming=False):
        _offer({"type": "alive", "note": note,
                "sender": sender_addr, "stemming": stemming}, "alive")

    def on_peers(peer_list, sender_addr):
        for p in peer_list:
            if isinstance(p, str) and ":" in p:
                discovery.enqueue_candidate(p)
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
    discovery = Discovery(udp, pool, genesis["hash"], port, pk_hex)
    node      = Node(args.keyfile, pk, gossip, syncer, pool, net_in_q, db_path=args.db)
    del passphrase   # nothing past startup needs it; the kek is what signs

    def _chain_provider(from_h, to_h):
        chain = node.view.chain
        end   = (to_h + 1) if to_h is not None else None
        return chain[from_h:end]

    def _tip_provider():
        chain = node.view.chain
        tip   = chain[-1]
        return (tip.get("height", 0), tip.get("hash", ""),
                LOCAL_VERSION, node.cs.cumulative_iterations)

    udp.set_alive_callback(on_alive)
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

    # Off by default; toggled and its budget edited from the private
    # dashboard's /rewards page. Its background thread sleeps a full cycle
    # (an hour, by default) before its first run, so it's never racing
    # node.start() below for the signing key, that's always up first.
    rewarder = UptimeRewarder(node)
    rewarder.start()

    # ------------------------------------------------------------------
    # HTTP servers: browser UI only, no peer routes
    # ------------------------------------------------------------------
    private_port = args.private_port if args.private_port else port + 2

    app = create_app(node, pool, private_port=private_port,
                     public_port=port, update_checker=update_checker,
                     rewarder=rewarder)
    _serve(app, args.host, port)
    log.info("[startup] public API on http://%s:%d", args.host, port)

    private_app = create_private_app(node, pool, private_port=private_port,
                                     public_port=port, update_checker=update_checker,
                                     rewarder=rewarder)
    _serve(private_app, "127.0.0.1", private_port)
    log.info("[startup] private API on http://127.0.0.1:%d (send/burn)", private_port)
    log.info("[startup] genesis=%s", genesis["hash"][:12])

    if pool.count() > 0:
        syncer.check_and_sync(
            node.cs.chain,
            lambda chain: node.apply_better_chain(chain)[0],
        )

    if use_gui:
        threading.Thread(target=node.start, kwargs={"kek": kek}, daemon=True).start()
        gui.run_status_window(node, udp, private_port, LOG_FILE)
    else:
        try:
            node.start(kek=kek)
        except KeyboardInterrupt:
            log.info("[shutdown] stopped")
            node.stop()
            udp.stop()


def _prompt_new_passphrase(first=None):
    while True:
        p1 = first if first else getpass.getpass("New passphrase: ")
        first = None
        if len(p1) < 8:
            print("Passphrase must be at least 8 characters.")
            continue
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases do not match.")


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
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as e:
        _show_fatal_error(e)
        sys.exit(1)
