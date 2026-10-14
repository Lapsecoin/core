"""The look shared by every window the program opens (the passphrase prompt,
the status window and the first-run question), so they read as one program.

It follows the web UI (templates_html/css/base.css): warm paper, white cards
with a hairline border, one orange accent, large figures over small muted
labels. Everything is drawn with ttk's "clam" theme, the one built-in theme
that honours colours the same way on Windows, macOS and Linux, and fonts come
from the system's own default so each platform keeps its native typeface.

Tkinter and, optionally, Pillow for the logo. Nothing else is imported, so the
first-run question can use it before the rest of the program has loaded.
"""

import logging
import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

log = logging.getLogger("ec.gui")

# Mirrors the custom properties at the top of templates_html/css/base.css.
PAPER = "#f4f1ea"
SURFACE = "#fffefb"
INK = "#1a1813"
INK_DIM = "#6a6558"
LINE = "#d9d4c6"        # the hairline: --ink at low opacity over paper
LINE_STRONG = "#c4bda9"  # the edge of a button or field that must read as a control
HOVER = "#ebe7dc"
ACCENT = "#c2570c"
ACCENT_DARK = "#a84a08"
GAIN = "#1d7a3e"
LOSS = "#b42318"


# ---------------------------------------------------------------- resources

def _resource_dir():
    """Directory holding lapsecoin.png and lapsecoin.svg, whichever way the
    program is packaged."""
    import resources
    candidates = []
    if resources.is_frozen():
        candidates.append(resources.bundle_dir())
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(exe_dir)
        candidates.append(os.path.join(exe_dir, "_internal"))
    else:
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for c in candidates:
        if os.path.exists(os.path.join(c, "lapsecoin.svg")):
            return c
    return candidates[0] if candidates else "."


ICON_PNG = os.path.join(_resource_dir(), "lapsecoin.png")
ICON_SVG = os.path.join(_resource_dir(), "lapsecoin.svg")


def ensure_png_exists():
    """Make lapsecoin.png from the SVG when a source checkout lacks it."""
    if os.path.exists(ICON_PNG) or not os.path.exists(ICON_SVG):
        return
    try:
        import cairosvg
        cairosvg.svg2png(url=ICON_SVG, write_to=ICON_PNG, output_width=512, output_height=512)
        log.info("[gui] generated missing lapsecoin.png from svg")
    except Exception as e:
        log.debug("[gui] runtime png generation skipped: %s", e)


