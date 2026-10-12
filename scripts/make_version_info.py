#!/usr/bin/env python3
"""Write version_info.txt, the Windows version resource PyInstaller embeds.

An .exe with no company, description or version in its properties is one of
the things reputation systems treat as unfamiliar. This fills them in from
the VERSION file so every build says what it is. Used by the Windows build
in .github/workflows/release.yml; the specs skip it when the file is absent,
so a local build without it still works.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(version):
    parts = [int(p) for p in version.split(".")] + [0] * 4
    tup = tuple(parts[:4])
    text = ".".join(str(p) for p in tup)
    rows = [
        ("CompanyName", "LapseCoin"),
        ("FileDescription", "LapseCoin node and wallet"),
        ("FileVersion", text),
        ("LegalCopyright", "MIT License. https://github.com/Lapsecoin/core"),
        ("ProductName", "LapseCoin"),
        ("ProductVersion", text),
    ]
    strings = ",\n        ".join(f"StringStruct({k!r}, {v!r})" for k, v in rows)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tup}, prodvers={tup},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        {strings}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main():
    with open(os.path.join(ROOT, "VERSION")) as f:
        version = f.read().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"VERSION is not x.y.z: {version!r}")
    with open(os.path.join(ROOT, "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(render(version))
    print(f"wrote version_info.txt for {version}")


if __name__ == "__main__":
    main()
