"""Best-effort check for a newer released version.

Points at a raw VERSION file (defaults to this project's own repo) rather
than a GitHub Releases API endpoint. Two reasons:
  1. It works the same way whether the canonical project or someone's fork
     bumped VERSION, without needing a formal GitHub Release object to
     exist for that bump.
  2. raw.githubusercontent.com is served off a CDN, not GitHub's REST API,
     which caps unauthenticated requests at 60/hour *per source IP*,
     shared across the whole API. Several nodes behind one NAT/office IP
     polling that API would collectively risk that shared ceiling; the
     raw-content path doesn't have this problem.

This module never uploads anything about the node. It's a plain GET of
a static file, the same shape of request discovery.py's IP-echo fallback
already makes by default.
"""

import logging
import threading
import time

log = logging.getLogger("ec.update")

DEFAULT_VERSION_URL  = "https://raw.githubusercontent.com/Lapsecoin/core/main/VERSION"
DEFAULT_RELEASES_URL = "https://github.com/Lapsecoin/core/releases"

# A version bump is a rare event; this just keeps latency to noticing one
# low without polling anything unnecessarily.
CHECK_INTERVAL_SECONDS = 3600


def _parse_version(s):
    """'0.1.1' -> (0, 1, 1). Returns None if unparseable."""
    try:
        parts = tuple(int(p) for p in s.strip().split("."))
        return parts if parts else None
    except (ValueError, AttributeError):
        return None


def classify_update(remote: str, local: str):
    """Return None if remote isn't newer than local. Otherwise, the most
    significant differing version component:
      'protocol', first component (major) changed: a wire/consensus
                    break is likely; old and new nodes may not sync.
      'critical', second component (minor) changed.
      'minor'   . Only the third+ component (patch) changed.
    """
    r, l = _parse_version(remote), _parse_version(local)
    if r is None or l is None:
        return None
    width = max(len(r), len(l), 3)
    r = r + (0,) * (width - len(r))
    l = l + (0,) * (width - len(l))
    if r <= l:
        return None
    if r[0] != l[0]:
        return "protocol"
    if r[1] != l[1]:
        return "critical"
    return "minor"


class UpdateChecker:
    """Polls a VERSION file URL on its own daemon thread.

    .severity / .latest_version are the only state the web UI reads. Only
    this object's own poll thread ever writes them, so plain attribute
    access is safe without a lock.
    """

    def __init__(self, local_version, version_url=DEFAULT_VERSION_URL,
                 releases_url=DEFAULT_RELEASES_URL,
                 interval=CHECK_INTERVAL_SECONDS):
        self.local_version  = local_version
        self.version_url    = version_url
        self.releases_url   = releases_url
        self.interval       = interval
        self.severity       = None   # None | "minor" | "critical" | "protocol"
        self.latest_version = None

    def start(self):
        """No-op if disabled (version_url falsy)."""
        if not self.version_url:
            return
        threading.Thread(target=self._loop, daemon=True, name="update-check").start()

    def _loop(self):
        while True:
            self.check_once()
            time.sleep(self.interval)

    def check_once(self):
        """One check, synchronous. Never raises, any failure (missing
        'requests', network error, bad response) just leaves state as-is."""
        try:
            import requests
        except ImportError:
            log.debug("[update] 'requests' not installed; update check disabled")
            return
        try:
            r = requests.get(self.version_url, timeout=10)
            if r.status_code != 200:
                return
            remote = r.text.strip()
            severity = classify_update(remote, self.local_version)
            if severity:
                self.latest_version = remote
                self.severity = severity
                log.info("[update] newer version available: %s (current %s, severity=%s)",
                         remote, self.local_version, severity)
        except Exception:
            log.debug("[update] check failed", exc_info=True)
