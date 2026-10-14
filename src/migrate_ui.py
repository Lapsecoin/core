"""The question the installer build asks on its first run: were you using
LapseCoin before, and if so, where? Answering copies the key and data across.

Written so that it never blocks a node that cannot show a window: with no
display, no Tk, or --no-gui, it takes the most recent folder it found, and
starts fresh when it found none.
"""

import os
import sys
import time

USE, FRESH, CANCEL = "use", "fresh", "cancel"


def _fallback(suggestions):
    return (USE, suggestions[0]) if suggestions else (FRESH, None)


def _has_key(folder, key_name):
    return os.path.isfile(os.path.join(folder, key_name))


def ask(suggestions, key_name="lapsecoin_key.json"):
    """Returns (USE, folder), (FRESH, None) or (CANCEL, None)."""
    if any(a in sys.argv for a in ("--no-gui", "--export", "--update")):
        return _fallback(suggestions)
    try:
        import tkinter  # noqa: F401
    except Exception:
        return _fallback(suggestions)
    try:
        return _show(suggestions, key_name)
    except Exception:
        # No display, or Tk failed to start.
        return _fallback(suggestions)


def _label(folder):
    try:
        when = time.strftime("%d %b %Y", time.localtime(os.path.getmtime(folder)))
    except OSError:
        when = "unknown date"
    return f"{folder}   ({when})"


def _show(suggestions, key_name):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import gui_theme as theme

    result = {"v": (CANCEL, None)}
    theme.make_dpi_aware()
    root = tk.Tk()
    root.resizable(False, False)
    theme.install(root, "LapseCoin")

    frame = ttk.Frame(root, padding=theme.px(root, 28))
    frame.pack(fill="both", expand=True)
    theme.header(frame, "LapseCoin").pack(anchor="w")
    ttk.Label(frame, text="Restore your keys", style="H2.TLabel").pack(
        anchor="w", pady=(theme.px(root, 22), 2))
    ttk.Label(frame, style="Muted.TLabel", wraplength=theme.px(root, 460), text=(
        "Choose the folder you used before:" if suggestions
        else "No old folder found. Browse to the folder you used before, "
             "or start new.")).pack(anchor="w")

    box = None
    if suggestions:
        box = theme.listbox(frame, root, min(max(len(suggestions), 2), 5))
        for folder in suggestions:
            box.insert("end", _label(folder))
        box.selection_set(0)
        box.pack(fill="x", pady=(theme.px(root, 12), 0), ipady=theme.px(root, 4))

    def finish(kind, folder=None):
        result["v"] = (kind, folder)
        root.destroy()

    def use_folder(folder):
        if not _has_key(folder, key_name):
            messagebox.showwarning(
                "LapseCoin", "No LapseCoin key in that folder.\nChoose the folder "
                "that holds lapsecoin_key.json.", parent=root)
            return
        finish(USE, folder)

    def use_selected():
        if box is not None and box.curselection():
            use_folder(suggestions[box.curselection()[0]])

    def browse():
        folder = filedialog.askdirectory(parent=root, title="Choose the folder you used before")
        if folder:
            use_folder(os.path.normpath(folder))

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(theme.px(root, 22), 0))
    if suggestions:
        ttk.Button(row, text="Restore", style="Primary.TButton", command=use_selected
                   ).pack(side="left")
    ttk.Button(row, text="Browse...", style="Secondary.TButton", command=browse
               ).pack(side="left", padx=(8, 0))
    ttk.Button(row, text="Start new", style="Secondary.TButton", command=lambda: finish(FRESH)
               ).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", lambda: finish(CANCEL))
    theme.center(root)
    theme.bring_to_front(root)
    root.mainloop()
    return result["v"]
