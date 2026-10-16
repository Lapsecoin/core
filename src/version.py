"""This node's own baked-in version, read from the VERSION file bundled
alongside the source (or the PyInstaller bundle root when frozen)."""

import os


def _load_local_version():
    import resources
    base = resources.bundle_dir()
    path = os.path.join(base, "VERSION")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


LOCAL_VERSION = _load_local_version()
