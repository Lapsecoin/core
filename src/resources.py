"""Where a build finds the files shipped with it, and whether it is a build.

Written to hold for a source checkout, a PyInstaller bundle and a Nuitka
standalone build, so the rest of the code asks here instead of looking at
sys._MEIPASS or sys.frozen itself.
"""

import os
import sys


def is_frozen():
    # Nuitka marks every module it compiles with __compiled__; PyInstaller
    # sets sys.frozen.
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def bundle_dir():
    """The directory that holds VERSION, templates_html/, vendor/ and the
    other shipped files. In a source checkout that is the repository root."""
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        return mei
    here = os.path.dirname(os.path.abspath(__file__))
    if "__compiled__" in globals():
        # Nuitka puts the data files at the top of the distribution folder,
        # where its compiled modules report themselves to be.
        return here
    return os.path.dirname(here)


def prepare_oqs():
    """Load the liboqs shipped beside a frozen build before oqs is imported,
    so it is found whatever the search path. PyInstaller does the same in
    hook_oqs.py; a Nuitka build has no such hook, so main.py calls this."""
    import ctypes
    import glob
    base = bundle_dir()
    patterns = ["oqs.dll", "liboqs.dll"] if sys.platform == "win32" else ["liboqs.so*"]
    for pat in patterns:
        for lib in sorted(glob.glob(os.path.join(base, pat))):
            if not os.path.isfile(lib):
                continue
            try:
                ctypes.CDLL(lib)
            except OSError:
                continue
            os.environ["OQS_INSTALL_PATH"] = base
            return True
    return False
