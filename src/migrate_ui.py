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
    from tkinter import filedialog, font as tkfont, messagebox

    result = {"v": (CANCEL, None)}
    root = tk.Tk()
    root.title("LapseCoin")
    root.resizable(False, False)

    head = tkfont.Font(family="Segoe UI", size=20, weight="bold")
    body = tkfont.Font(family="Segoe UI", size=14)
    small = tkfont.Font(family="Segoe UI", size=12)

    tk.Label(root, text="Restore your keys", font=head, padx=24, pady=16).pack(anchor="w")
    tk.Label(root, font=body, padx=24, justify="left", wraplength=620, text=(
        "Choose the folder you used before:" if suggestions
        else "No old folder found. Browse to the folder you used before, "
             "or start new.")).pack(anchor="w")

    box = None
    if suggestions:
        box = tk.Listbox(root, width=60, height=min(max(len(suggestions), 2), 5),
                         exportselection=False, font=small, activestyle="none")
        for folder in suggestions:
            box.insert("end", _label(folder))
        box.selection_set(0)
        box.pack(padx=24, pady=(8, 4), fill="x")

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

    def button(parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, font=body, padx=14, pady=6,
                         default="active" if primary else "normal")

    row = tk.Frame(root, padx=24, pady=16)
    row.pack(fill="x")
    if suggestions:
        button(row, "Restore", use_selected, primary=True).pack(side="left")
    button(row, "Browse...", browse).pack(side="left", padx=10)
    button(row, "Start new", lambda: finish(FRESH)).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", lambda: finish(CANCEL))
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    root.lift()
    root.attributes("-topmost", True)
    root.mainloop()
    return result["v"]
