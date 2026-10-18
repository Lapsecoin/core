"""The window the uninstaller shows before it removes the program.

Uninstalling removes the program files only. The wallet and the chain stay in
%USERPROFILE%\\LapseCoin, and installing again picks them up. This tells the
user so, shows where the wallet is, and reminds them that the recovery phrase
is what restores it on another computer.

It never blocks: with no wallet on disk, no display or no Tk, it shows
nothing and the uninstall carries on.
"""

import os

import datadir


def wallet_files(folder):
    return [n for n in datadir.KEY_FILES if os.path.exists(os.path.join(folder, n))]


def text(folder):
    return (
        "Uninstalling removes the program, not your wallet.\n\n"
        f"Your wallet stays in:\n{folder}\n\n"
        "Installing LapseCoin again picks it up. To use it on another "
        "computer, or if you delete that folder, you need your recovery "
        "phrase. Make sure it is written down before you continue.")


def _show(folder):
    import tkinter as tk
    from tkinter import ttk

    import gui_theme as theme

    theme.make_dpi_aware()
    root = tk.Tk()
    root.resizable(False, False)
    theme.install(root, "LapseCoin")

    frame = ttk.Frame(root, padding=theme.px(root, 28))
    frame.pack(fill="both", expand=True)
    theme.header(frame, "LapseCoin").pack(anchor="w")
    ttk.Label(frame, text="Before you uninstall", style="H2.TLabel").pack(
        anchor="w", pady=(theme.px(root, 22), 2))
    ttk.Label(frame, style="Muted.TLabel", wraplength=theme.px(root, 460),
              text=text(folder)).pack(anchor="w")

    def open_folder():
        try:
            os.startfile(folder)
        except (AttributeError, OSError):
            pass

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(theme.px(root, 22), 0))
    ttk.Button(row, text="Continue", style="Primary.TButton", command=root.destroy
               ).pack(side="left")
    ttk.Button(row, text="Open wallet folder", style="Secondary.TButton",
               command=open_folder).pack(side="left", padx=(8, 0))

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    theme.center(root)
    theme.bring_to_front(root)
    root.mainloop()


def run(environ=None, show=None):
    folder = datadir.user_data_dir(environ)
    if not wallet_files(folder):
        return False
    try:
        (show or _show)(folder)
    except Exception:
        return False
    return True
