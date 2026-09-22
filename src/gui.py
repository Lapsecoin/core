"""Minimal desktop GUI: a small status window with one button, replacing the
console for a double-clicked binary. Not used at all under --no-gui or when
LAPSECOIN_PASSPHRASE is set (headless/server runs keep the console path in
main.py unchanged).

Two things this exists to get right, per real feedback from testing the
console-only version: closing the window must for-sure kill the whole
process (not just the window), and a wrong passphrase must be retryable
in place rather than crashing out.
"""

import logging
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from tkinter import ttk

import crypto

log = logging.getLogger("ec.gui")


def _resource_dir():
    """Directory to look for bundled assets in. Tries a few candidate
    locations rather than assuming one exact layout, since PyInstaller's
    onedir output layout has changed across versions (6.x moved bundled
    data into an _internal/ subfolder next to the executable rather than
    flat alongside it), and the AppImage wrapping step in the Makefile
    adds another layer on top of that."""
    candidates = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(sys._MEIPASS)
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(exe_dir)
        candidates.append(os.path.join(exe_dir, "_internal"))
    else:
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for c in candidates:
        if os.path.exists(os.path.join(c, "lapsecoin.svg")):
            return c
    # Nothing found, return the first candidate anyway so callers get a
    # consistent (if wrong) path and _apply_icon's own try/except handles
    # the resulting failure gracefully rather than crashing here.
    return candidates[0] if candidates else "."


_ICON_PNG = os.path.join(_resource_dir(), "lapsecoin.png")


def _apply_icon(root):
    """Set the window icon from the already-bundled lapsecoin.png.

    Previously this rasterized lapsecoin.svg at runtime via cairosvg, which
    needs libcairo, a native library lapsecoin.spec never actually bundles
    (no collect_all/collect_dynamic_libs for it, unlike nacl/cffi/oqs/
    chiavdf), so it silently fails on any Windows machine without Cairo
    already installed system-wide. lapsecoin.png is a plain build-time-
    generated PNG (see Makefile's `icons` target) that PIL can open directly
    with no native dependency beyond Pillow, which is already required and
    properly bundled. Failure here should never take down the GUI itself,
    worst case the window just keeps whatever default icon the OS/tkinter
    picks."""
    try:
        from PIL import Image, ImageTk

        img = Image.open(_ICON_PNG)
        photo = ImageTk.PhotoImage(img)
        root.iconphoto(True, photo)
        root._icon_photo = photo  # keep a reference; tkinter drops GC'd PhotoImages
    except Exception as e:
        log.debug("[gui] could not set window icon: %s", e)


