# -*- mode: python ; coding: utf-8 -*-
#
# The stand-in for the old single-file lapsecoin.exe. Old copies update by
# downloading lapsecoin.exe from the latest release; this is what they get,
# and it moves them onto the MSI. See lapsecoin_bridge.py.
#
#   pyinstaller --clean --noconfirm --distpath dist-bridge lapsecoin-bridge.spec
#   -> dist-bridge/lapsecoin.exe
#
# Standard library only, so it is small and has nothing heavy to flag.

import os

VERSION_INFO = "version_info.txt" if os.path.exists("version_info.txt") else None

a = Analysis(
    ["lapsecoin_bridge.py"],
    pathex=[".", "src"],
    binaries=[],
    datas=[("VERSION", ".")],
    hiddenimports=["datadir"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "tkinter", "numpy", "PIL", "flask", "requests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lapsecoin",
    debug=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    icon="favicon.ico",
    version=VERSION_INFO,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
