"""Keep repetitive log lines readable.

A node retrying the same unreachable address every minute, or answering the
same relay hundreds of times, writes the same line each time. Throttle lets
the first through, then at most one per interval for that key, and says how
many it held back, so nothing is hidden, only folded.
"""

import threading
import time


class Throttle:
    def __init__(self, interval):
        self.interval = interval
        self._lock = threading.Lock()
        self._state = {}   # key -> (last emitted, suppressed since)

    def allow(self, key):
        """Return None to skip this line, or the number of lines held back
        since the last one that was let through (0 for the first)."""
        now = time.monotonic()
        with self._lock:
            last, held = self._state.get(key, (None, 0))
            if last is not None and now - last < self.interval:
                self._state[key] = (last, held + 1)
                return None
            self._state[key] = (now, 0)
            if len(self._state) > 2048:
                cutoff = now - self.interval
                self._state = {k: v for k, v in self._state.items() if v[0] >= cutoff}
            return held


def more(held):
    """Suffix for a line that stood in for `held` skipped ones."""
    return f"  (+{held} more since the last)" if held else ""
