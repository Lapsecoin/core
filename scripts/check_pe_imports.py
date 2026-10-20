"""Lists the DLLs that a program folder imports and does not carry itself.

A Windows machine that has Visual Studio (a CI runner, a developer's PC)
already has the Visual C++ runtime in System32, so a native library that needs
a runtime DLL the folder does not ship works there and fails on a clean
machine, which is what the Store's testers use. Any import of the Visual C++
runtime family that is not in the folder is reported and fails the check. Other
imports that are not in the folder are listed for information.

    python scripts/check_pe_imports.py <folder>
"""

import os
import re
import sys

import pefile

# Not part of Windows itself: they come with a redistributable or an app.
NOT_IN_WINDOWS = re.compile(
    r"^(vcruntime|msvcp|concrt|vcomp|vccorlib|mfc|atl|ucrtbase_clr|python3|libssl|libcrypto|tcl|tk)", re.I)
# Always present in Windows 10 and later.
WINDOWS = re.compile(r"^(api-ms-win-|ext-ms-win-)", re.I)


def imports(path):
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    except pefile.PEFormatError:
        return set()
    names = set()
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in getattr(pe, attr, []):
            names.add(entry.dll.decode("ascii", "replace").lower())
    pe.close()
    return names


def main(folder):
    present, files = set(), []
    for root, _, names in os.walk(folder):
        for n in names:
            present.add(n.lower())
            if n.lower().endswith((".dll", ".pyd", ".exe")):
                files.append(os.path.join(root, n))
    missing = {}
    for f in files:
        for dll in imports(f):
            if dll in present or WINDOWS.match(dll):
                continue
            missing.setdefault(dll, []).append(os.path.relpath(f, folder))
    bad = {d: u for d, u in missing.items() if NOT_IN_WINDOWS.match(d)}
    other = {d: u for d, u in missing.items() if d not in bad}
    print(f"{len(files)} binaries scanned in {folder}")
    for d, u in sorted(other.items()):
        print(f"  not in the folder (assumed part of Windows): {d}  <- {', '.join(sorted(u)[:3])}")
    for d, u in sorted(bad.items()):
        print(f"MISSING RUNTIME DLL: {d}  <- {', '.join(sorted(u)[:5])}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
