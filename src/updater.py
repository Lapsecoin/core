"""Self-update: detects what kind of install this is and, only for the
install types where doing so is actually safe, performs the update in
place. Every other install type keeps doing exactly what it did before
this module existed: nothing, the UI just links out to the releases page.

Install types this recognizes:
  "windows-msi"    the MSI installer build on Windows: a folder of files
                   installed per user, with its data under %LOCALAPPDATA%.
  "windows-store"  the same MSI, installed by the Microsoft Store. It does not
                   update itself: the Store does, so the UI sends the user
                   there (STORE_URL, baked in at build time).
  "windows-msix"   the Microsoft Store build, an MSIX package. It does not
                   update itself: the Store does, so the UI sends the user
                   there (STORE_URL). Its data is in %USERPROFILE%\\LapseCoin.
  "linux-appimage" frozen PyInstaller build running inside an AppImage
                   (identified by the $APPIMAGE env var the AppImage
                   runtime itself sets, not guessed at).
  "pip"            installed via `pip install lapsecoin` (not frozen, and
                   importlib.metadata can find the "lapsecoin" distribution).
  "docker"         LAPSECOIN_DOCKER=1 in the environment. Self-declared
                   (set by this project's own Dockerfile) rather than
                   detected: nothing in a container's filesystem reliably
                   says "I am a container", and a false negative here
                   would try to self-update a Docker image from the
                   inside, which is never right.
  "source"         anything else: a git checkout run with `python main.py`,
                   an editable install, a frozen build that isn't an
                   AppImage and isn't on Windows (e.g. a bare Linux onedir
                   build run directly), or anything ambiguous. Never
                   guessed into one of the self-updatable types.

Only "windows-msi", "linux-appimage" and "pip" are self-updatable. This
mirrors scripts/install.sh and scripts/install.ps1's own reasoning: the
prebuilt binaries always have libtorrent and liboqs bundled at CI build
time, so libtorrent availability never affects *whether* a self-update
can happen, only what a pip upgrade needs to double-check afterward.
"""

import contextlib
import logging
import os
import subprocess
import sys
import tempfile
import threading

log = logging.getLogger("ec.updater")

RELEASE_ASSET_LINUX   = "lapsecoin"
RELEASE_ASSET_MSI     = "lapsecoin.msi"
RELEASE_ASSET_SUMS    = "SHA256SUMS"
DOWNLOAD_BASE = "https://github.com/Lapsecoin/core/releases/download"

def _load_store_url():
    """The Store page of this app, written into the program folder at build
    time from the MSIX_STORE_ID repo variable (scripts/build_windows.ps1). It
    is empty when the variable was not set. A Store-installed copy only exists
    once the listing is live, so no check is needed here."""
    import resources
    try:
        with open(os.path.join(resources.bundle_dir(), "STORE_URL")) as f:
            return f.read().strip()
    except OSError:
        return ""


STORE_URL = _load_store_url()

STORE_TYPES = {"windows-store", "windows-msix"}

SELF_UPDATABLE = {"windows-msi", "linux-appimage", "pip"}

# Stages, in the order a successful attempt moves through them. "restarting"
# is the last one any caller ever observes: every self-update path either
# execs a new process image or hands off to a helper and exits, so nothing
# after that point runs in this process to report a later stage.
_TERMINAL_STAGES = {"unsupported", "failed"}


def detect_install_type():
    if os.environ.get("LAPSECOIN_DOCKER"):
        return "docker"
    import resources
    if resources.is_frozen():
        if os.environ.get("APPIMAGE"):
            return "linux-appimage"
        if sys.platform.startswith("win"):
            import datadir
            if datadir.is_packaged():
                return "windows-msix"
            if datadir.is_store_install():
                return "windows-store"
            return "windows-msi" if datadir.is_msi_install() else "source"
        return "source"
    try:
        import importlib.metadata as _md
        _md.distribution("lapsecoin")
        return "pip"
    except Exception:
        return "source"


def _installed_exe():
    """The program to start again after an MSI update. Not sys.executable: in a
    Nuitka build that names a python.exe which does not exist, and the real
    program is lapsecoin.exe in the same folder. Checked now, before the app
    is closed, so a wrong path stops the update with a message instead of
    leaving the user with no app running and an error dialog."""
    import datadir
    exe = os.path.join(datadir.install_dir(), "lapsecoin.exe")
    if not os.path.isfile(exe):
        raise RuntimeError(f"cannot find the installed program at {exe}, "
                           "so it could not be started again after the update")
    return exe


def restart_check(report_path):
    """For CI: does this running build restart the right program after an
    update? Writes what it found to report_path (a windowed build has no
    console) and returns an exit code, 0 when _installed_exe() is the very
    file that is running. Starts nothing."""
    lines = [f"sys.executable: {sys.executable}",
             f"sys.argv[0]:    {sys.argv[0]}"]
    code = 1
    try:
        exe = _installed_exe()
        lines.append(f"restart target: {exe}")
        running = os.path.abspath(sys.argv[0])
        if os.path.samefile(exe, running):
            code = 0
        else:
            lines.append(f"but the running program is {running}")
    except Exception as e:
        lines.append(f"failed: {e}")
    lines.append("ok" if code == 0 else "WRONG")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return code


