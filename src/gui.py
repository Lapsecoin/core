"""Desktop GUI: the passphrase prompt and a status window, replacing the
console for a double-clicked binary. Not used at all under --no-gui or when
LAPSECOIN_PASSPHRASE is set (headless/server runs keep the console path in
main.py unchanged). The look is shared with the first-run question through
gui_theme.py.

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
import gui_theme as theme
from version import LOCAL_VERSION

log = logging.getLogger("ec.gui")


class _PassphraseDialog:
    """Blocking passphrase entry with inline retry, a wrong passphrase or
    a too-short new one re-shows the same dialog with an error message
    instead of crashing or reopening a fresh window each attempt."""

    def __init__(self, keyfile, preset_passphrase=None):
        self.keyfile = keyfile
        self.is_new = not os.path.exists(keyfile)
        self.result = None  # (pk, kek, passphrase) on success

        theme.make_dpi_aware()
        self.root = tk.Tk()
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        theme.install(self.root, "LapseCoin")
        pad = theme.px(self.root, 28)

        frame = ttk.Frame(self.root, padding=pad)
        frame.pack(fill="both", expand=True)
        theme.header(frame, "LapseCoin").pack(anchor="w")

        if self.is_new:
            title = "Create your wallet"
            msg = "No key file found. Choose a passphrase for your new wallet."
        else:
            title = "Unlock your wallet"
            msg = "Enter your wallet passphrase."
        ttk.Label(frame, text=title, style="H2.TLabel").pack(
            anchor="w", pady=(theme.px(self.root, 22), 2))
        ttk.Label(frame, text=msg, style="Muted.TLabel", wraplength=theme.px(self.root, 360)
                  ).pack(anchor="w")

        self.error_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.error_var, style="Error.TLabel",
                  wraplength=theme.px(self.root, 360)).pack(anchor="w", pady=(4, 0))

        ttk.Label(frame, text="Passphrase", style="Dim.TLabel").pack(
            anchor="w", pady=(theme.px(self.root, 12), 3))
        self.pass1 = tk.StringVar()
        if preset_passphrase is not None:
            self.pass1.set(preset_passphrase)
        entry1 = ttk.Entry(frame, textvariable=self.pass1, show="*", width=36, style="App.TEntry")
        entry1.pack(anchor="w", fill="x")

        self.pass2 = None
        if self.is_new:
            ttk.Label(frame, text="Confirm passphrase", style="Dim.TLabel").pack(
                anchor="w", pady=(theme.px(self.root, 12), 3))
            self.pass2 = tk.StringVar()
            if preset_passphrase is not None:
                self.pass2.set(preset_passphrase)
            ttk.Entry(frame, textvariable=self.pass2, show="*", width=36,
                      style="App.TEntry").pack(anchor="w", fill="x")

        btns = ttk.Frame(frame)
        btns.pack(anchor="e", pady=(theme.px(self.root, 22), 0))
        ttk.Button(btns, text="Cancel", style="Secondary.TButton", command=self._cancel
                   ).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Continue", style="Primary.TButton", command=self._submit
                   ).pack(side="left")

        entry1.bind("<Return>", lambda e: self._submit())
        entry1.focus_set()
        theme.center(self.root)
        if preset_passphrase is not None:
            self.root.after(0, self._submit)

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


def load_or_create_key_gui(keyfile, preset_passphrase=None):
    """GUI equivalent of main._load_or_create_key. Returns
    (pk, kek, passphrase), or exits the process if the user cancels."""
    result = _PassphraseDialog(keyfile, preset_passphrase).run()
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

    theme.ensure_png_exists()
    try:
        from PIL import Image

        # lapsecoin.png is bundled at 512px (see Makefile's `icons` target);
        # downsize ourselves with high-quality resampling to a real
        # tray-icon size rather than handing the OS a large image and
        # letting it scale down, which is what produced a blurry/pixelated
        # result, most Linux tray implementations don't use good
        # downsampling on their own. No cairosvg/libcairo needed here: see
        # gui_theme._load_logo's docstring for why that dependency isn't reliable on
        # a Windows build.
        raw = Image.open(theme.ICON_PNG).convert("RGBA")

        # Some AppIndicator/Ayatana implementations don't composite true
        # transparency correctly and render it as solid black instead of the
        # panel's own background. Rather than leaving the icon transparent
        # and hoping the backend handles it, flatten it onto an opaque
        # square in the window colour, worst case it's a small paper-
        # coloured square with the logo, not a broken black one.
        flattened = Image.new("RGBA", raw.size, theme.PAPER)
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
    theme.make_dpi_aware()
    root = tk.Tk()
    theme.install(root, f"LapseCoin v{LOCAL_VERSION}", minsize=(520, 420))
    pad = theme.px(root, 28)
    gap = theme.px(root, 12)

    outer = ttk.Frame(root, padding=pad)
    outer.pack(fill="both", expand=True)

    top = ttk.Frame(outer)
    top.pack(fill="x")
    theme.header(top, "LapseCoin", f"Version {LOCAL_VERSION}").pack(side="left")
    live = ttk.Frame(top)
    live.pack(side="right", anchor="n")
    ttk.Label(live, text="\u25cf Node is running", style="Live.TLabel").pack(anchor="e")

    height_var = tk.StringVar(value="…")
    peers_var = tk.StringVar(value="…")
    mempool_var = tk.StringVar(value="…")
    sync_var = tk.StringVar(value="100%")
    activity_var = tk.StringVar(value="starting…")
    error_var = tk.StringVar(value="")

    tiles = ttk.Frame(outer)
    tiles.pack(fill="x", pady=(theme.px(root, 22), 0))
    tiles.columnconfigure((0, 1), weight=1, uniform="tile")

    def _tile(row, col, label, var):
        box = theme.card(tiles)
        box.grid(row=row, column=col, sticky="nsew",
                 padx=(0 if col == 0 else gap // 2, 0 if col == 1 else gap // 2),
                 pady=(0 if row == 0 else gap // 2, 0 if row == 1 else gap // 2))
        inner = ttk.Frame(box, style="Card.TFrame", padding=theme.px(root, 16))
        inner.pack(fill="both", expand=True)
        ttk.Label(inner, text=label, style="CardDim.TLabel").pack(anchor="w")
        ttk.Label(inner, textvariable=var, style="Figure.TLabel").pack(anchor="w")

    _tile(0, 0, "Height", height_var)
    _tile(0, 1, "Peers", peers_var)
    _tile(1, 0, "Mempool", mempool_var)
    _tile(1, 1, "Sync", sync_var)

    wrap = theme.px(root, 460)
    ttk.Label(outer, textvariable=activity_var, style="Muted.TLabel", wraplength=wrap
              ).pack(anchor="w", pady=(theme.px(root, 16), 0))
    ttk.Label(outer, textvariable=error_var, style="Error.TLabel", wraplength=wrap
              ).pack(anchor="w", pady=(4, 0))

    _sync_start_h = -1
    _sync_start_t = 0
    _bps = 0.0

    def refresh():
        nonlocal _sync_start_h, _sync_start_t, _bps
        import time
        try:
            info = node.get_info()
            now = time.time()
            h = info.get("height", 0)
            target = info.get("sync_target", h)
            if target > h:
                if _sync_start_h == -1 or h < _sync_start_h:
                    _sync_start_h = h
                    _sync_start_t = now
                elif h > _sync_start_h and now > _sync_start_t:
                    _bps = (h - _sync_start_h) / (now - _sync_start_t)
            else:
                _sync_start_h = -1
            height_var.set(f"{h:,}")
            peers_var.set(f"{info['peer_count']:,}")
            mempool_var.set(f"{info['mempool_size']:,}")
            pct = info.get("sync_percent", 100)
            target = info.get("sync_target", h)
            if pct < 100 and _bps > 0 and target > h:
                sec = int((target - h) / _bps)
                if sec > 3600:
                    eta = f" ({sec//3600}h {(sec%3600)//60}m)"
                elif sec > 60:
                    eta = f" ({sec//60}m {sec%60}s)"
                else:
                    eta = f" ({sec}s)"
                sync_var.set(f"{pct}% {eta}")
            else:
                sync_var.set(f"{pct}%")
            activity_var.set(info.get("status") or "")
            error_var.set("")
        except Exception as e:
            error_var.set(f"status unavailable: {e}")
        root.after(3000, refresh)

    btns = ttk.Frame(outer)
    btns.pack(fill="x", side="bottom", pady=(theme.px(root, 20), 0))
    ttk.Button(
        btns, text="Open Node", style="Primary.TButton",
        command=lambda: webbrowser.open(f"http://127.0.0.1:{private_port}"),
    ).pack(side="left", padx=(0, 8))
    ttk.Button(
        btns, text="Logs", style="Secondary.TButton",
        command=lambda: _open_path(log_file),
    ).pack(side="left", padx=(0, 8))
    ttk.Button(
        btns, text="Quit", style="Secondary.TButton", command=lambda: _quit(),
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

    theme.center(root)
    refresh()
    root.mainloop()
