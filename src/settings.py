"""Node-local settings: operator policy, never protocol.

Nothing here is consensus. Every value is a choice one operator makes for
their own node, and no other node can tell or care what it is set to.

Resolution order, highest first:
  1. environment variable
  2. value stored in this node's database
  3. shipped default

The environment comes first so a node can be run with a setting forced for
one launch (a container, a systemd unit, a one-off) without that quietly
rewriting what the operator saved. When a value is forced that way the
settings page shows it as such rather than pretending it can be edited.
"""

import logging
import os
import threading
import time

log = logging.getLogger("ec.settings")

ENV_PREFIX = "LAPSECOIN_"


class Setting:
    __slots__ = ("key", "default", "kind", "label", "help", "minimum")

    def __init__(self, key, default, kind=str, label="", help="", minimum=None):
        self.key     = key
        self.default = default
        self.kind    = kind
        self.label   = label
        self.help    = help
        self.minimum = minimum

    @property
    def env_name(self):
        return ENV_PREFIX + self.key.upper()

    def parse(self, raw):
        """Parse and range-check, raising ValueError on anything this
        setting can't hold. One implementation for both sources, so a value
        the settings page would reject can't get in through the environment
        instead."""
        if self.kind is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        value = self.kind(raw)
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.key} must be at least {self.minimum}")
        return value


# Privacy is off by default deliberately. A node that advertises nothing is
# harder to pay and harder to reach, and most operators want to be findable;
# the ones who don't should have to say so.
PRIVATE_ADDRESS = Setting(
    "private_address", False, bool,
    label="Hide wallet address from peers",
    help="Advertises a separate, already-generated address instead of "
         "the one this node builds blocks with.",
)

ADVERTISED_ADDRESS = Setting(
    "advertised_address", "", str,
    label="Address to advertise",
    help="Overrides the generated privacy address. Operator/deployment "
         "use only, set via environment; not shown on the settings page.",
)

# How long a height keeps accepting a better same-height block. See
# Node._reorg_to_sibling for what the draw is and Node.open_draw for what
# the window is anchored to.
#
# What this number really sets is the draw's tie tolerance: a builder who
# finishes within it of the first finisher gets compared on vdf_output,
# and one who finishes outside it loses the height outright however good
# its output would have been. So it is the line between "these two tied"
# and "that one was simply slower", expressed in seconds.
#
# It has a floor, and the floor is propagation, not taste. A block has to
# reach other nodes before their windows close, so a window shorter than
# the time a block takes to get around is one where even a genuinely
# simultaneous builder loses for being far away in the graph. That is the
# draw degrading back into deciding heights by network position, which is
# the one thing it exists to prevent, and it degrades silently: nothing
# errors, the draws just quietly stop being fair. Propagation here is not
# raw link latency either, since a block walks a stem of ~10 expected
# hops (gossip.STEM_CONTINUE_PROB) before it floods at all.
#
# The ceiling is not free either, and it is the side that is easy to get
# wrong. Every second above propagation is a second of real speed
# advantage converted into a coin flip: a builder ten seconds faster than
# the field wins outright under a five-second window and ties under a
# fifteen-second one, having done nothing differently. Measured
# propagation is around half a second for an ordinary block, so a setting
# in the low seconds already clears the floor several times over, and the
# rest is a choice about how much of a lead should count.
#
# This node does not widen it from measurement, and deliberately so after
# trying. What was measured was the gap between a height's first candidate
# and each later one, which is not propagation but how far apart the
# builders are in speed, so the window grew to cover exactly the
# differences the draw exists to settle and erased the lead it was meant
# to adjudicate. One number, set here.
#
# Worth knowing that unlike most settings here, this one is not purely
# local in effect: nodes running very different windows admit different
# sets of entrants to the same draw, and disagree more often as a result.
DRAW_WINDOW_SECONDS = Setting(
    "draw_window_seconds", 10.0, float, minimum=0.0,
    label="Draw window (seconds)",
    help="How long a height keeps accepting a better same-height block. "
         "Anything finishing inside it is treated as a tie and decided on "
         "proof rather than speed, so this is also how much of a speed "
         "advantage it takes to win outright. Not a wait, work on the next "
         "height continues throughout.",
)

# ADVERTISED_ADDRESS is deliberately not in ALL: it's an env-only escape
# hatch for operators, not a page field. The page shows the generated
# privacy address (always already there, see Node.ensure_privacy_key)
# and lets the operator flip whether it's used, nothing to type in.
ALL = [PRIVATE_ADDRESS, DRAW_WINDOW_SECONDS]


# How long a value read from storage is reused before going back to the
# database for it.
#
# Reading a setting looks like an attribute access and is a SQLite query,
# about a quarter of a millisecond, and the callers are not occasional:
# Node.open_draw reads the draw window once per candidate entering a draw,
# and Node.advertised_addr (up to three reads) runs on every inbound
# GETINFO, from a UDP worker thread. A second of staleness is
# indistinguishable from none for a value a person edits by hand on a
# settings page, and set() invalidates immediately anyway, so the page
# still reflects a change on the very next read.
CACHE_SECONDS = 1.0


class Settings:
    """Reads through env -> storage -> default on every access, so a value
    changed from the settings page takes effect without a restart."""

    def __init__(self, storage, cache_seconds=CACHE_SECONDS):
        self.storage = storage
        self._cache_seconds = cache_seconds
        self._cache = {}          # setting key -> (value, read_at)
        self._lock  = threading.Lock()

    def get(self, setting):
        # The environment always wins and never touches the database, so it
        # is checked first and is not what the cache is for.
        raw = os.environ.get(setting.env_name)
        if raw is not None:
            try:
                return setting.parse(raw)
            except (TypeError, ValueError):
                log.warning("[settings] %s is not a valid %s, ignoring",
                            setting.env_name, setting.kind.__name__)

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(setting.key)
        if cached is not None and now - cached[1] < self._cache_seconds:
            return cached[0]

        value = self._read_stored(setting)
        with self._lock:
            self._cache[setting.key] = (value, now)
        return value

    def _read_stored(self, setting):
        raw = self.storage.get_meta("setting_" + setting.key)
        if raw is not None:
            try:
                return setting.parse(raw)
            except (TypeError, ValueError):
                log.warning("[settings] stored %s unreadable, using default",
                            setting.key)
        return setting.default

    def set(self, setting, value):
        self.storage.set_meta("setting_" + setting.key, str(value))
        with self._lock:
            self._cache.pop(setting.key, None)

    def forced_by_env(self, setting):
        return os.environ.get(setting.env_name) is not None