def _load_logo(px):
    """The logo at px by px, or None. Uses the PNG made at build time, via
    Pillow alone. Rasterising the SVG at run time needs libcairo, a native
    library the build does not bundle, so it silently fails on any Windows
    machine without Cairo installed."""
    ensure_png_exists()
    try:
        from PIL import Image, ImageTk
        img = Image.open(ICON_PNG).convert("RGBA").resize((px, px), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as e:
        log.debug("[gui] could not load the logo: %s", e)
        return None


def apply_window_icon(root):
    """The window and taskbar icon. Failure only means the OS default shows."""
    ensure_png_exists()
    try:
        from PIL import Image, ImageTk
        photo = ImageTk.PhotoImage(Image.open(ICON_PNG))
        root.iconphoto(True, photo)
        root._icon_photo = photo  # tkinter drops PhotoImages that are garbage collected
    except Exception as e:
        log.debug("[gui] could not set window icon: %s", e)


def install_exception_logging(root):
    """Send Tk's callback exceptions to the log. A windowed build has no
    stderr, so Tk's own report would fail and the error would vanish."""
    def _report(exc, val, tb):
        import traceback
        log.error("[gui] unhandled exception in Tk callback:\n%s",
                  "".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = _report


# ------------------------------------------------------------------- sizing

def make_dpi_aware():
    """Windows draws a program that has not said it understands high-DPI
    screens as a blurry, stretched bitmap. Called before the first Tk()."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def scale(root):
    """How much bigger than a 96 dpi screen this one is."""
    try:
        return max(root.winfo_fpixels("1i") / 96.0, 1.0)
    except Exception:
        return 1.0


def px(root, n):
    """n pixels at 96 dpi, as pixels on this screen."""
    return int(round(n * scale(root)))


def center(root):
    """Put the window a little above the middle of the screen."""
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def bring_to_front(root):
    """Show the window on top of the others. Windows keeps a window opened by
    a background process, such as the app started again by the update helper,
    behind everything and only flashes its taskbar button, so a prompt could
    sit unseen. Marking it topmost shows it without needing focus; the mark is
    dropped a moment later so it does not stay above other programs."""
    try:
        root.update_idletasks()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(500, lambda: _drop_topmost(root))
    except tk.TclError:
        pass


def _drop_topmost(root):
    try:
        root.attributes("-topmost", False)
    except tk.TclError:
        pass


# ------------------------------------------------------------------- styles

def fonts(root):
    """The program's type scale, built from the system's own UI font."""
    if hasattr(root, "_fonts"):
        return root._fonts
    base = tkfont.nametofont("TkDefaultFont").actual("family")
    mono = tkfont.nametofont("TkFixedFont").actual("family")
    root._fonts = {
        "h1": tkfont.Font(root=root, family=base, size=20, weight="bold"),
        "h2": tkfont.Font(root=root, family=base, size=15, weight="bold"),
        "body": tkfont.Font(root=root, family=base, size=12),
        "bold": tkfont.Font(root=root, family=base, size=12, weight="bold"),
        "small": tkfont.Font(root=root, family=base, size=10),
        "figure": tkfont.Font(root=root, family=base, size=24, weight="bold"),
        "mono": tkfont.Font(root=root, family=mono, size=11),
    }
    return root._fonts


def install(root, title, minsize=None):
    """Make root look like the rest of the program. Returns the font set."""
    install_exception_logging(root)
    root.title(title)
    apply_window_icon(root)
    f = fonts(root)
    root.configure(bg=PAPER)
    if minsize:
        root.minsize(px(root, minsize[0]), px(root, minsize[1]))

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=PAPER, foreground=INK, font=f["body"],
                    bordercolor=LINE, focuscolor=ACCENT)
    style.configure("TFrame", background=PAPER)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("TLabel", background=PAPER, foreground=INK, font=f["body"])
    style.configure("Card.TLabel", background=SURFACE, foreground=INK, font=f["body"])
    style.configure("H1.TLabel", font=f["h1"])
    style.configure("H2.TLabel", font=f["h2"])
    style.configure("Dim.TLabel", foreground=INK_DIM, font=f["small"])
    style.configure("Muted.TLabel", foreground=INK_DIM, font=f["body"])
    style.configure("CardDim.TLabel", background=SURFACE, foreground=INK_DIM, font=f["small"])
    style.configure("Figure.TLabel", background=SURFACE, foreground=INK, font=f["figure"])
    style.configure("Error.TLabel", foreground=LOSS, font=f["body"])
    style.configure("Live.TLabel", foreground=GAIN, font=f["small"])

    pad = (px(root, 18), px(root, 9))
    style.configure("Primary.TButton", background=ACCENT, foreground="white", font=f["bold"],
                    padding=pad, borderwidth=0, relief="flat", focuscolor=ACCENT)
    style.map("Primary.TButton", background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK)])
    style.configure("Secondary.TButton", background=SURFACE, foreground=INK, font=f["body"],
                    padding=pad, borderwidth=1, relief="flat", bordercolor=LINE_STRONG,
                    lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=SURFACE)
    style.map("Secondary.TButton", background=[("active", HOVER), ("pressed", HOVER)])

    style.configure("App.TEntry", fieldbackground=SURFACE, foreground=INK, insertcolor=INK,
                    bordercolor=LINE_STRONG, lightcolor=SURFACE, darkcolor=SURFACE,
                    padding=px(root, 8), font=f["body"])
    style.map("App.TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    return f


# ---------------------------------------------------------------- building

def card(parent, **kw):
    """A white panel with a hairline border."""
    return tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                    highlightbackground=LINE, highlightcolor=LINE, bd=0, **kw)


def header(parent, title, subtitle=None):
    """The mark, the name and an optional muted line, across the top."""
    root = parent.winfo_toplevel()
    row = ttk.Frame(parent)
    logo = _load_logo(px(root, 44))
    if logo is not None:
        mark = ttk.Label(row, image=logo)
        mark.image = logo  # keep a reference
        mark.pack(side="left", padx=(0, px(root, 14)))
    text = ttk.Frame(row)
    text.pack(side="left")
    ttk.Label(text, text=title, style="H1.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(text, text=subtitle, style="Muted.TLabel").pack(anchor="w")
    return row


def listbox(parent, root, height):
    """A plain list in the program's colours."""
    f = fonts(root)
    return tk.Listbox(parent, height=height, exportselection=False, activestyle="none",
                      font=f["body"], bg=SURFACE, fg=INK, bd=0, relief="flat",
                      highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                      selectbackground=ACCENT, selectforeground="white")