def _download(url, dest_path, on_progress=None):
    import requests
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if on_progress:
                    if total:
                        on_progress(f"{done * 100 // total}%")
                    else:
                        on_progress(f"{done // 1024} KiB")


def _verify_appimage(path):
    """Just enough of a sanity check to catch a truncated download or an
    HTML error page saved by mistake, not a substitute for HTTPS (which
    requests already gives us) or a signature. AppImages are ELF binaries;
    a real one is comfortably larger than this floor."""
    size = os.path.getsize(path)
    if size < 1_000_000:
        raise RuntimeError(f"downloaded AppImage is only {size} bytes; refusing to install it")
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic != b"\x7fELF":
        raise RuntimeError("downloaded file doesn't look like an ELF binary; refusing to install it")


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # every .msi starts with this


def _verify_msi(path):
    size = os.path.getsize(path)
    if size < 1_000_000:
        raise RuntimeError(f"downloaded installer is only {size} bytes; refusing to run it")
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic != _OLE_MAGIC:
        raise RuntimeError("downloaded file doesn't look like a Windows installer; refusing to run it")


def _expected_sha256(sums_text, name):
    """The hash listed for `name` in a SHA256SUMS file, or None."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def _verify_sha256(path, expected):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise RuntimeError("downloaded installer does not match the published checksum; refusing to run it")


class UpdateSession:
    """One update attempt. A fresh instance per attempt; a retry after a
    failure just gets a new one, there's nothing about a half-finished
    attempt worth keeping around.

    .stage / .detail / .error are read by both the Flask status endpoint
    and (via on_progress) the console --update flow. Only this session's
    own thread ever writes them.
    """

    def __init__(self, install_type, target_version, on_progress=None):
        self.install_type    = install_type
        self.target_version  = target_version
        self.stage  = "idle"
        self.detail = ""
        self.error  = None
        self._on_progress = on_progress
        self._lock = threading.Lock()

    def _set(self, stage, detail=""):
        with self._lock:
            self.stage, self.detail = stage, detail
        log.info("[updater] %s: %s", stage, detail)
        if self._on_progress:
            with contextlib.suppress(Exception):
                self._on_progress(stage, detail)

    def snapshot(self):
        with self._lock:
            return {"stage": self.stage, "detail": self.detail, "error": self.error}

    def run(self):
        try:
            if self.install_type == "pip":
                self._run_pip()
            elif self.install_type == "linux-appimage":
                self._run_appimage()
            elif self.install_type == "windows-msi":
                self._run_windows_msi()
            else:
                self._set("unsupported",
                          f"Can't auto-update a {self.install_type!r} install.")
        except Exception as e:
            log.exception("[updater] update failed")
            self.error = str(e)
            self._set("failed", str(e))

    # ---- pip ---------------------------------------------------------------

    def _run_pip(self):
        self._set("downloading", "pip install --upgrade lapsecoin")
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             "--no-cache-dir", "lapsecoin"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        tail = []
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            tail = tail[-20:]
            self._set("downloading", line)
        code = proc.wait()
        if code != 0:
            raise RuntimeError("pip install --upgrade failed:\n" + "\n".join(tail))

        self._set("restarting", "relaunching")
        self._relaunch_in_place()

    def _relaunch_in_place(self):
        # Replaces this process image rather than spawning a second copy,
        # which would otherwise race the still-running original for the
        # same ports and the same chain database file.
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ---- linux appimage ------------------------------------------------------

    def _run_appimage(self):
        appimage_path = os.environ.get("APPIMAGE")
        if not appimage_path:
            raise RuntimeError("$APPIMAGE isn't set; not actually running as an AppImage")
        appimage_path = os.path.realpath(appimage_path)
        install_dir = os.path.dirname(appimage_path)
        if not os.access(install_dir, os.W_OK):
            raise RuntimeError(
                f"{install_dir} isn't writable by this process; "
                "move the AppImage somewhere you own, or update it by hand")

        url = f"{DOWNLOAD_BASE}/v{self.target_version}/{RELEASE_ASSET_LINUX}"
        self._set("downloading", url)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=install_dir, prefix=".lapsecoin-update-")
        os.close(tmp_fd)
        try:
            _download(url, tmp_path, on_progress=lambda d: self._set("downloading", d))

            self._set("verifying", "checking the download looks like a real AppImage")
            _verify_appimage(tmp_path)

            os.chmod(tmp_path, 0o755)
            self._set("installing", f"replacing {appimage_path}")
            os.replace(tmp_path, appimage_path)  # atomic: same filesystem, same dir
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise

        self._set("restarting", "relaunching the updated AppImage")
        os.execv(appimage_path, [appimage_path] + sys.argv[1:])

    # ---- windows msi -------------------------------------------------------

    def _run_windows_msi(self):
        """Download the new installer, check it, then hand off to a helper
        that closes this process, runs the installer quietly (a per-user
        install, so no elevation) and starts the app again. The installer
        replaces the program files; the data folder is not touched."""
        import datadir
        exe_path = _installed_exe()
        work = os.path.join(datadir.user_data_dir(), "update")
        os.makedirs(work, exist_ok=True)
        msi_path = os.path.join(work, f"lapsecoin-{self.target_version}.msi")

        base = f"{DOWNLOAD_BASE}/v{self.target_version}"
        self._set("downloading", f"{base}/{RELEASE_ASSET_MSI}")
        try:
            _download(f"{base}/{RELEASE_ASSET_MSI}", msi_path,
                      on_progress=lambda d: self._set("downloading", d))
            self._set("verifying", "checking the installer")
            _verify_msi(msi_path)
            expected = None
            try:
                import requests
                r = requests.get(f"{base}/{RELEASE_ASSET_SUMS}", timeout=30)
                if r.ok:
                    expected = _expected_sha256(r.text, RELEASE_ASSET_MSI)
            except Exception:
                log.debug("[updater] could not fetch the checksums", exc_info=True)
            if expected:
                _verify_sha256(msi_path, expected)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(msi_path)
            raise

        self._set("installing", "handing off to the update helper")
        pid = os.getpid()
        helper_path = os.path.join(tempfile.gettempdir(), f"lapsecoin-update-{pid}.bat")
        msi_log = os.path.join(work, "installer.log")
        launch_args = subprocess.list2cmdline(sys.argv[1:])
        helper_script = (
            "@echo off\r\n"
            f'taskkill /PID {pid} /F >nul 2>&1\r\n'
            "timeout /T 2 /NOBREAK >nul\r\n"
            f'msiexec /i "{msi_path}" /qn /norestart LAUNCH=0 /l*v "{msi_log}"\r\n'
            f'del "{msi_path}" >nul 2>&1\r\n'
            f'start "" "{exe_path}"{(" " + launch_args) if launch_args else ""}\r\n'
            '(goto) 2>nul & del "%~f0"\r\n'
        )
        with open(helper_path, "w", encoding="ascii") as f:
            f.write(helper_script)

        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                        getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.upper().startswith(("_MEI", "_PYI", "PYINSTALLER", "TCL", "TK"))}
        subprocess.Popen(["cmd.exe", "/D", "/C", helper_path],
                         creationflags=creationflags, close_fds=True,
                         env=clean_env, cwd=work)
        self._set("restarting", "exiting so the installer can finish")
        os._exit(0)


class Updater:
    """Owns the install-type detection (computed once, it never changes for
    a running process) and the currently-running UpdateSession, if any."""

    def __init__(self, install_type=None):
        self.install_type = install_type if install_type is not None else detect_install_type()
        self._session = None
        self._lock = threading.Lock()

    def can_self_update(self):
        return self.install_type in SELF_UPDATABLE

    def update_url(self, releases_url):
        """Where a user who cannot be updated in place gets the new version."""
        return (STORE_URL or releases_url) if self.install_type in STORE_TYPES else releases_url

    def status(self):
        with self._lock:
            session = self._session
        if session is None:
            return {"stage": "idle", "detail": "", "error": None,
                    "install_type": self.install_type,
                    "can_self_update": self.can_self_update()}
        snap = session.snapshot()
        snap["install_type"] = self.install_type
        snap["can_self_update"] = self.can_self_update()
        return snap

    def start(self, target_version, on_progress=None):
        """Returns True if a new attempt was started, False if one was
        already running.

        A session is only ever swapped in here or in run_sync(), always
        while holding _lock, and this check happens under that same lock
        as the swap -- so two calls racing each other can't both pass the
        guard before either session's thread has run. (Treating a fresh
        session's transient "idle" stage as already-overridable would
        reopen exactly that race, since a session sits in "idle" for a
        moment before its thread's first _set() call; not done here.)"""
        with self._lock:
            if self._session is not None and self._session.stage not in _TERMINAL_STAGES:
                return False
            session = UpdateSession(self.install_type, target_version, on_progress=on_progress)
            self._session = session
        threading.Thread(target=session.run, daemon=True, name="update-run").start()
        return True

    def run_sync(self, target_version, on_progress=None):
        """Runs an attempt on the calling thread instead of a background
        one. Used by the console --update flow, which has nothing else to
        do while it waits and wants the same exceptions/exit behavior
        (execv/os._exit on success) to happen in *this* process."""
        with self._lock:
            self._session = UpdateSession(self.install_type, target_version, on_progress=on_progress)
            session = self._session
        session.run()
        return session