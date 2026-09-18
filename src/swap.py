"""Incremental swap: sizing, scheduling, and the rules that bound loss.

Pure logic only. Nothing here touches a chain, a socket, or the database,
so every rule that decides how much money is at risk can be tested
directly. The I/O that acts on these decisions lives in swap_engine.py.

The safety property
-------------------
A trade is delivered in steps, and a step is only sent once the previous
one has settled. So whatever happens, the most anyone loses is one step.
That holds with no trust, no escrow, no arbiter, and no cooperation from
the counterparty, which is what makes it the floor the rest sits on.

Everything else here exists to keep that floor meaningful:

Exposure is capped in absolute value, not as a share of the trade. A
"trusted" peer splitting a trade into two steps would put half of it at
risk in one move, which is the safety property switched off by a good
reputation. The cap is a number of stroops; trust may raise that number,
never remove it.

The first move alternates. Within a step somebody has to send first, and
that party carries the step's risk. Resting it on one side for the whole
trade makes the deal lopsided in a way neither party agreed to, so it
swaps every step, and step one is deliberately small so the opening risk
is smaller still.

A trade too large for the counterparty's standing is refused rather than
quietly made riskier. That refusal is the design working: the alternative
is raising the cap to fit, which is how bounded loss stops being bounded.
"""

import hashlib
import math
import secrets

# Steps are paced by LapseCoin, whose blocks are two minutes apart and
# which needs a couple of them to settle. Stellar's five seconds round to
# nothing against that, so a step costs roughly one confirmation window
# whichever asset moves first.
#
# The ceiling is therefore a time budget, not a safety one: twenty steps
# is already well over an hour. Past this a trade is refused for being
# too large for the trust behind it, which is the honest answer, rather
# than run with steps big enough to break the loss bound.
MAX_INCREMENTS = 20
MIN_INCREMENTS = 2

# The opening step, as a fraction of an ordinary one. Whoever moves first
# on step one is exposed before anything at all has been established, so
# that step is a probe rather than a full increment.
PROBE_FRACTION = 0.25

# What this node will expose in a single step to a counterparty it knows
# nothing about, in stroops. Roughly a dollar at the XLM prices this was
# written against. It is a node-local policy, not a protocol rule: a user
# may lower it freely, and trust is what raises it.
DEFAULT_STRANGER_CAP_STROOPS = 5 * 10_000_000

# The most trust may ever raise that cap, as a multiple. Without a ceiling
# a long record would eventually license a single step large enough that
# losing one hurts, which is the bound dissolving by degrees rather than
# by decision.
MAX_TRUST_MULTIPLIER = 20.0

# Dust floor. A step below this costs more in fees and attention than it
# protects, and on the LapseCoin side an output must be a positive integer
# number of ticks regardless.
MIN_STEP_STROOPS = 1_000        # 0.0001 XLM
MIN_STEP_TICKS = 1


class TradeTooLarge(Exception):
    """This trade cannot be split finely enough to stay inside the loss
    bound without taking longer than the step ceiling allows.

    Carries what would have been needed so the caller can tell the user
    the actual limit instead of a bare refusal.
    """

    def __init__(self, requested_stroops, max_safe_stroops, cap_stroops):
        self.requested_stroops = requested_stroops
        self.max_safe_stroops = max_safe_stroops
        self.cap_stroops = cap_stroops
        super().__init__(
            f"trade of {requested_stroops} stroops needs steps above the "
            f"{cap_stroops}-stroop exposure cap; the most this counterparty "
            f"supports right now is {max_safe_stroops} stroops")


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

def new_session_id(order_id, taker_lapse_addr, nonce=None):
    """The tag tying both chains' halves of one trade together.

    Derived from the order, the taker, and a fresh nonce, so two fills of
    the same order by the same taker never collide. Truncated to fit
    alongside a step number inside LapseCoin's 200-byte memo and Stellar's
    28-byte text memo, which is the binding constraint.
    """
    if nonce is None:
        nonce = secrets.token_hex(8)
    raw = f"{order_id}|{taker_lapse_addr}|{nonce}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


ORDER_TAG_LEN = 8
SESSION_TAG_LEN = 8


def session_tag(order_id, session_id, n):
    """What goes in the memo on both chains: "<order8>:<session8>:<n>".

    Stellar's text memo allows 28 bytes, which is the binding constraint:
    8 hex characters of the order id, a colon, 8 of the session id,
    another colon, and up to two digits of step number is 20 bytes with
    room to spare.

    The order prefix is what lets the maker recognise a payment as
    belonging to one of its own open orders before any trade exists on
    its side at all: without it, a fresh incoming payment carries nothing
    to look up (see swap_engine.discover_trades). Both prefixes only need
    to disambiguate within one node's own small set of live orders and
    live sessions per order, not across the whole network, since a
    payment is only ever read by the address it was actually sent to.
    """
    order8 = order_id.replace("-", "")[:ORDER_TAG_LEN]
    session8 = session_id[:SESSION_TAG_LEN]
    tag = f"{order8}:{session8}:{n}"
    if len(tag.encode()) > 28:
        raise ValueError(f"session tag too long for a Stellar memo: {tag}")
    return tag


