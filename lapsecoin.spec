# -*- mode: python ; coding: utf-8 -*-
#
# Linux:   make linux   -> dist/lapsecoin            (AppImage, no extension)
# Windows: make windows -> dist/lapsecoin.exe       (onefile)

import glob, os, sys
from PyInstaller.utils.hooks import collect_all

nacl_datas,    nacl_binaries,    nacl_hiddenimports    = collect_all("nacl")
cffi_datas,    cffi_binaries,    cffi_hiddenimports    = collect_all("cffi")
oqs_datas,     oqs_binaries,     oqs_hiddenimports     = collect_all("oqs")
chiavdf_datas, chiavdf_binaries, chiavdf_hiddenimports = collect_all("chiavdf")

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

# Search multiple locations for MSVC runtime DLLs (no-op on Linux).
#
# SysWOW64 is a deliberate last resort, not an equal alternative: on a
# 64-bit Windows box that folder holds the 32-bit copies kept for legacy
# 32-bit apps (System32 has the 64-bit ones despite the name), so a
# 64-bit build must stop searching the moment an earlier root has the
# file, never fall through to it. This used to keep searching every root
# for every pattern regardless, so on a runner where the same filename
# existed in both System32 and SysWOW64, both copies got queued for the
# same destination name and PyInstaller's table of contents kept
# whichever won that collision, not necessarily the 64-bit one. That
# shipped a real Windows build that failed for exactly this reason: a
# 32-bit vcruntime140.dll loaded into a 64-bit process reads as chiavdf's
# DLL failing to load, "%1 is not a valid Win32 application", rather than
# naming the actual mismatched file.
_msvc_dlls = []
if sys.platform == "win32":
    _msvc_search = [
        os.path.dirname(sys.executable),
        os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32"),
        os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "SysWOW64"),
    ]
    for _pat in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                 "concrt140.dll"):
        for _root in _msvc_search:
            _matches = [p for p in glob.glob(os.path.join(_root, _pat)) if os.path.isfile(p)]
            if _matches:
                if (_matches[0], ".") not in _msvc_dlls:
                    _msvc_dlls.append((_matches[0], "."))
                break  # stop at the first root that has it; do not also check the rest

    # chiavdf installs as a single top-level .pyd (not a package directory),
    # so collect_all/collect_dynamic_libs silently skips its native DLLs.
    # Delvewheel places the mpir DLLs directly in site-packages next to the
    # .pyd for top-level extension modules, so we grab them explicitly here.
    import site
    for _sp in site.getsitepackages():
        for _pat in ("mpir*.dll", "chiavdf*.dll"):
            for _p in glob.glob(os.path.join(_sp, _pat)):
                if os.path.isfile(_p) and (_p, ".") not in _msvc_dlls:
                    _msvc_dlls.append((_p, "."))

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
    *nacl_datas, *cffi_datas, *oqs_datas, *chiavdf_datas,
]
_all_hiddenimports = [
    *nacl_hiddenimports, *cffi_hiddenimports,
    *oqs_hiddenimports, *chiavdf_hiddenimports,
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

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="lapsecoin",
        debug=False,
        strip=False,
        # UPX-packed executables are a common heuristic trigger for AV
        # engines (the same compression malware droppers use to evade
        # static signatures), which was flagging this onefile build before
        # users could even run it. Uncompressed and larger, but otherwise
        # identical: still a single lapsecoin.exe, same distribution and
        # update flow.
        upx=False,
        upx_exclude=[],
        console=False,
        icon="favicon.ico",
        bootloader_ignore_signals=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
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
