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
    __slots__ = ("key", "default", "kind", "label", "help", "minimum",
                 "slider_max", "supports_inf")

    def __init__(self, key, default, kind=str, label="", help="", minimum=None,
                 slider_max=None, supports_inf=False):
        self.key     = key
        self.default = default
        self.kind    = kind
        self.label   = label
        self.help    = help
        self.minimum = minimum
        # Purely a UI convenience (the settings page's slider needs some
        # upper bound to draw one at all) and never enforced here: parse()
        # below still accepts anything >= minimum, same as before this
        # existed. A value above it is still valid, just not reachable by
        # dragging the slider itself, the page pairs it with a plain
        # number field for that. None means this setting doesn't get a
        # slider at all (rendered as a number field only).
        self.slider_max = slider_max
        # True only for a setting whose own semantics give infinity a real
        # meaning (see SWAP_AUTO_ACCEPT_MIN_TRUST), never enforced here
        # either: parse() already accepts float("inf") for any float
        # setting on its own terms (Python's float() does), this only
        # tells the settings page to offer a plain checkbox for that one
        # state instead of asking someone to drag a slider to infinity.
        self.supports_inf = supports_inf

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
    "draw_window_seconds", 10.0, float, minimum=0.0, slider_max=60.0,
    label="Draw window (seconds)",
    help="How long a height keeps accepting a better same-height block. "
         "Anything finishing inside it is treated as a tie and decided on "
         "proof rather than speed, so this is also how much of a speed "
         "advantage it takes to win outright. Not a wait, work on the next "
         "height continues throughout.",
)

# How deep a LapseCoin payment must be buried before a swap treats it as
# settled.
#
# The floor is two and the page will not accept less, which is not
# caution but a property of this chain. A height keeps accepting a better
# same-height block for the whole draw window above, so the tip changes
# hands as a matter of routine (Node._reorg_to_sibling) and the node only
# bothers recording a reorg at all once it replaced two blocks or more
# (node.REORG_NOTABLE_DEPTH), because one is ordinary traffic. A payment
# accepted at depth one can therefore be un-accepted by the chain working
# exactly as designed, and in a swap that means reciprocating a payment
# that no longer exists.
#
# Raising it costs time and nothing else: each step waits for this many
# blocks, so the whole trade lengthens roughly in proportion. Worth doing
# for a large trade, pointless for a small one, which is why it is a
# setting rather than a constant.
#
# Snapshotted onto a trade when it starts, so changing this never moves
# the goalposts on a trade already running.
SWAP_CONFIRM_DEPTH = Setting(
    "swap_confirm_depth", 2, int, minimum=2, slider_max=50,
    label="Swap confirmation depth (blocks)",
    help="How many blocks must bury a LapseCoin payment before a swap "
         "counts it as settled. Two is the minimum and the default: the "
         "draw window means the newest block routinely changes hands, so "
         "a single confirmation can be undone by the chain behaving "
         "normally. Higher is safer and slower; each step waits this many "
         "blocks.",
)

# There used to be a per-node setting here, the most this node would
# have outstanding in one step with a stranger. It was never really node-
# local policy the way it claimed to be, despite living in this file
# ("operator policy, never protocol", see the module docstring): it fed
# straight into the exposure cap a maker computed when accepting a fill,
# which is a number the maker signs into the FillResponse and the other
# side (and any bystander reconstructing the trade later, see
# trust._network_tally_by_counterparty) has to be able to recompute
# identically to check it. A value either side could privately tune made
# that impossible to verify and turned trade sizing into something a
# maker unilaterally decided rather than a fact both parties' own public
# trust history determines - exactly the asymmetry swap.plan_mutual and
# swap_engine._open_taker_trade's own cap check now close. What used to
# be this setting is swap.DEFAULT_STRANGER_CAP_STROOPS: one fixed number
# every node computes a trade's terms from, not an operator dial.
#
# The trust score (trust.get_detail's "score") a counterparty must have
# with this node before a fill request against your own order auto-
# accepts. Below it, the request is not declined, it sits on the Market
# page waiting for a person to accept or decline it by hand.
#
# There used to be a second switch here, a plain on/off for auto-accept
# itself. It was redundant with this one: a trust score can never go
# negative, so a floor of zero already means "accept anyone" and is the
# whole of what the switch's "on" position did. Its "off" position (never
# auto-accept, review everything by hand) has no finite score below it,
# but this setting still expresses it directly: enter inf and no
# counterparty, however trusted, will ever clear it, so every request
# waits for a person on the Market page. One number now covers the
# entire range from "trust nobody automatically" to "trust everybody
# automatically" that used to need two settings to say.
#
# Zero, the default, auto-accepts anyone, including a total stranger
# (score 0): the exposure cap already bounds what a stranger can cost
# you in one step, so nothing unsafe changes by leaving this at zero.
# Raising it keeps automatic trading within counterparties who have
# already earned some standing while everyone else waits for a person;
# inf is the limit of that, everyone waits for a person.
SWAP_AUTO_ACCEPT_MIN_TRUST = Setting(
    "swap_auto_accept_min_trust", 0.0, float, minimum=0.0, slider_max=20.0,
    supports_inf=True,
    label="Minimum trust to auto-accept",
    help="A fill request auto-accepts only if this counterparty's trust "
         "score is at least this. Zero (the default) auto-accepts "
         "anyone; your exposure cap, not this number, is what actually "
         "limits what a stranger can cost you. Raise it to auto-trade "
         "only with counterparties who already have some history, and "
         "review everyone else by hand on the Market page. Enter inf to "
         "review every single request yourself, with the same trust "
         "detail either way.",
)

# Two settings used to live here alongside this one: a switch to advertise
# a separate address instead of this node's own, and an env-only override
# naming any address at all. Both existed because a node had to tell its
# peers where to pay it, which tied an address to an IP and published the
# pairing. Nothing tells peers that any more (see
# Node._handle_inbound_alive), so neither has anything left to do: there
# is no advertised address to make private, and no second key to hold the
# proceeds.

# Whether the odds page's hardware cell (CPU, cores, RAM, OS, see
# hardware_info.describe) is shown at all. On by default: it's this
# node's own machine, read fresh on every request, never sent to a peer
# or stored anywhere (see hardware_info's own module docstring), so
# there's nothing to protect by default. The switch exists for whoever
# is showing the dashboard on a screen they don't want naming their own
# hardware to whoever's looking, a stream, a shared display, and the
# like, not because showing it is otherwise a risk.
SHOW_HARDWARE_DETAILS = Setting(
    "show_hardware_details", True, bool,
    label="Show hardware details on the odds page",
    help="Displays this node's own CPU, core count, RAM, and OS on the "
         "odds page. Read fresh on every request, never sent anywhere; "
         "turn off if you'd rather your screen not name your hardware to "
         "whoever's looking at it.",
)

ALL = [DRAW_WINDOW_SECONDS, SWAP_CONFIRM_DEPTH, SWAP_AUTO_ACCEPT_MIN_TRUST,
       SHOW_HARDWARE_DETAILS]


# How long a value read from storage is reused before going back to the
# database for it.
#
# Reading a setting looks like an attribute access and is a SQLite query,
# about a quarter of a millisecond, and the callers are not occasional:
# Node.open_draw reads the draw window once per candidate entering a draw.
# A second of staleness is
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
