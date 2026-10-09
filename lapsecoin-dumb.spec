# -*- mode: python ; coding: utf-8 -*-
#
# LapseCoin light client ("dumb"): the wallet and the board through another
# node, with no chain of its own. See src/light.py.
#
# Both platforms: make dumb -> dist/lapsecoin-dumb  (.exe on Windows, onefile)
#
# It needs only liboqs (to sign) and the web stack, so the VDF, the chain
# database, the torrent code, and the desktop GUI are all left out
# of the build, which is most of what makes the full node hard to build and
# large to download. tests/test_light.py checks nothing it imports reaches
# for them.

import glob, os, sys
from PyInstaller.utils.hooks import collect_all

nacl_datas,    nacl_binaries,    nacl_hiddenimports    = collect_all("nacl")
cffi_datas,    cffi_binaries,    cffi_hiddenimports    = collect_all("cffi")
oqs_datas,     oqs_binaries,     oqs_hiddenimports     = collect_all("oqs")
# The Fees page checks an EVM address signature and an address checksum.
coincurve_datas, coincurve_binaries, coincurve_hiddenimports = collect_all("coincurve")

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


_all_binaries = [
    *nacl_binaries, *cffi_binaries, *oqs_binaries, *coincurve_binaries,
    *_liboqs_bins, *_msvc_dlls,
]
_all_datas = [
    ("VERSION", "."),
    ("src/bip39_english.txt", "."),
    ("lapsecoin.svg",       "."),
    ("templates_html",     "templates_html"),
    ("vendor/markdown-toolbar-element.js", "vendor"),
    *nacl_datas, *cffi_datas, *oqs_datas, *coincurve_datas,
]
_all_hiddenimports = [
    *nacl_hiddenimports, *cffi_hiddenimports, *oqs_hiddenimports, *coincurve_hiddenimports,
    "oqs", "_cffi_backend", "coincurve", "Crypto.Hash.keccak",
    "flask", "werkzeug", "jinja2", "jinja2.ext",
    "waitress", "waitress.server", "waitress.task", "waitress.channel",
    # Only used when --proxy names a socks5 proxy (Tor), reached by requests
    # dynamically, which static analysis cannot see.
    "socks",
]

a = Analysis(
    ["lapsecoin_light.py"],
    pathex=[".", "src"],
    binaries=_all_binaries,
    datas=_all_datas,
    hiddenimports=_all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_oqs.py"],
    excludes=["pytest", "unittest", "tkinter", "chiavdf", "peewee", "libtorrent",
              "pystray", "PIL", "cairosvg", "miniupnpc", "markdown",
              "flask_limiter", "argcomplete", "gi"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One file on every platform, run from a terminal: it asks for the key's
# passphrase there, then opens the wallet in the browser.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lapsecoin-dumb",
    debug=False,
    strip=False,
    # Same reason as the full node's spec: UPX-packed executables trip
    # antivirus heuristics.
    upx=False,
    upx_exclude=[],
    console=True,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
