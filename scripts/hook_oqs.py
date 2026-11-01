"""
PyInstaller runtime hook for oqs (liboqs-python).

Pre-loads liboqs via ctypes before oqs imports so it gets the cached
handle regardless of search path. Works on both Linux and Windows.
"""
import ctypes
import glob
import os
import sys

if hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS

    if sys.platform == "win32":
        patterns = ["oqs.dll", "liboqs.dll"]
    else:
        patterns = ["liboqs.so*"]

    candidates = []
    for pat in patterns:
        candidates += sorted(glob.glob(os.path.join(meipass, pat)))

    errors = []
    for lib in candidates:
        if os.path.isfile(lib):
            try:
                ctypes.CDLL(lib)
                os.environ["OQS_INSTALL_PATH"] = meipass
                os.environ.pop("LAPSECOIN_OQS_LOAD_ERROR", None)
                break
            except OSError as e:
                errors.append(str(e))
    else:
        # Kept, not dropped: oqs falls back to building liboqs from source when
        # it finds none, which needs cmake and a compiler a user will not have.
        # main.py reads this and stops with the real reason instead.
        os.environ["LAPSECOIN_OQS_LOAD_ERROR"] = (
            "; ".join(errors) if errors else "no liboqs library is in this build")
