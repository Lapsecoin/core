"""The small program that takes the place of the old single-file lapsecoin.exe.

Copies of the old executable update themselves by downloading lapsecoin.exe
from the latest release and swapping it in. That keeps working: this is what
they now download. On first run it
  1. copies the node's key and data from where the old exe lived into the
     new data folder (a copy: the originals stay),
  2. downloads the installer for the same version, checks it, and runs it
     (per user, no administrator prompt),
  3. starts the installed program with the same arguments.
Afterwards it only starts the installed program, so old shortcuts keep working.

Standard library only, so it stays small.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request

if getattr(sys, "frozen", False):
    sys.path.insert(0, getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))
else:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import datadir  # noqa: E402

DOWNLOAD_BASE = "https://github.com/Lapsecoin/core/releases/download"
MANUAL_URL = "https://github.com/Lapsecoin/core/releases/latest"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def local_version():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def say(text):
    """A message box on Windows, stderr elsewhere. Never raises."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "LapseCoin", 0x40)
        else:
            print(text, file=sys.stderr)
    except Exception:
        pass


def fetch(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


def verify(path, sums_text=None, name="lapsecoin.msi"):
    if os.path.getsize(path) < 1_000_000:
        raise RuntimeError("the installer download is too small")
    with open(path, "rb") as f:
        if f.read(8) != OLE_MAGIC:
            raise RuntimeError("the download is not a Windows installer")
    if sums_text:
        want = None
        for line in sums_text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                want = parts[0].lower()
        if want:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                raise RuntimeError("the installer does not match its published checksum")


def install(version):
    base = f"{DOWNLOAD_BASE}/v{version}"
    work = tempfile.mkdtemp(prefix="lapsecoin-")
    msi = os.path.join(work, "lapsecoin.msi")
    fetch(f"{base}/lapsecoin.msi", msi)
    sums = None
    try:
        sums_path = os.path.join(work, "SHA256SUMS")
        fetch(f"{base}/SHA256SUMS", sums_path)
        with open(sums_path, encoding="utf-8") as f:
            sums = f.read()
    except Exception:
        pass
    verify(msi, sums)
    # /passive shows a progress bar and asks nothing; per-user, so no prompt.
    code = subprocess.call(["msiexec", "/i", msi, "/passive", "/norestart", "LAUNCH=0"])
    if code not in (0, 3010):
        raise RuntimeError(f"the installer exited with code {code}")


def start(exe, args):
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("_MEI", "_PYI", "PYINSTALLER"))}
    subprocess.Popen([exe, *args], creationflags=flags, close_fds=True, env=env,
                     cwd=os.path.dirname(exe))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    exe = datadir.installed_exe()
    if not os.path.exists(exe):
        dst = datadir.user_data_dir()
        # Where the old program sat: a compiled single file runs from a temp
        # folder, so its own location is the first thing on the command line.
        places = []
        for exe_path in (sys.argv[0], sys.executable):
            here = os.path.dirname(os.path.realpath(exe_path))
            if here not in places:
                places.append(here)
        for src in (*places, os.getcwd()):
            datadir.migrate(src, dst)
        try:
            say("LapseCoin is moving to a proper installer. This takes a moment "
                "and your keys and data are kept. Press OK to continue.")
            install(local_version())
        except Exception as e:
            say(f"The installer could not be set up automatically ({e}).\n\n"
                f"Please download the installer from {MANUAL_URL}. "
                "Your keys and data have been kept.")
            return 1
    if not os.path.exists(exe):
        say(f"LapseCoin was not found after installing. Please download it from {MANUAL_URL}.")
        return 1
    start(exe, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
