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
where they were, and nothing is ever overwritten in the new location. On the
first run the user is asked where the old program ran from, with the folders
found under Desktop and Downloads offered first (see migrate_ui.py).
"""

import logging
import os
import shutil
import sys
import time

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


# Folders to look in for an earlier install, with how deep to go. Desktop and
# Downloads are where a downloaded single-file program usually ends up; a
# synced Desktop lives under OneDrive.
_SEARCH = (("Desktop", 3), ("OneDrive/Desktop", 3), ("Downloads", 3),
           ("Documents", 2), ("OneDrive/Documents", 2), ("", 1))
_SKIP = {"appdata", "node_modules", "$recycle.bin", "windows", "program files",
         "program files (x86)", "programdata", ".git", "__pycache__"}


def find_legacy_folders(home=None, max_dirs=4000, seconds=3.0):
    """Folders that hold an earlier install's key, most recently used first.
    The search is bounded in depth, in folders visited and in time, so a huge
    Downloads folder cannot hold up the first start."""
    home = home or os.path.expanduser("~")
    deadline = time.monotonic() + seconds
    seen, found, visited = set(), [], 0
    for sub, depth in _SEARCH:
        root = os.path.join(home, *sub.split("/")) if sub else home
        queue = [(root, 0)]
        while queue and visited < max_dirs and time.monotonic() < deadline:
            folder, level = queue.pop(0)
            real = os.path.realpath(folder)
            if real in seen:
                continue
            seen.add(real)
            visited += 1
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.name == KEY_FILES[0] and e.is_file():
                        found.append((e.stat().st_mtime, folder))
                    elif (level < depth and e.is_dir(follow_symlinks=False)
                          and e.name.lower() not in _SKIP and not e.name.startswith(".")):
                        queue.append((e.path, level + 1))
                except OSError:
                    continue
    found.sort(reverse=True)
    return [folder for _, folder in found]


def prepare(environ=None, home=None, ask=None):
    """Called before anything else starts. For an installed build, makes the
    data folder and makes it the working directory. On the very first run,
    when there is no key or chain yet, asks whether an earlier install exists
    and brings its data across. Returns the folder, or None when this is not
    an installed build and nothing changed."""
    if not is_msi_install():
        return None
    dst = user_data_dir(environ)
    os.makedirs(dst, exist_ok=True)
    # Asking for help is not starting the node: no question, nothing copied.
    asking_for_help = any(a in ("-h", "--help") for a in sys.argv[1:])
    if not _has_data(dst) and not asking_for_help:
        import migrate_ui
        suggestions = find_legacy_folders(home)
        kind, folder = (ask or migrate_ui.ask)(suggestions)
        if kind == migrate_ui.CANCEL:
            # Closing the question must not quietly make a new key.
            sys.exit(0)
        if kind == migrate_ui.USE and folder:
            copied = migrate(folder, dst)
            if copied:
                log.info("[datadir] brought %s across from %s", ", ".join(copied), folder)
            else:
                log.warning("[datadir] nothing to copy from %s", folder)
    os.chdir(dst)
    return dst
