"""What this node believes about a counterparty, and what that buys them.

Trust here does one job: it decides how large a single step may be. It
never decides whether a trade is safe, because the loss bound in swap.py
already does that without reference to anyone's reputation. A peer with a
spotless record still cannot get a step above the cap, and a total
stranger can still trade, just in smaller pieces.

That division is deliberate. A reputation system that gates safety is one
that fails open the moment it is fooled; this one only ever trades
smoothness for caution, so being fooled costs fees and time rather than
money.

Why a trade count alone is worthless
------------------------------------
A LapseCoin address costs nothing to create, so reputation attached to
one is reputation attached to nothing: defect, discard, generate another.
Bisq refuses to run a reputation system at all for exactly this reason,
and the first version of this file had the same hole.

So standing is not counted, it is weighed against what the address would
forfeit by being thrown away:

  - how long it has existed on chain, which cannot be bought
  - what it holds, which can be, but not for free

Both are read from the public ledger, so nothing needs to be agreed with
anybody and a lie is not expressible. A Sybil is still possible; it is
just no longer free, and pricing it is the achievable goal. Preventing it
outright is not, without an identity system this design is right not to
have.

Slashing
--------
One unreciprocated payment zeroes the score outright, rather than
decrementing it. Recovering means aging and funding a fresh address,
which is the cost that makes the number mean anything.

What it does not do is fire on a timeout. A stalled trade is not a slash;
see swap_engine.consider_abandonment for the margin and the reachability
evidence required first. A peer whose node was restarting must never lose
standing for it.
"""

import logging
import math
import time

from trade_storage import PeerRecord, ensure_tables

log = logging.getLogger("ec.trust")

# How long good standing takes to halve without further trading. Old
# behaviour should not vouch indefinitely for a peer nobody has dealt
# with recently, but a trader who takes a month off should not start over
# either.
DECAY_HALFLIFE_SECONDS = 90 * 86_400

# Blocks before an address's age counts for anything, and where the age
# component saturates. Roughly a day and roughly a month at two-minute
# blocks. A Sybil can wait, but waiting is the cost.
AGE_FLOOR_BLOCKS = 720
AGE_FULL_BLOCKS = 21_600

# Balance at which the stake component saturates, in ticks. Holding more
# than this proves nothing further; the point is skin in the game, not a
# wealth ranking.
STAKE_FULL_TICKS = 100 * 100_000_000

TICKS_PER_LAPSE = 100_000_000


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def history_component(completed_count, completed_ticks, last_completed_at,
                      now=None):
    """Standing earned by trades that actually settled.

    Square root in volume and logarithmic in count, so neither one large
    trade nor a burst of tiny ones buys standing out of proportion to it.
    Decays with time since the last completed trade.
    """
    if completed_count <= 0:
        return 0.0
    now = time.time() if now is None else now
    volume = math.sqrt(max(completed_ticks, 0) / TICKS_PER_LAPSE)
    breadth = math.log1p(completed_count)
    earned = volume * breadth
    age = max(now - (last_completed_at or 0), 0)
    return earned * (0.5 ** (age / DECAY_HALFLIFE_SECONDS))


def stake_component(address_age_blocks, balance_ticks):
    """What the address would cost to replace, as a multiplier in [0, 1].

    The two halves are multiplied rather than added, so both are required:
    a freshly minted address holding a fortune scores zero, and so does an
    ancient empty one. Either alone is cheap to manufacture, which is
    precisely the case this exists to price.
    """
    if address_age_blocks < AGE_FLOOR_BLOCKS:
        return 0.0
    span = max(AGE_FULL_BLOCKS - AGE_FLOOR_BLOCKS, 1)
    age = min((address_age_blocks - AGE_FLOOR_BLOCKS) / span, 1.0)
    stake = min(max(balance_ticks, 0) / STAKE_FULL_TICKS, 1.0)
    return age * stake


def score(completed_count, completed_ticks, abandoned_count,
          last_completed_at, address_age_blocks=0, balance_ticks=0,
          now=None):
    """A peer's standing. Zero for anyone who has ever walked away.

    History is multiplied by stake rather than added to it, so a record
    built on a throwaway address is worth nothing however long it is.
    That multiplication is the entire Sybil defence: it makes standing
    non-transferable to a fresh identity.
    """
    if abandoned_count > 0:
        return 0.0
    history = history_component(completed_count, completed_ticks,
                                last_completed_at, now=now)
    if history <= 0:
        return 0.0
    return history * stake_component(address_age_blocks, balance_ticks)


