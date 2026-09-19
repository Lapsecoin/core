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


def get_detail(addr, address_age_blocks=0, balance_ticks=0, now=None, node=None):
    """Standing plus the figures behind it.

    Broken out because a score with no stated reason is a verdict, and a
    user deciding whether to trade with somebody deserves to see what the
    number is made of rather than be handed it.

    node is optional and, when given, folds in this node's own verified
    reading of gossiped step receipts (see market.py's step-receipts
    section) naming addr, for sessions this node was not itself a party
    to. Without it, standing is exactly the old bilateral figure, built
    only from trades this node personally ran with addr; a caller that
    never passes node keeps behaving exactly as before. See
    _network_tally for what "verified" means here and why a receipt this
    node cannot currently check contributes nothing either way rather
    than being guessed at.
    """
    ensure_tables()
    row = PeerRecord.get_or_none(PeerRecord.lapse_addr == addr)
    completed_count = row.completed_count if row else 0
    completed_lapse = row.completed_lapse if row else 0
    abandoned_count = row.abandoned_count if row else 0
    last_completed_at = row.last_completed_at if row else 0.0
    last_abandoned_at = row.last_abandoned_at if row else 0.0
    last_abandon_session = row.last_abandon_session if row else ""
    known = row is not None

    # Broken out from the merged totals below so a caller (see
    # market_take.html's Track record card) can say plainly which part
    # of this is this node's own experience and which part is this node
    # independently verifying what other traders have gossiped, rather
    # than presenting one blended number as if it all came from the same
    # place.
    net_abandoned = net_count = net_lapse = 0
    if node is not None:
        net_abandoned, net_count, net_lapse, net_last_completed = \
            _network_tally(addr)
        if net_abandoned or net_count:
            known = True
        abandoned_count += net_abandoned
        completed_count += net_count
        completed_lapse += net_lapse
        last_completed_at = max(last_completed_at, net_last_completed)

    history = history_component(completed_count, completed_lapse,
                                last_completed_at, now=now)
    stake = stake_component(address_age_blocks, balance_ticks)
    return {
        "score": score(completed_count, completed_lapse, abandoned_count,
                       last_completed_at, address_age_blocks, balance_ticks, now),
        "completed_count": completed_count,
        "completed_lapse": completed_lapse,
        "network_completed_count": net_count,
        "network_abandoned_count": net_abandoned,
        "abandoned_count": abandoned_count,
        "last_completed_at": last_completed_at,
        "last_abandoned_at": last_abandoned_at,
        "last_abandon_session": last_abandon_session,
        "history": history,
        "stake": stake,
        "known": known,
    }


def _network_tally(addr):
    """(abandoned, completed_count, completed_lapse, last_completed_at)
    from already-verified step receipts naming addr, excluding any
    session this node already has a local Trade row for (see PeerRecord:
    those are already counted above, and counting them again from a
    receipt this node itself likely emitted would double them).

    Deliberately DB-only: this runs on every trust lookup, including from
    a page render, so it must never itself make a network call. Turning
    an unverified receipt into a verified one is verify_pending_receipts'
    job, run in the background by the swap worker on its own pace; by the
    time a lookup happens here, a receipt is either already checked or it
    contributes nothing yet, never a guess either way.

    A receipt's reporter is not checked against addr, deliberately:
    verify_receipt_against_chain (see verify_pending_receipts) confirms
    or refutes the claim against the chain itself, not against who
    signed it, so a false claim fails that check regardless of who made
    it. Identity only ever mattered for admission control and dedup on
    the way in, never for what a receipt is worth once verified.
    """
    import market as market_mod
    from trade_storage import Trade, StepReceipt

    known_sessions = {t.session_id for t in
                      Trade.select(Trade.session_id).where(Trade.peer_lapse_addr == addr)}

    abandoned = 0
    completed_sessions = set()
    completed_lapse = 0
    last_completed_at = 0.0
    for r in market_mod.receipts_for_addr(addr):
        if r.verified is not True or r.session_id in known_sessions:
            continue
        if r.outcome == "missed":
            abandoned += 1
        elif r.outcome == "settled" and r.asset == "lapse":
            completed_sessions.add(r.session_id)
            completed_lapse += r.amount
            last_completed_at = max(last_completed_at, r.received_at)
    return abandoned, len(completed_sessions), completed_lapse, last_completed_at


