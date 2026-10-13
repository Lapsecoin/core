# -*- mode: python ; coding: utf-8 -*-
#
# Linux:   make linux   -> dist/lapsecoin            (AppImage, no extension)
# Windows: scripts/build_windows.ps1 (Nuitka); this spec is the Linux build

import glob, os, sys
from PyInstaller.utils.hooks import collect_all

nacl_datas,    nacl_binaries,    nacl_hiddenimports    = collect_all("nacl")
cffi_datas,    cffi_binaries,    cffi_hiddenimports    = collect_all("cffi")
oqs_datas,     oqs_binaries,     oqs_hiddenimports     = collect_all("oqs")
chiavdf_datas, chiavdf_binaries, chiavdf_hiddenimports = collect_all("chiavdf")
crypto_datas, crypto_binaries, crypto_hiddenimports = collect_all("cryptography")

_search_roots = [
    "/usr/local/lib",
    "/usr/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    os.path.join(os.path.dirname(sys.executable), "..", "lib"),
    # Windows: liboqs installed to C:/liboqs/install by CI
    "C:/liboqs/install/bin",
    "C:/liboqs/install/lib",
]
_liboqs_patterns = ["liboqs.so*"] if sys.platform != "win32" else ["oqs.dll", "liboqs.dll"]
_liboqs_bins = []
for _root in _search_roots:
    for _pat in _liboqs_patterns:
        for _p in sorted(glob.glob(os.path.join(_root, _pat))):
            if os.path.isfile(_p):
                _liboqs_bins.append((_p, "."))
    if _liboqs_bins:
        break

_msvc_dlls = []

_all_binaries = [
    *nacl_binaries, *cffi_binaries, *oqs_binaries,
    *chiavdf_binaries, *_liboqs_bins, *_msvc_dlls,
]
_all_datas = [
    ("VERSION", "."),
    ("src/bip39_english.txt", "."),
    ("docs/whitepaper.md", "docs"),
    ("lapsecoin.svg",       "."),
    ("lapsecoin.png",       "."),
    ("favicon.ico",        "."),
    ("templates_html",     "templates_html"),
    ("vendor",             "vendor"),
    *nacl_datas, *cffi_datas, *oqs_datas, *chiavdf_datas, *crypto_datas,
]
_all_hiddenimports = [
    *nacl_hiddenimports, *cffi_hiddenimports,
    *oqs_hiddenimports, *chiavdf_hiddenimports, *crypto_hiddenimports,
    
    "oqs", "_cffi_backend", "libtorrent", "miniupnpc",
    "flask", "werkzeug", "werkzeug.serving", "werkzeug.debug",
    # waitress serves both HTTP ports (see main.py _serve); its
    # concurrency modules are reached dynamically.
    "waitress", "waitress.server", "waitress.task", "waitress.channel",
    "jinja2", "jinja2.ext", "markdown",
    "argcomplete", "argcomplete.completers",
    # pystray picks its backend at import time based on the OS
    # (Gtk/AppIndicator on Linux, win32 on Windows, darwin on macOS).
    # PyInstaller's static analysis can't see that dynamic import, so
    # without listing it explicitly the backend, and sometimes pystray
    # itself, gets silently left out of the binary, and the app just
    # falls back to taskbar-minimize with no visible error.
    "pystray", "pystray._base",
    "pystray._gtk", "gi", "gi.repository.Gtk", "gi.repository.AppIndicator3",
    "pystray._win32",
    "pystray._darwin",
    "PIL._tkinter_finder",
]

a = Analysis(
    ["main.py"],
    pathex=[".", "src"],
    binaries=_all_binaries,
    datas=_all_datas,
    hiddenimports=_all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_oqs.py"],
    excludes=["pytest", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onedir: exclude_binaries=True tells EXE not to bundle libs itself;
# COLLECT then assembles everything into dist/lapsecoin-onedir/.
# Named differently from the final AppImage (dist/lapsecoin, no
# extension) so the two don't collide on disk.
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="lapsecoin",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="lapsecoin-onedir",
)