# ---------------------------------------------------------------------------
# Stored records
# ---------------------------------------------------------------------------

def _record(addr):
    ensure_tables()
    row, _created = PeerRecord.get_or_create(lapse_addr=addr)
    return row


def get_score(addr, address_age_blocks=0, balance_ticks=0, now=None):
    """Standing for one peer, or 0 for one never seen."""
    ensure_tables()
    row = PeerRecord.get_or_none(PeerRecord.lapse_addr == addr)
    if row is None:
        return 0.0
    return score(row.completed_count, row.completed_lapse, row.abandoned_count,
                 row.last_completed_at, address_age_blocks, balance_ticks, now)


def get_detail(addr, address_age_blocks=0, balance_ticks=0, now=None):
    """Standing plus the figures behind it.

    Broken out because a score with no stated reason is a verdict, and a
    user deciding whether to trade with somebody deserves to see what the
    number is made of rather than be handed it.
    """
    ensure_tables()
    row = PeerRecord.get_or_none(PeerRecord.lapse_addr == addr)
    if row is None:
        return {"score": 0.0, "completed_count": 0, "completed_lapse": 0,
                "abandoned_count": 0, "last_completed_at": 0.0,
                "last_abandoned_at": 0.0, "last_abandon_session": "",
                "stake": 0.0, "history": 0.0, "known": False}
    history = history_component(row.completed_count, row.completed_lapse,
                                row.last_completed_at, now=now)
    stake = stake_component(address_age_blocks, balance_ticks)
    return {
        "score": score(row.completed_count, row.completed_lapse,
                       row.abandoned_count, row.last_completed_at,
                       address_age_blocks, balance_ticks, now),
        "completed_count": row.completed_count,
        "completed_lapse": row.completed_lapse,
        "abandoned_count": row.abandoned_count,
        "last_completed_at": row.last_completed_at,
        "last_abandoned_at": row.last_abandoned_at,
        "last_abandon_session": row.last_abandon_session,
        "history": history,
        "stake": stake,
        "known": True,
    }


def record_completed(addr, ticks):
    ensure_tables()
    row = _record(addr)
    row.completed_count += 1
    row.completed_lapse += max(ticks, 0)
    row.last_completed_at = time.time()
    row.save()
    return row


def record_abandonment(addr, session_id=""):
    """Register that a peer took a payment and did not reciprocate.

    Called only from swap_engine.consider_abandonment, which requires a
    wide margin past the deadline and evidence the peer was reachable
    throughout. Nothing else should call this: a timeout on its own is not
    proof of anything, and this is not recoverable by waiting.
    """
    ensure_tables()
    row = _record(addr)
    row.abandoned_count += 1
    row.last_abandoned_at = time.time()
    row.last_abandon_session = session_id
    row.save()
    log.warning("[trust] %s marked as having abandoned a trade (session %s); "
                "standing zeroed", addr[:24], session_id or "?")
    return row


def all_scores(stake_lookup=None, now=None):
    """Every peer this node has dealt with, scored.

    stake_lookup(addr) -> (age_blocks, balance_ticks), so the caller
    supplies chain facts rather than this module reaching for a node.
    Without it the stake term is zero, which makes every score zero; that
    is the honest answer when the chain cannot be consulted, not a reason
    to fall back to counting trades.
    """
    ensure_tables()
    out = {}
    for row in PeerRecord.select():
        age, balance = (0, 0)
        if stake_lookup is not None:
            age, balance = stake_lookup(row.lapse_addr)
        out[row.lapse_addr] = score(
            row.completed_count, row.completed_lapse, row.abandoned_count,
            row.last_completed_at, age, balance, now)
    return out


# ---------------------------------------------------------------------------
# Chain facts
# ---------------------------------------------------------------------------

def address_age_blocks(node, addr):
    """How many blocks since this address was first seen on chain.

    Read from the transaction index rather than tracked separately, so it
    cannot be inflated by anything a peer says. An address with no history
    is brand new by definition and scores zero age.
    """
    heights = node.storage.get_tx_heights_for_addr(addr)
    if not heights:
        return 0
    first_seen = min(height for height, _tx_hash in heights)
    return max(node.view.chain[-1]["height"] - first_seen, 0)


def stake_lookup_for(node):
    """A stake_lookup bound to a running node, for all_scores."""

    def lookup(addr):
        return (address_age_blocks(node, addr),
                node.view.state.get_balance(addr))

    return lookup