def _install_tk_exception_logging(root):
    """Route Tkinter's own callback-exception reporting through our logger.

    Once inside root.mainloop(), any exception raised in a widget command,
    an after() callback, etc. is caught by Tkinter itself and handed to
    report_callback_exception, whose default implementation tries to print
    to sys.stderr. On a windowed (console=False) Windows build launched by
    double-click, sys.stderr is None (see main.py's own comment on this),
    so that default handler's own print() fails, and the exception it was
    trying to report vanishes with it, the app just closes with nothing
    in the log. Logging here instead means a future crash of this kind
    actually shows up in LOG_FILE."""
    def _report(exc, val, tb):
        import traceback
        log.error("[gui] unhandled exception in Tk callback:\n%s",
                  "".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = _report


class _PassphraseDialog:
    """Blocking passphrase entry with inline retry, a wrong passphrase or
    a too-short new one re-shows the same dialog with an error message
    instead of crashing or reopening a fresh window each attempt."""

    def __init__(self, keyfile):
        self.keyfile = keyfile
        self.is_new = not os.path.exists(keyfile)
        self.result = None  # (pk, kek, passphrase) on success

        self.root = tk.Tk()
        _install_tk_exception_logging(self.root)
        self.root.title("LapseCoin")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        _apply_icon(self.root)
        _style_dark(self.root)

        frame = ttk.Frame(self.root, style="Dark.TFrame", padding=18)
        frame.pack(fill="both", expand=True)

        if self.is_new:
            msg = "No key file found. Choose a passphrase for your new wallet."
        else:
            msg = "Enter your wallet passphrase."
        ttk.Label(frame, text=msg, style="Dark.TLabel", wraplength=280).pack(anchor="w")

        self.error_var = tk.StringVar()
        ttk.Label(
            frame, textvariable=self.error_var, background=_BG, foreground="#ff6b6b",
        ).pack(anchor="w")

        ttk.Label(frame, text="Passphrase", style="Dim.TLabel").pack(anchor="w", pady=(10, 2))
        self.pass1 = tk.StringVar()
        entry1 = ttk.Entry(frame, textvariable=self.pass1, show="*", width=30, style="Dark.TEntry")
        entry1.pack(anchor="w")

        self.pass2 = None
        if self.is_new:
            ttk.Label(frame, text="Confirm passphrase", style="Dim.TLabel").pack(anchor="w", pady=(10, 2))
            self.pass2 = tk.StringVar()
            ttk.Entry(
                frame, textvariable=self.pass2, show="*", width=30, style="Dark.TEntry",
            ).pack(anchor="w")

        btns = ttk.Frame(frame, style="Dark.TFrame")
        btns.pack(anchor="e", pady=(16, 0))
        ttk.Button(btns, text="Cancel", style="Plain.TButton", command=self._cancel).pack(side="left", padx=(0, 6))
        submit = ttk.Button(btns, text="Continue", style="Accent.TButton", command=self._submit)
        submit.pack(side="left")

        entry1.bind("<Return>", lambda e: self._submit())
        entry1.focus_set()

    def _cancel(self):
        self.result = None
        self.root.destroy()

    def _submit(self):
        p1 = self.pass1.get()
        if self.is_new:
            if len(p1) < 8:
                self.error_var.set("Passphrase must be at least 8 characters.")
                return
            if p1 != self.pass2.get():
                self.error_var.set("Passphrases do not match.")
                self.pass2.set("")
                return
            try:
                sk, pk = crypto.generate_keypair()
                crypto.save_key(self.keyfile, sk, pk, p1)
                kek = crypto.derive_kek(self.keyfile, p1)
                addr = crypto.public_key_to_address(pk)
                del sk
                log.info("[startup] key created  file=%s", self.keyfile)
                log.info("[startup] address=%s", addr)
            except Exception as e:
                self.error_var.set(f"Could not create key: {e}")
                return
            self.result = (pk, kek, p1)
            self.root.destroy()
            return

        try:
            pk = crypto.load_pubkey(self.keyfile)
            kek = crypto.derive_kek(self.keyfile, p1)
            sk_test = crypto.decrypt_secret_key(self.keyfile, kek=kek)
            del sk_test
        except ValueError:
            self.error_var.set("Incorrect passphrase. Try again.")
            self.pass1.set("")
            return
        except Exception as e:
            self.error_var.set(f"Could not load key: {e}")
            return
        log.info("[startup] key loaded  file=%s", self.keyfile)
        self.result = (pk, kek, p1)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def load_or_create_key_gui(keyfile):
    """GUI equivalent of main._load_or_create_key. Returns
    (pk, kek, passphrase), or exits the process if the user cancels."""
    result = _PassphraseDialog(keyfile).run()
    if result is None:
        sys.exit(0)
    return result


def _open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606, local file, user's own log
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:
        log.warning("[gui] could not open %s: %s", path, e)


_BG = "#1c1f26"
_BG_PANEL = "#242832"
_FG = "#e7e9ee"
_FG_DIM = "#9aa1af"
_ACCENT = "#5b8cff"


def _style_dark(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg=_BG)
    style.configure("Dark.TFrame", background=_BG)
    style.configure("Dark.TLabel", background=_BG, foreground=_FG)
    style.configure("Dim.TLabel", background=_BG, foreground=_FG_DIM)
    style.configure(
        "Accent.TButton", background=_ACCENT, foreground="white",
        padding=(10, 6), borderwidth=0,
    )
    style.map("Accent.TButton", background=[("active", "#4674e0")])
    style.configure(
        "Plain.TButton", background=_BG_PANEL, foreground=_FG,
        padding=(10, 6), borderwidth=0,
    )
    style.map("Plain.TButton", background=[("active", "#2e3340")])
    style.configure(
        "Dark.TEntry", fieldbackground=_BG_PANEL, foreground=_FG,
        insertcolor=_FG, borderwidth=0, padding=6,
    )
    return style


def _make_tray_icon(node, on_open, on_quit):
    """Build a pystray icon so the window can fully vanish (no taskbar
    entry) while the node keeps running, with a way back in. Returns None
    if pystray isn't available/usable on this platform. Callers must
    fall back to just minimizing instead of hiding in that case, so the
    user is never left with literally no way to reopen the window."""
    try:
        import pystray
    except ImportError:
        print(
            "\nSystem tray icon unavailable: the 'pystray' package isn't installed.\n"
            "Install it with:\n"
            "  pip install pystray\n"
            "(or re-run: pip install -r requirements.txt)\n"
            "The window will minimize to the taskbar instead for now.\n"
        )
        return None

    try:
        from PIL import Image

        # lapsecoin.png is bundled at 512px (see Makefile's `icons` target);
        # downsize ourselves with high-quality resampling to a real
        # tray-icon size rather than handing the OS a large image and
        # letting it scale down, which is what produced a blurry/pixelated
        # result, most Linux tray implementations don't use good
        # downsampling on their own. No cairosvg/libcairo needed here: see
        # _apply_icon's docstring for why that dependency isn't reliable on
        # a Windows build.
        raw = Image.open(_ICON_PNG).convert("RGBA")

        # Some AppIndicator/Ayatana implementations don't composite true
        # transparency correctly and render it as solid black instead of the
        # panel's own background. Rather than leaving the icon transparent
        # and hoping the backend handles it, flatten it onto an opaque
        # square matching the app's own dark theme, worst case it's a
        # small dark square with the logo, not a broken black one.
        flattened = Image.new("RGBA", raw.size, _BG)
        flattened.paste(raw, (0, 0), raw)
        img = flattened.resize((32, 32), Image.LANCZOS)

        # Note: AppIndicator/Ayatana-based trays (GNOME/Ubuntu default) don't
        # have a separate "left click to open" action the way Windows does,
        # any click always opens this menu. `default=True` only matters on
        # backends that do support a distinct default click (Windows/macOS).
        menu = pystray.Menu(
            pystray.MenuItem("Open LapseCoin", on_open, default=True),
            pystray.MenuItem("Quit", on_quit),
        )
        return pystray.Icon("lapsecoin", img, "LapseCoin", menu)
    except Exception as e:
        # pystray itself is installed, but building/showing the icon failed,
        # on Linux this is usually a missing tray backend (AppIndicator/GTK),
        # separate from the pip package.
        print(
            f"\nSystem tray icon unavailable ({e}); the window will minimize "
            "to the taskbar instead.\n"
            "On Linux this is usually a missing tray backend, try:\n"
            "  Debian/Ubuntu:  sudo apt install gir1.2-ayatanaappindicator3-0.1\n"
            "  Fedora:         sudo dnf install libappindicator-gtk3\n"
        )
        return None


def run_status_window(node, udp, private_port, log_file):
    """Status window: live height/peers/mempool, a button into the local
    node UI, one to the log file.

    The X button no longer quits the process. It hides the window. If a
    system tray icon is available (pystray), the window disappears from
    the taskbar entirely and reopens from the tray icon; the node keeps
    running in the background either way. If pystray isn't usable on this
    platform, X falls back to a normal minimize so there's always a way
    back to the window. The only way to actually end the process is the
    explicit Quit button (or the tray menu's Quit).

    Several background components (peer_udp's callback pool, discovery's,
    the periodic HTTP reachability prober) run non-daemon ThreadPoolExecutor
    workers that can otherwise keep the interpreter alive waiting to join
    them even after node.stop()/udp.stop(), os._exit sidesteps that
    entirely rather than trying to track down and gracefully join every
    one of them.
    """
    root = tk.Tk()
    _install_tk_exception_logging(root)
    root.title("LapseCoin")
    root.minsize(440, 300)
    _apply_icon(root)
    style = _style_dark(root)

    outer = ttk.Frame(root, style="Dark.TFrame", padding=16)
    outer.pack(fill="both", expand=True)

    header = ttk.Frame(outer, style="Dark.TFrame")
    header.pack(fill="x")
    ttk.Label(
        header, text="LapseCoin", style="Dark.TLabel", font=("", 15, "bold"),
    ).pack(anchor="w")
    ttk.Label(
        header, text="Node is running", style="Dim.TLabel", font=("", 9),
    ).pack(anchor="w", pady=(0, 10))

    status_frame = ttk.Frame(outer, style="Dark.TFrame")
    status_frame.pack(fill="x", pady=(0, 12))

    height_var = tk.StringVar(value="—")
    peers_var = tk.StringVar(value="—")
    mempool_var = tk.StringVar(value="—")
    sync_var = tk.StringVar(value="100%")
    activity_var = tk.StringVar(value="starting…")
    error_var = tk.StringVar(value="")

    def _stat_row(row, label, var):
        ttk.Label(status_frame, text=label, style="Dim.TLabel").grid(
            row=row, column=0, sticky="w", pady=3,
        )
        ttk.Label(
            status_frame, textvariable=var, style="Dark.TLabel", font=("Consolas", 11, "bold"),
        ).grid(row=row, column=1, sticky="e", padx=(24, 0), pady=3)

    status_frame.columnconfigure(1, weight=1)
    _stat_row(0, "Height", height_var)
    _stat_row(1, "Peers", peers_var)
    _stat_row(2, "Mempool", mempool_var)
    _stat_row(3, "Sync", sync_var)
    ttk.Label(
        status_frame, textvariable=activity_var, style="Dim.TLabel",
        wraplength=360, font=("", 9, "italic"),
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Label(status_frame, textvariable=error_var, style="Dim.TLabel", wraplength=360).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(6, 0),
    )

    def refresh():
        try:
            info = node.get_info()
            height_var.set(f"{info['height']:,}")
            peers_var.set(f"{info['peer_count']:,}")
            mempool_var.set(f"{info['mempool_size']:,}")
            sync_var.set(f"{info.get('sync_percent', 100)}%")
            activity_var.set(info.get("status") or "")
            error_var.set("")
        except Exception as e:
            error_var.set(f"status unavailable: {e}")
        root.after(3000, refresh)

    btns = ttk.Frame(outer, style="Dark.TFrame")
    btns.pack(fill="x", side="bottom")
    ttk.Button(
        btns, text="Open Node", style="Accent.TButton",
        command=lambda: webbrowser.open(f"http://127.0.0.1:{private_port}"),
    ).pack(side="left", padx=(0, 8))
    ttk.Button(
        btns, text="Logs", style="Plain.TButton",
        command=lambda: _open_path(log_file),
    ).pack(side="left", padx=(0, 8))
    ttk.Button(
        btns, text="Quit", style="Plain.TButton", command=lambda: _quit(),
    ).pack(side="right")

    tray_icon = None  # set below if available

    def _show():
        root.deiconify()
        root.lift()
        root.focus_force()

    def _hide():
        root.withdraw()

    def _quit():
        log.info("[shutdown] quit requested")
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        try:
            node.stop()
        except Exception:
            pass
        try:
            udp.stop()
        except Exception:
            pass
        root.destroy()
        os._exit(0)

    def _on_tray_open(icon=None, item=None):
        root.after(0, _show)

    def _on_tray_quit(icon=None, item=None):
        root.after(0, _quit)

    tray_icon = _make_tray_icon(node, _on_tray_open, _on_tray_quit)

    if tray_icon is not None:
        # run_detached() lets pystray pick the correct run strategy per
        # platform (some backends need the main thread) instead of us
        # forcing a bare background thread that only some backends tolerate.
        try:
            tray_icon.run_detached()
            root.protocol("WM_DELETE_WINDOW", _hide)
        except Exception as e:
            print(f"\nCould not start tray icon ({e}); minimizing to taskbar instead.\n")
            tray_icon = None
            root.protocol("WM_DELETE_WINDOW", root.iconify)
    else:
        # No tray available on this platform/setup, fall back to a plain
        # minimize so the user always has a way back to the window rather
        # than losing it with no path to reopen.
        root.protocol("WM_DELETE_WINDOW", root.iconify)

    refresh()
    root.mainloop()
