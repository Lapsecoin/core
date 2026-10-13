"""Where a Windows installer build keeps its data.

Every file the node writes (chain database, key, peer list, log) is named
relative to the working directory. That is right for a program run from a
folder the user owns, and wrong for one installed under a path the program
should not write into. An installed build therefore moves its working
directory to %LOCALAPPDATA%\\LapseCoin before anything else starts, which
keeps the program files replaceable by an upgrade and the data out of
their way.

Only a build installed by the MSI does this. It is recognised by a marker
file the installer places next to the executable, so a source checkout, a
pip install, the AppImage and a portable copy behave exactly as before.

Moving from the old single .exe is a copy, never a move: the originals stay
where they were, and nothing is ever overwritten in the new location.
"""

import logging
import os
import shutil
import sys

log = logging.getLogger("ec.datadir")

APP_DIR_NAME = "LapseCoin"
MSI_MARKER = "installed-by-msi"
MIGRATED_NOTE = "migrated-from.txt"

# The key is the one file that cannot be recreated. The rest are a chain
# that can be fetched again, and caches.
KEY_FILES = ("lapsecoin_key.json", "base_gas.key")
CHAIN_FILES = ("lapsecoin_chain.db", "lapsecoin_chain.db-wal",
               "lapsecoin_chain.db-shm", "lapsecoin_chain_mempool.json")
CACHE_FILES = ("lapsecoin_peers.json", "lapsecoin_lt_dht.dat")
DATA_FILES = KEY_FILES + CHAIN_FILES + CACHE_FILES


def install_dir():
    return os.path.dirname(os.path.realpath(sys.executable))


def is_msi_install():
    import resources
    return (resources.is_frozen() and sys.platform.startswith("win")
            and os.path.exists(os.path.join(install_dir(), MSI_MARKER)))


def user_data_dir(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DIR_NAME)


def installed_exe(environ=None):
    """Where the MSI puts the program, per user. The WiX package and the
    bridge both rely on this being %LOCALAPPDATA%\\Programs\\LapseCoin."""
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Programs", APP_DIR_NAME, "lapsecoin.exe")


def _has_data(directory):
    return any(os.path.exists(os.path.join(directory, n))
               for n in KEY_FILES + ("lapsecoin_chain.db",))


def _copy(src, dst):
    """Copy through a temp name so an interrupted copy never leaves a file
    that looks complete."""
    tmp = dst + ".copying"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _wal_pending(directory):
    wal = os.path.join(directory, "lapsecoin_chain.db-wal")
    return os.path.exists(wal) and os.path.getsize(wal) > 0


def migrate(src, dst):
    """Copy the data files found in src into dst, skipping any dst already
    has. The chain database is left behind when src has a write-ahead log
    with content in it, which means a node was running or stopped without
    finishing: copying that pair is not safe, and the chain syncs again.
    Returns the names copied."""
    if os.path.abspath(src) == os.path.abspath(dst) or not os.path.isdir(src):
        return []
    os.makedirs(dst, exist_ok=True)
    skip_chain = _wal_pending(src)
    copied = []
    for name in DATA_FILES:
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.isfile(s) or os.path.exists(d):
            continue
        if skip_chain and name in CHAIN_FILES:
            continue
        try:
            _copy(s, d)
            copied.append(name)
        except OSError:
            log.warning("[datadir] could not copy %s from %s", name, src, exc_info=True)
    if copied:
        try:
            with open(os.path.join(dst, MIGRATED_NOTE), "a", encoding="utf-8") as f:
                f.write(f"{src}\n")
        except OSError:
            pass
    return copied


def legacy_candidates(home=None):
    """Folders a single-file .exe was most likely run from, newest data
    first. A double-clicked exe keeps its data beside itself, and people keep
    such files in their downloads or on the desktop."""
    home = home or os.path.expanduser("~")
    folders = [os.path.join(home, d) for d in ("Downloads", "Desktop", "Documents", "")]
    found = []
    for folder in folders:
        key = os.path.join(folder, KEY_FILES[0])
        if os.path.isfile(key):
            found.append((os.path.getmtime(key), folder))
    return [f for _, f in sorted(found, reverse=True)]


def prepare(environ=None, home=None):
    """Called before anything else starts. For an installed build, makes the
    data folder, brings an older install's data across if this one is empty,
    and makes it the working directory. Returns the folder, or None when this
    is not an installed build and nothing changed."""
    if not is_msi_install():
        return None
    dst = user_data_dir(environ)
    os.makedirs(dst, exist_ok=True)
    if not _has_data(dst):
        for src in legacy_candidates(home):
            copied = migrate(src, dst)
            if copied:
                log.info("[datadir] brought %s across from %s", ", ".join(copied), src)
                break
    os.chdir(dst)
    return dst
