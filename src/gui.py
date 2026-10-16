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

import falcon_seeded
import gui_theme as theme
import update_check
from version import LOCAL_VERSION

log = logging.getLogger("ec.gui")


class _StartupDialog:
    """Blocking wallet start-up: unlock, or create a new wallet and show its
    recovery phrase once. A wrong passphrase or a too-short new one re-shows
    the same dialog with an error message instead of crashing or reopening a
    fresh window each attempt. The logic is wallet.py's; this is the window."""

    def __init__(self, source, account=0, preset_passphrase=None):
        self.source = source
        self.account = account
        self.is_new = source.kind == "new"
        self.result = None  # (wallet, kek, passphrase) on success
        self._pending = None

        theme.make_dpi_aware()
        self.root = tk.Tk()
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        theme.install(self.root, "LapseCoin")
        self.pad = theme.px(self.root, 28)
        self.preset = preset_passphrase
        self._ask()

    # The passphrase page
    def _fresh_frame(self):
        for child in self.root.winfo_children():
            child.destroy()
        frame = ttk.Frame(self.root, padding=self.pad)
        frame.pack(fill="both", expand=True)
        theme.header(frame, "LapseCoin").pack(anchor="w")
        return frame

    def _ask(self):
        preset = self.preset
        frame = self._fresh_frame()
        if self.is_new:
            title = "Create your wallet"
            msg = "No wallet found. Choose a passphrase for your new wallet."
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
        if preset is not None:
            self.pass1.set(preset)
        entry1 = ttk.Entry(frame, textvariable=self.pass1, show="*", width=36, style="App.TEntry")
        entry1.pack(anchor="w", fill="x")

        self.pass2 = None
        if self.is_new:
            ttk.Label(frame, text="Confirm passphrase", style="Dim.TLabel").pack(
                anchor="w", pady=(theme.px(self.root, 12), 3))
            self.pass2 = tk.StringVar()
            if preset is not None:
                self.pass2.set(preset)
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
        theme.bring_to_front(self.root)
        if preset is not None:
            self.preset = None      # a retry after a wrong preset asks properly
            self.root.after(0, self._submit)

    def _cancel(self):
        self.result = None
        self.root.destroy()

    def _submit(self):
        import wallet as wallet_mod
        p1 = self.pass1.get()
        if self.is_new:
            if len(p1) < wallet_mod.MIN_PASSPHRASE:
                self.error_var.set(
                    f"Passphrase must be at least {wallet_mod.MIN_PASSPHRASE} characters.")
                return
            if p1 != self.pass2.get():
                self.error_var.set("Passphrases do not match.")
                self.pass2.set("")
                return
            try:
                w, kek, phrase = wallet_mod.create_wallet(p1, self.source.value)
            except Exception as e:
                self.error_var.set(f"Could not create the wallet: {e}")
                return
            log.info("[startup] address=%s", w.addr)
            self._show_phrase(phrase, (w, kek, p1))
            return

        try:
            w, kek = wallet_mod.unlock(self.source, p1, self.account)
        except wallet_mod.WalletFileError as e:
            self.error_var.set(str(e))
            return
        except ValueError as e:
            self.error_var.set("Incorrect passphrase. Try again." if "passphrase" in str(e)
                               else str(e))
            self.pass1.set("")
            return
        except Exception as e:
            self.error_var.set(f"Could not load the wallet: {e}")
            return
        if self.source.kind == "legacy" and not falcon_seeded.available():
            log.warning("[startup] %s", wallet_mod.NO_SEEDED_LIBRARY)
            self.result = (w, kek, p1)
            self.root.destroy()
            return
        if self.source.kind == "legacy":
            try:
                w, kek, phrase = wallet_mod.begin_migration(p1, w, kek, self.source.value)
            except Exception as e:
                self.error_var.set(f"Could not create the new wallet: {e}")
                return
            log.info("[startup] address=%s", w.addr)
            self._show_phrase(phrase, (w, kek, p1), notice=wallet_mod.MIGRATION_NOTICE)
            return
        log.info("[startup] key loaded  kind=%s", self.source.kind)
        if self.source.kind == "wallet" and not w.walletfile.phrase_shown:
            self._show_phrase(w.walletfile.phrase(kek), (w, kek, p1))
            return
        self.result = (w, kek, p1)
        self.root.destroy()

    # The recovery phrase, shown once
    def _show_phrase(self, phrase, result, notice=None):
        self._pending = result
        frame = self._fresh_frame()
        if notice:
            ttk.Label(frame, text=notice.replace("\n", " "), style="Muted.TLabel",
                      wraplength=theme.px(self.root, 400)).pack(
                anchor="w", pady=(theme.px(self.root, 18), 0))
        ttk.Label(frame, text="Your recovery phrase", style="H2.TLabel").pack(
            anchor="w", pady=(theme.px(self.root, 22), 2))
        ttk.Label(frame, text="Write these words down. They restore every address of this "
                  "wallet on any machine, and they are shown only this once.",
                  style="Muted.TLabel", wraplength=theme.px(self.root, 400)).pack(anchor="w")
        grid = ttk.Frame(frame)
        grid.pack(anchor="w", pady=(theme.px(self.root, 14), 0))
        words = phrase.split()
        rows = (len(words) + 2) // 3
        for i, w in enumerate(words):
            col, row = divmod(i, rows)
            ttk.Label(grid, text=f"{i + 1:>2}.  {w}", style="Muted.TLabel").grid(
                row=row, column=col, sticky="w", padx=(0, theme.px(self.root, 22)), pady=2)
        btns = ttk.Frame(frame)
        btns.pack(anchor="e", pady=(theme.px(self.root, 22), 0))
        ttk.Button(btns, text="I have saved it", style="Primary.TButton",
                   command=self._phrase_done).pack(side="left")
        theme.center(self.root)

    def _phrase_done(self):
        w, kek, passphrase = self._pending
        try:
            w.walletfile.mark_phrase_shown()
        except Exception as e:
            log.warning("[startup] could not record that the phrase was shown: %s", e)
        self.result = (w, kek, passphrase)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def startup_gui(key_text=None, account=0, preset_passphrase=None, source=None):
    """GUI equivalent of wallet.startup_console. Returns
    (wallet, kek, passphrase), or exits the process if the user cancels."""
    import wallet as wallet_mod
    try:
        source = source or wallet_mod.find_source(key_text)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    result = _StartupDialog(source, account, preset_passphrase).run()
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


