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
    """Point oqs at the liboqs shipped with a frozen build, before it is
    imported. PyInstaller does this in scripts/hook_oqs.py; a Nuitka build has no such
    hook, so main.py calls this. oqs expects an install prefix with the
    library under lib/ (bin/ on Windows), which is how a Nuitka build lays
    it out in _oqs/ beside the executable."""
    prefix = os.path.join(bundle_dir(), "_oqs")
    if os.path.isdir(prefix):
        os.environ["OQS_INSTALL_PATH"] = prefix
        return True
    return False
