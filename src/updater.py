"""Self-update: detects what kind of install this is and, only for the
install types where doing so is actually safe, performs the update in
place. Every other install type keeps doing exactly what it did before
this module existed: nothing, the UI just links out to the releases page.

Install types this recognizes:
  "windows-exe"    frozen PyInstaller onefile build on Windows.
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

Only "windows-exe", "linux-appimage" and "pip" are self-updatable. This
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
RELEASE_ASSET_WINDOWS = "lapsecoin.exe"
DOWNLOAD_BASE = "https://github.com/Lapsecoin/core/releases/download"

# Same file discovery.py's PEER_CACHE_FILE loads on startup, and the same
# URL scripts/install.sh / install.ps1 already fetch it from when pip
# couldn't install libtorrent. Duplicated here rather than imported from
# discovery.py: that module owns the live peer pool and importing it just
# for two string constants would pull in far more than this needs.
PEER_CACHE_FILE     = "lapsecoin_peers.json"
DEFAULT_PEERS_URL   = "https://lapsenode.vicnas.me/api/peers/download"

SELF_UPDATABLE = {"windows-exe", "linux-appimage", "pip"}

# Stages, in the order a successful attempt moves through them. "restarting"
# is the last one any caller ever observes: every self-update path either
# execs a new process image or hands off to a helper and exits, so nothing
# after that point runs in this process to report a later stage.
_TERMINAL_STAGES = {"unsupported", "failed"}


def detect_install_type():
    if os.environ.get("LAPSECOIN_DOCKER"):
        return "docker"
    if getattr(sys, "frozen", False):
        if os.environ.get("APPIMAGE"):
            return "linux-appimage"
        if sys.platform.startswith("win"):
            return "windows-exe"
        return "source"
    try:
        import importlib.metadata as _md
        _md.distribution("lapsecoin")
        return "pip"
    except Exception:
        return "source"


def _has_libtorrent():
    """Checked in a fresh subprocess, never via an in-process import: right
    after a pip upgrade the current interpreter's own sys.modules/import
    caches can't be trusted to reflect what's actually on disk any more."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import libtorrent"],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        log.debug("[updater] libtorrent probe failed", exc_info=True)
        return False


def _refresh_peer_cache(peers_url=None):
    """Best-effort. Only overwrites PEER_CACHE_FILE on a successful fetch;
    a failed fetch leaves whatever was already there alone, so a flaky
    network on update never leaves the node with a worse peer list than
    it started with."""
    url = peers_url or os.environ.get("LAPSECOIN_PEERS_URL", DEFAULT_PEERS_URL)
    try:
        import requests
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.content
        import json
        json.loads(data)  # sanity-check it's real JSON before writing it
    except Exception:
        log.warning("[updater] could not refresh peer cache from %s", url, exc_info=True)
        return False
    try:
        with open(PEER_CACHE_FILE, "wb") as f:
            f.write(data)
        return True
    except OSError:
        log.warning("[updater] could not write %s", PEER_CACHE_FILE, exc_info=True)
        return False


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


def _verify_pe(path):
    size = os.path.getsize(path)
    if size < 1_000_000:
        raise RuntimeError(f"downloaded executable is only {size} bytes; refusing to install it")
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic != b"MZ":
        raise RuntimeError("downloaded file doesn't look like a Windows executable; refusing to install it")


def _cleanup_stray_update_files():
    """Removes leftovers from a windows-exe update whose helper never got
    to run. A successful run always cleans both the download and the helper
    script up itself, so anything still here on startup is from a run that
    didn't finish -- never a file this attempt itself just created, since
    this only ever runs before any update has begun.
    """
    install_dir = os.path.dirname(os.path.realpath(sys.executable))
    with contextlib.suppress(OSError):
        for name in os.listdir(install_dir):
            if name.startswith(".lapsecoin-update-") and name.endswith(".exe"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(install_dir, name))
    tmp_dir = tempfile.gettempdir()
    with contextlib.suppress(OSError):
        for name in os.listdir(tmp_dir):
            if name.startswith("lapsecoin-update-") and name.endswith(".bat"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(tmp_dir, name))


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
            elif self.install_type == "windows-exe":
                self._run_windows_exe()
            else:
                self._set("unsupported",
                          f"Can't auto-update a {self.install_type!r} install.")
        except Exception as e:
            log.exception("[updater] update failed")
            self.error = str(e)
            self._set("failed", str(e))

    # ---- pip ---------------------------------------------------------------

    def _run_pip(self):
        had_libtorrent = _has_libtorrent()

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

        self._set("verifying", "checking libtorrent is still available")
        now_has_libtorrent = _has_libtorrent()
        if had_libtorrent and not now_has_libtorrent:
            self._set("verifying",
                      "libtorrent is no longer importable after the upgrade; "
                      "refreshing the starter peers list so DHT discovery "
                      "isn't relied on until that's fixed")
            _refresh_peer_cache()

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

    # ---- windows exe -----------------------------------------------------

    def _run_windows_exe(self):
        exe_path = os.path.realpath(sys.executable)
        install_dir = os.path.dirname(exe_path)
        if not os.access(install_dir, os.W_OK):
            raise RuntimeError(
                f"{install_dir} isn't writable by this process; "
                "re-run the installer instead")

        url = f"{DOWNLOAD_BASE}/v{self.target_version}/{RELEASE_ASSET_WINDOWS}"
        self._set("downloading", url)
        tmp_path = os.path.join(install_dir, f".lapsecoin-update-{os.getpid()}.exe")
        try:
            _download(url, tmp_path, on_progress=lambda d: self._set("downloading", d))
            self._set("verifying", "checking the download looks like a real executable")
            _verify_pe(tmp_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise

        # Windows refuses to overwrite a running EXE. Follow the portable
        # handoff used by Bitflash: start a detached stock cmd.exe, terminate
        # this process from outside, then move and relaunch. The loop and
        # move-success gate make the handoff tolerate a short PE file-lock
        # tail without ever starting the old executable as if it were new.
        self._set("installing", "handing off to the update helper")
        pid = os.getpid()
        helper_path = os.path.join(tempfile.gettempdir(), f"lapsecoin-update-{pid}.bat")
        launch_args = subprocess.list2cmdline(sys.argv[1:])
        helper_script = (
            "@echo off\r\n"
            f'taskkill /PID {pid} /F >nul 2>&1\r\n'
            "for /L %%N in (1,1,120) do (\r\n"
            f'  move /Y "{tmp_path}" "{exe_path}" >nul 2>&1 && goto moved\r\n'
            "  timeout /T 1 /NOBREAK >nul\r\n"
            ")\r\n"
            "exit /B 1\r\n"
            ":moved\r\n"
            f'start "" "{exe_path}"{(" " + launch_args) if launch_args else ""}\r\n'
            'del "%~f0"\r\n'
        )
        with open(helper_path, "w", encoding="ascii") as f:
            f.write(helper_script)

        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                        getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        subprocess.Popen(
            ["cmd.exe", "/D", "/C", helper_path],
            creationflags=creationflags,
            close_fds=True,
        )
        self._set("restarting", "exiting so the update helper can finish")
        os._exit(0)


class Updater:
    """Owns the install-type detection (computed once, it never changes for
    a running process) and the currently-running UpdateSession, if any."""

    def __init__(self, install_type=None):
        self.install_type = install_type if install_type is not None else detect_install_type()
        self._session = None
        self._lock = threading.Lock()
        if self.install_type == "windows-exe":
            _cleanup_stray_update_files()

    def can_self_update(self):
        return self.install_type in SELF_UPDATABLE

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