# What the Linux tray icon is flattened onto: a dark neutral square, which
# sits quietly on the default dark panels.
TRAY_FLAT_BG = "#1c1f26"


def _tray_image(raw, platform):
    """The 32px tray icon, from the full-size RGBA logo.

    Windows and macOS composite a transparent icon onto the taskbar
    themselves, so it keeps its transparency and takes the taskbar's own
    colour, light or dark. Some AppIndicator/Ayatana implementations on Linux
    do not composite transparency and draw it as solid black, so there the
    icon is flattened onto an opaque square instead."""
    from PIL import Image
    if platform.startswith("linux"):
        flat = Image.new("RGBA", raw.size, TRAY_FLAT_BG)
        flat.paste(raw, (0, 0), raw)
        raw = flat
    return raw.resize((32, 32), Image.LANCZOS)


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

        img = _tray_image(raw, sys.platform)

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
    notice_var = tk.StringVar(value="")
    ttk.Label(outer, textvariable=notice_var, style="Notice.TLabel", wraplength=wrap
              ).pack(anchor="w", pady=(4, 0))

    # Only says an update exists. Running it is the node page's job, where
    # the confirmation and progress already are.
    def _show_notice():
        checker = getattr(node, "update_checker", None)
        notice_var.set(update_check.notice_text(
            getattr(checker, "severity", None), getattr(checker, "latest_version", None)))

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
            _show_notice()
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
    theme.bring_to_front(root)
    refresh()
    root.mainloop()