def parse_session_tag(memo):
    """Split a memo back into (order8, session8, step), or None if it is
    not one.

    Used when reading either chain, so it has to be strict: anything that
    is merely memo-shaped must not be mistaken for a payment in a trade.
    Both prefixes are validated as lowercase hex of the exact expected
    length, since a memo that merely looks tag-shaped (three colon-joined
    fields) must not be treated as a real reference to an order or
    session it was never signed against.
    """
    if not isinstance(memo, str):
        return None
    parts = memo.split(":")
    if len(parts) != 3:
        return None
    order8, session8, step = parts
    if not _is_hex_of_length(order8, ORDER_TAG_LEN):
        return None
    if not _is_hex_of_length(session8, SESSION_TAG_LEN):
        return None
    if not step.isdigit():
        return None
    return order8, session8, int(step)


def _is_hex_of_length(s, length):
    if len(s) != length:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Exposure cap
# ---------------------------------------------------------------------------

def exposure_cap_stroops(trust_score, stranger_cap=DEFAULT_STRANGER_CAP_STROOPS,
                         peer_stake_stroops=None):
    """The most this node will have outstanding in one step.

    Trust raises the cap along a square root, so a record ten times longer
    buys about three times the step size rather than ten. Reputation
    should make trading smoother, not eventually remove the limit it was
    measured under.

    When the counterparty's stake is known it also hard-caps the result.
    That is the inequality the whole model rests on: a step must be worth
    less than what the counterparty forfeits by walking away from it, or
    defecting pays for itself and no amount of good history matters.
    """
    if trust_score <= 0:
        cap = stranger_cap
    else:
        multiplier = min(1.0 + math.sqrt(trust_score), MAX_TRUST_MULTIPLIER)
        cap = int(stranger_cap * multiplier)
    if peer_stake_stroops is not None:
        cap = min(cap, max(int(peer_stake_stroops), 0))
    return max(cap, MIN_STEP_STROOPS)


def max_safe_trade_stroops(cap_stroops):
    """The largest trade that still fits inside the cap and the step ceiling.

    Spans (MAX_INCREMENTS - 1) full steps plus one probe, matching what
    increment_count actually solves for. Reporting a plain cap times
    ceiling instead would name a limit that is itself refused, which is a
    worse answer than the refusal.
    """
    return int(cap_stroops * (MAX_INCREMENTS - 1 + PROBE_FRACTION))


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def increment_count(xlm_total, cap_stroops):
    """How many steps this trade needs to stay under the cap.

    The probe has to be priced in here, not just in the schedule. A small
    opening step means the remaining steps carry more than an even share,
    so a count derived from total/cap alone produces steps above the cap:
    ten stroops over two steps with a cap of five yields a probe of one
    and a second step of nine. The count is solved from what the body
    steps actually end up being instead.

    With a probe of f times a body step, the trade spans (count - 1 + f)
    bodies, so keeping a body inside the cap needs
    count >= total/cap + 1 - f.

    Raises TradeTooLarge rather than returning a count whose steps would
    exceed the cap. Silently oversizing the steps is the one outcome this
    must never produce: it would report a safe-looking trade that is not.
    """
    if xlm_total <= 0:
        raise ValueError("trade amount must be positive")
    needed = math.ceil(xlm_total / cap_stroops + 1 - PROBE_FRACTION)
    count = max(needed, MIN_INCREMENTS)
    if count > MAX_INCREMENTS:
        raise TradeTooLarge(xlm_total, max_safe_trade_stroops(cap_stroops),
                            cap_stroops)
    return count


