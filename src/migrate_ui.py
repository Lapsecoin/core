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
    if "--no-gui" in sys.argv:
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
    return f"{folder}    (last used {when})"


def _show(suggestions, key_name):
    import tkinter as tk
    from tkinter import filedialog, messagebox

    result = {"v": (CANCEL, None)}
    root = tk.Tk()
    root.title("LapseCoin")
    root.resizable(False, False)

    tk.Label(root, justify="left", wraplength=520, padx=16, pady=12, text=(
        "Welcome to the new LapseCoin installer.\n\n"
        "If you were running LapseCoin before as lapsecoin.exe, choose the "
        "folder it ran from, and your key and data will be copied here. "
        "Nothing is moved or deleted.")).pack(anchor="w")

    box = None
    if suggestions:
        tk.Label(root, text="Found on this computer:", padx=16).pack(anchor="w")
        box = tk.Listbox(root, width=78, height=min(max(len(suggestions), 2), 6),
                         exportselection=False)
        for folder in suggestions:
            box.insert("end", _label(folder))
        box.selection_set(0)
        box.pack(padx=16, pady=(2, 8))
    else:
        tk.Label(root, padx=16, justify="left",
                 text="No earlier LapseCoin data was found in Desktop or Downloads."
                 ).pack(anchor="w", pady=(0, 8))

    def finish(kind, folder=None):
        result["v"] = (kind, folder)
        root.destroy()

    def use_folder(folder):
        if not _has_key(folder, key_name):
            messagebox.showwarning(
                "LapseCoin", f"There is no {key_name} in that folder, so it "
                "is not a LapseCoin folder.", parent=root)
            return
        finish(USE, folder)

    def use_selected():
        if box is not None and box.curselection():
            use_folder(suggestions[box.curselection()[0]])

    def browse():
        folder = filedialog.askdirectory(parent=root, title="Where did lapsecoin.exe run from?")
        if folder:
            use_folder(os.path.normpath(folder))

    row = tk.Frame(root, padx=16, pady=12)
    row.pack(fill="x")
    if suggestions:
        tk.Button(row, text="Use the selected folder", command=use_selected).pack(side="left")
    tk.Button(row, text="Choose another folder...", command=browse).pack(side="left", padx=8)
    tk.Button(row, text="I am new, start fresh", command=lambda: finish(FRESH)).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", lambda: finish(CANCEL))
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    root.lift()
    root.attributes("-topmost", True)
    root.mainloop()
    return result["v"]
