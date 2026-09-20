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
    "swap_confirm_depth", 2, int, minimum=2,
    label="Swap confirmation depth (blocks)",
    help="How many blocks must bury a LapseCoin payment before a swap "
         "counts it as settled. Two is the minimum and the default: the "
         "draw window means the newest block routinely changes hands, so "
         "a single confirmation can be undone by the chain behaving "
         "normally. Higher is safer and slower; each step waits this many "
         "blocks.",
)

# The most this node will have outstanding in one step of a swap with a
# counterparty it has no history with, in stroops (10,000,000 to the XLM).
#
# This is the number that decides how much a stranger can actually take
# from you: a swap is delivered in steps and a step is only sent once the
# previous one settled, so the worst case is exactly one step. Lowering it
# makes that worst case smaller and the trade longer, since the same total
# is delivered in more pieces.
#
# Trust raises this per counterparty and never removes it, and the trade
# is refused outright rather than made riskier if it cannot be split
# finely enough (see swap.plan).
SWAP_STRANGER_CAP_STROOPS = Setting(
    "swap_stranger_cap_stroops", 50_000_000, int, minimum=1_000,
    label="Maximum exposure per step (stroops)",
    help="The most that can be outstanding at any moment when trading "
         "with someone you have no history with. 10,000,000 stroops is 1 "
         "XLM. This is your worst case if a stranger takes a payment and "
         "walks away. Lower is safer and splits a trade into more steps.",
)

# Whether this node runs swaps at all. This does not itself create a
# wallet or contact Horizon: a Stellar trading wallet is only ever made
# by an explicit, passphrase-gated action on the Market page
# (market_routes._create_wallet), and nothing here touches Horizon until
# that wallet exists and a trade actually needs it. What this switch
# actually gates is posting your own orders, answering fill requests,
# and running the swap worker's trade loop; a node with it off still
# relays other people's orders and receipts exactly as before, it just
# never becomes a party to a trade itself. On by default: turning it off
# is the deliberate act, for someone who wants a LapseCoin node with the
# market surface switched off entirely.
SWAP_ENABLED = Setting(
    "swap_enabled", True, bool,
    label="Enable peer-to-peer swaps",
    help="Trade LAPSE for XLM directly with peers. On by default; turning "
         "it off still relays other people's orders (like any other "
         "gossip) but posts none of your own, answers no fill requests, "
         "and runs no trade. No Stellar wallet is created and nothing "
         "contacts Horizon either way until you explicitly create one on "
         "the Market page.",
)

# Whether a fill request against one of this node's own orders is ever
# accepted automatically, the moment it clears the trust-based exposure
# cap (see swap.plan), rather than always left pending on the Market
# page for a person to accept or decline by hand.
#
# On by default: the exposure cap is what actually bounds the loss on a
# bad decision (see swap.py's module docstring), not this switch, so
# auto-accept is not a weaker safety mode, only a faster one. Someone
# who wants to look at every single request before a stroop moves, not
# just find out afterward from the Trades page, turns this off; nothing
# about what a request may cost changes either way. SWAP_AUTO_ACCEPT_
# MIN_TRUST below is the finer control most people actually want: off
# for everyone is the blunt version of "trust nobody automatically",
# raising the floor there is the graduated one.
SWAP_AUTO_ACCEPT_FILLS = Setting(
    "swap_auto_accept_fills", True, bool,
    label="Auto-accept fill requests",
    help="Accept a fill against your own order the moment it clears "
         "your exposure cap and this node's trust floor (see below), "
         "without waiting for you to look at it. Turn this off to "
         "review every request yourself on the Market page (with the "
         "same trust detail either way) before anything is agreed to.",
)

# The trust score (trust.get_detail's "score") a counterparty must have
# with this node before a fill request against your own order auto-
# accepts, when SWAP_AUTO_ACCEPT_FILLS is on. Below it, the request is
# not declined, it sits on the Market page exactly like it would with
# auto-accept off, waiting for a person to accept or decline it by hand.
#
# Zero, the default, auto-accepts anyone, including a total stranger
# (score 0), same as this node has always done: the exposure cap already
# bounds what a stranger can cost you in one step, so nothing unsafe
# changes by leaving this at zero. Raising it is for someone who wants
# automatic trading to stay within counterparties who have already
# earned some standing, and to see everyone else before committing,
# without having to turn auto-accept off altogether and review every
# single request, established counterparties included.
SWAP_AUTO_ACCEPT_MIN_TRUST = Setting(
    "swap_auto_accept_min_trust", 0.0, float, minimum=0.0,
    label="Minimum trust to auto-accept",
    help="A fill request auto-accepts only if this counterparty's trust "
         "score is at least this. Zero (the default) auto-accepts "
         "anyone; your exposure cap, not this number, is what actually "
         "limits what a stranger can cost you. Raise it to auto-trade "
         "only with counterparties who already have some history, and "
         "review everyone else by hand on the Market page.",
)

# Two settings used to live here alongside this one: a switch to advertise
# a separate address instead of this node's own, and an env-only override
# naming any address at all. Both existed because a node had to tell its
# peers where to pay it, which tied an address to an IP and published the
# pairing. Nothing tells peers that any more (see
# Node._handle_inbound_alive), so neither has anything left to do: there
# is no advertised address to make private, and no second key to hold the
# proceeds.
ALL = [DRAW_WINDOW_SECONDS, SWAP_ENABLED, SWAP_CONFIRM_DEPTH,
       SWAP_STRANGER_CAP_STROOPS, SWAP_AUTO_ACCEPT_FILLS,
       SWAP_AUTO_ACCEPT_MIN_TRUST]


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