def build_schedule(lapse_total, xlm_total, count):
    """Split a trade into per-step amounts.

    Returns a list of (lapse_ticks, xlm_stroops), one per step, summing
    exactly to the totals. Exactly, not approximately: a rounding crumb
    left at the end is a step that can never settle, because the
    counterparty is watching for an amount that does not arrive. The
    remainder is folded into the final step instead.

    Step one is a probe, smaller than the rest, because whoever moves
    first on it is exposed before the trade has established anything.
    """
    if count < 1:
        raise ValueError("a trade needs at least one step")
    if lapse_total < count or xlm_total < count:
        raise ValueError("trade too small to split into this many steps")

    def split(total):
        if count == 1:
            return [total]
        # Sized from the body step rather than from an even share, so the
        # probe shrinks the opening step without inflating the rest past
        # what increment_count solved for.
        body = int(total / (count - 1 + PROBE_FRACTION))
        probe = max(int(body * PROBE_FRACTION), 1)
        amounts = [probe] + [body] * (count - 1)
        # Integer division leaves a shortfall. Spreading it across the body
        # steps keeps every one of them within a stroop of `body`; dumping
        # it all on the last step instead can push that one step clear of
        # the exposure cap, which is the bound quietly breaking on the
        # largest step of the trade.
        short = total - sum(amounts)
        if short > 0:
            each, extra = divmod(short, count - 1)
            for i in range(1, count):
                amounts[i] += each
            for i in range(1, 1 + extra):
                amounts[i] += 1
        return amounts

    lapse_parts = split(lapse_total)
    xlm_parts = split(xlm_total)
    assert sum(lapse_parts) == lapse_total
    assert sum(xlm_parts) == xlm_total
    if any(p < MIN_STEP_TICKS for p in lapse_parts):
        raise ValueError("a step would be below the minimum LAPSE amount")
    if any(p < 1 for p in xlm_parts):
        raise ValueError("a step would be below one stroop")
    return list(zip(lapse_parts, xlm_parts))


def plan(lapse_total, xlm_total, trust_score,
         stranger_cap=DEFAULT_STRANGER_CAP_STROOPS, peer_stake_stroops=None):
    """Everything about how a trade will be executed, decided up front.

    Returns (schedule, count, cap). Raising TradeTooLarge here, before a
    trade exists, is what keeps the refusal cheap and legible: the user is
    told the limit while they are still choosing an amount.

    The cap is then checked against the schedule that was actually built,
    rather than trusted from the arithmetic that chose the count. Integer
    rounding can leave one step a little above what the formula solved
    for, and this is the number that decides how much is at risk, so it is
    verified rather than assumed. A step over the line adds another step
    and rebuilds.
    """
    cap = exposure_cap_stroops(trust_score, stranger_cap, peer_stake_stroops)
    count = increment_count(xlm_total, cap)
    while count <= MAX_INCREMENTS:
        schedule = build_schedule(lapse_total, xlm_total, count)
        if max(xlm for _lapse, xlm in schedule) <= cap:
            return schedule, count, cap
        count += 1
    raise TradeTooLarge(xlm_total, max_safe_trade_stroops(cap), cap)


# ---------------------------------------------------------------------------
# Who moves first
# ---------------------------------------------------------------------------

def opening_mover(my_trust_of_peer, peer_trust_of_me):
    """Which side sends first on step one: True if this node does.

    The less-established side opens. They are the one asking to be
    trusted, so they are the one who demonstrates it, and the probe step
    keeps what they risk small.

    Ties go to the taker, decided by the caller passing its own role; an
    even match still needs somebody to start, and picking the side that
    chose to engage is arbitrary but stable, which is what matters.
    """
    if my_trust_of_peer == peer_trust_of_me:
        return None      # caller breaks the tie by role
    return my_trust_of_peer > peer_trust_of_me


def i_move_first(step_n, i_open):
    """Whether this node sends first on a given step.

    Alternates, so neither side carries the first-move risk for the whole
    trade. Over a run of steps the exposure comes out even.
    """
    if step_n < 1:
        raise ValueError("steps are numbered from 1")
    return i_open if step_n % 2 == 1 else not i_open


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

TICKS_PER_LAPSE = 100_000_000


def xlm_for_lapse(lapse_ticks, price_stroops_per_lapse):
    """What a LAPSE amount costs in stroops at a given price.

    Integer arithmetic throughout, rounded up. Rounding up rather than to
    nearest means any rounding step favours the party receiving XLM, which
    is the side that would otherwise be short-changed by a fraction of a
    stroop on every step; over many steps a consistent direction is the
    difference between a trade that settles and one that stalls one
    stroop from the end.
    """
    if price_stroops_per_lapse <= 0:
        raise ValueError("price must be positive")
    return -(-lapse_ticks * price_stroops_per_lapse // TICKS_PER_LAPSE)


def lapse_for_xlm(stroops, price_stroops_per_lapse):
    """The most LAPSE a given number of stroops buys at this price.

    For the "you can afford about this much" line in the UI, and for
    nothing that decides a trade. A trade's terms are the two totals the
    parties agreed, and per-step amounts are split from those totals
    directly, so this is never in a path where money moves.

    Its guarantee is one-directional and that is the useful direction:
    what comes back always costs no more than the stroops put in, so a
    quote built from it can always be paid. The reverse does not hold, and
    should not be expected to. xlm_for_lapse rounds a cost up, so that
    rounded-up cost genuinely does buy slightly more than what was asked
    for; that is the buyer's fraction of a stroop, not value invented.
    """
    if price_stroops_per_lapse <= 0:
        raise ValueError("price must be positive")
    return stroops * TICKS_PER_LAPSE // price_stroops_per_lapse