def verify_pending_receipts(node, limit=20):
    """Chain-check up to `limit` not-yet-verified receipts, any address,
    and cache the verdict. The only place a receipt's `verified` column
    is ever set; see _network_tally for why every trust lookup only ever
    reads that cache rather than triggering this itself.

    Meant to be called once per swap-worker pass, the same cadence
    everything else about trades already runs on, not from a request
    path: this is the one place in the step-receipts design that does
    real chain I/O (a Horizon call for an XLM leg is possible here), and
    bounding it per pass is what keeps that cost predictable instead of
    proportional to how many receipts happen to be sitting unverified.
    Returns how many were resolved (true or false); an outage leaves the
    rest for the next pass rather than raising.
    """
    import swap_engine as swap_engine_mod
    from trade_storage import StepReceipt, ensure_tables as _ensure

    _ensure()
    engine = _lightweight_engine(node)
    resolved = 0
    pending = (StepReceipt.select()
              .where(StepReceipt.verified.is_null())
              .limit(limit))
    for r in pending:
        try:
            r.verified = swap_engine_mod.verify_receipt_against_chain(engine, r)
        except swap_engine_mod.Unreachable:
            continue
        r.save()
        resolved += 1
    return resolved


def _lightweight_engine(node):
    """Just enough of a swap_engine.Engine for verify_receipt_against_chain:
    the two read-only chain adapters, nothing that signs or sends. Built
    fresh per call rather than held anywhere, since it is as cheap as the
    node reference it wraps.
    """
    import swap_engine as swap_engine_mod

    class _Adapters:
        pass

    adapters = _Adapters()
    adapters.lapse = swap_engine_mod.LapseAdapter(node)
    # No real trading wallet needed: find_payment/confirmations, the only
    # calls verification makes, never touch the keyfile.
    adapters.xlm = swap_engine_mod.XLMAdapter("")
    return adapters


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


def mutual_scores(node, peer_addr):
    """(my_trust_of_peer, peer_trust_of_me), for swap.opening_mover.

    Both halves have to be computable without anything the peer says,
    since a self-reported score is exactly what a peer would inflate to
    win the "opens second" side of the coin flip. The completed-trade
    count between two specific addresses cannot be lied about this way:
    both sides watched the same legs settle on the same public chains, so
    this node's own PeerRecord for the peer already holds the number the
    peer's own worker would compute too. Standing is public on both
    sides too: anyone's address age and balance are chain facts, not
    something told to you.

    A defection is no longer only a private opinion, either: get_detail's
    node argument folds in any gossiped step receipt this node can
    independently verify against the chain (market.py's step-receipts
    section), for sessions between this pair that this node was not
    itself party to. That is a checked fact, not the peer's word, so
    trusting it here carries none of the risk a bare broadcast opinion
    would: a false claim fails verification regardless of who signed it.
    Bilateral history (this node's own PeerRecord for the peer) and
    network-verified history are simply added together before either
    half of this function reads them.
    """
    peer_age = address_age_blocks(node, peer_addr)
    peer_balance = node.view.state.get_balance(peer_addr)
    detail = get_detail(peer_addr, peer_age, peer_balance, node=node)
    my_trust_of_peer = detail["score"]

    my_age = address_age_blocks(node, node.addr)
    my_balance = node.view.state.get_balance(node.addr)
    peer_trust_of_me = score(
        detail["completed_count"], detail["completed_lapse"],
        detail["abandoned_count"], detail["last_completed_at"],
        my_age, my_balance)
    return my_trust_of_peer, peer_trust_of_me
