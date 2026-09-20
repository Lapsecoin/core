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

Slashing, and why it is never permanent
----------------------------------------
One unreciprocated payment zeroes the score outright, rather than
decrementing it: a partial penalty would price honesty and dishonesty on
the same curve, and they are not the same thing.

What it does not do is fire on a mere timeout, or stay fired once the
reason for it stops being true. A stalled trade is not a slash on its
own (see swap_engine.is_delinquent for the margin required first), and
this node is never the last word on whether one happened: standing here
is computed fresh from this node's own Trade and Increment rows every
time it is asked, never incremented and stored. There is no
TRADE_ABANDONED status any more, and nothing to reverse if a late
payment arrives (a node that was offline for entirely mundane reasons,
paying what it always owed, late): a trade simply stays "stalled" for as
long as a leg is missing, is_delinquent recomputes whether that still
counts against the peer from the current chain height every single
call, and the moment the missing leg settles it stops counting on the
very next call, nothing was ever written down that needed to be written
back. The same goes for a gossiped step receipt claiming a payment never
arrived: that claim is re-checked against the chain the next time
anything asks for this address's standing (see _network_tally), not
accepted once and then trusted forever, because whether it never arrived
is true only until the moment it does.
"""

import logging
import math
import time

from trade_storage import ensure_tables

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
# Local history: derived from this node's own Trade/Increment rows, never
# stored
# ---------------------------------------------------------------------------
#
# There used to be a PeerRecord table here, incremented once per completed
# or abandoned trade and read back as the answer. Then a TRADE_ABANDONED
# status on Trade itself, which was the same problem one layer down: a
# status set once and needing a dedicated pass (swap_engine.recheck_abandoned,
# since removed) to notice when it stopped being true. Both were an
# incremented or written verdict with no way to hear about a trade that
# later resolved differently. A Trade row's own `status` plus its
# Increment rows are already the one place that outcome lives, and
# swap_engine.is_delinquent recomputes whether a stall currently counts
# against a peer from exactly those rows and the current chain height,
# fresh, every call: there is nothing left over to fall out of sync with.

def local_tally(addr, current_height=None):
    """This node's own completed/delinquent counts and volumes against
    addr, computed fresh from Trade and Increment rows every call rather
    than read back from a running total. Cheap: a node's own trade
    history with one counterparty is small by construction (see
    MAX_ORDERS_PER_MAKER and friends bounding the market generally), so
    this is a couple of indexed queries plus, at most, one row scan per
    still-stalled trade with this peer, not a scan of everything.

    current_height is the current LapseCoin height, needed to tell
    whether a stalled trade has been delinquent long enough to count
    (see swap_engine.is_delinquent). Without it (the default) delinquency
    simply cannot be computed, and this reports none, the same way an
    unreachable chain never zeroes anyone's standing elsewhere in this
    module: not knowing is not the same as knowing it is fine.
    """
    import swap_engine as swap_engine_mod
    from trade_storage import Trade, TRADE_COMPLETED, TRADE_STALLED

    completed = list(Trade.select()
                     .where(Trade.peer_lapse_addr == addr,
                            Trade.status == TRADE_COMPLETED))
    delinquent = []
    if current_height is not None:
        stalled = (Trade.select()
                  .where(Trade.peer_lapse_addr == addr,
                         Trade.status == TRADE_STALLED)
                  .order_by(Trade.updated_at.desc()))
        delinquent = [t for t in stalled
                     if swap_engine_mod.is_delinquent(t, current_height)]
    return {
        "completed_count": len(completed),
        "completed_lapse": sum(t.lapse_total for t in completed),
        "last_completed_at": max((t.updated_at for t in completed), default=0.0),
        "abandoned_count": len(delinquent),
        "last_abandoned_at": delinquent[0].updated_at if delinquent else 0.0,
        "last_abandon_session": delinquent[0].session_id if delinquent else "",
    }


def get_detail(addr, address_age_blocks=0, balance_ticks=0, now=None, node=None):
    """Standing plus the figures behind it.

    Broken out because a score with no stated reason is a verdict, and a
    user deciding whether to trade with somebody deserves to see what the
    number is made of rather than be handed it.

    node is optional and, when given, does two things a bare address
    cannot: supplies the current LapseCoin height that local_tally needs
    to judge a stalled trade of this node's own (see swap_engine.
    is_delinquent), and folds in this node's own verified reading of
    gossiped step receipts (see market.py's step-receipts section) naming
    addr, for sessions this node was not itself a party to. Without node,
    standing is the bilateral figure built only from trades this node
    personally ran with addr, with no stall of its own counted as
    delinquent (current height unknown) and nothing network-sourced
    folded in; a caller that never passes node keeps behaving exactly as
    before. See _network_tally for what "verified" means here and why a
    receipt this node cannot currently check contributes nothing either
    way rather than being guessed at.
    """
    ensure_tables()
    current_height = node.view.height if node is not None else None
    local = local_tally(addr, current_height)
    completed_count = local["completed_count"]
    completed_lapse = local["completed_lapse"]
    abandoned_count = local["abandoned_count"]
    last_completed_at = local["last_completed_at"]
    last_abandoned_at = local["last_abandoned_at"]
    last_abandon_session = local["last_abandon_session"]
    known = bool(completed_count or abandoned_count)

    # Broken out from the merged totals below so a caller (see
    # market_take.html's Track record card) can say plainly which part
    # of this is this node's own experience and which part is this node
    # independently verifying what other traders have gossiped, rather
    # than presenting one blended number as if it all came from the same
    # place.
    net_abandoned = net_count = net_lapse = 0
    if node is not None:
        _verify_addr_receipts(node, addr, current_height)
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
    session this node already has a local Trade row for (see
    local_tally: those are already counted above, and counting them
    again from a receipt this node itself likely emitted would double
    them).

    DB-only: reads whatever verified cache _verify_addr_receipts (called
    first by get_detail, whenever it has a node to verify with) already
    populated for this address. A receipt this node has never had reason
    to check yet, or one an outage left unresolved, contributes nothing
    either way rather than being guessed at.

    A receipt's reporter is not checked against addr, deliberately:
    verify_receipt_against_chain (see _verify_addr_receipts) confirms or
    refutes the claim against the chain itself, not against who signed
    it, so a false claim fails that check regardless of who made it.
    Identity only ever mattered for admission control and dedup on the
    way in, never for what a receipt is worth once verified.
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



# How many receipts _verify_addr_receipts will chain-check in one call.
# A real counterparty has a handful of receipts naming it; this exists
# for the address that does not. verify_receipt's reporter check means
# spamming one victim address still costs an attacker one of their own
# MAX_RECEIPTS_PER_REPORTER slots per receipt, but nothing stops them
# from paying that cost from many cheaply-generated addresses, up to
# MAX_RECEIPTS_TOTAL network-wide, all naming the same victim. Without
# this cap, the first trust lookup for that address (a market page
# render, easily triggered by anyone, not just the victim) would make
# one chain call, possibly a Horizon one, per spammed receipt,
# synchronously, in that one request. This is the difference between a
# handful of lookups and hundreds: bounded per call, not per address.
MAX_CHAIN_CHECKS_PER_LOOKUP = 20


def _verify_addr_receipts(node, addr, current_height):
    """Chain-check up to MAX_CHAIN_CHECKS_PER_LOOKUP of this address's
    on-file receipts that still need it, right here, on the caller's own
    thread, at the moment something actually asks for addr's standing.
    Returns how many were (re)checked.

    A real address, with a real handful of counterparties, never
    notices the cap: everything it has gets checked in one call, same as
    before. An address somebody has spammed with receipts (see
    MAX_CHAIN_CHECKS_PER_LOOKUP for how, and why the cap exists at all)
    instead gets a bounded number of chain calls per lookup and no more;
    receipts left over this call are exactly as unverified as they were
    before it, for the next lookup to make progress on, not a scan this
    request pays for in full. A background sweep would grind through a
    spam pile faster, but only by bringing back the always-on cost this
    design exists to avoid; this trades that for the pile taking longer
    to resolve, on whichever caller last happened to ask about it.

    "settled" and "missed" are not the same kind of claim. A settled
    claim is monotonic: once verified true or caught false, it never
    needs re-checking (barring a reorg, and an active trade of this
    node's own already re-checks its own legs continuously; a third
    party's settled receipt is not re-chased here, an accepted, narrower
    gap). A missed claim is not monotonic: "the payment has not arrived"
    is true only until the moment it does, and a node that was genuinely
    offline rather than dishonest can make it false at any time by
    finally sending what it owed and gossiping a settled receipt for the
    same step, so a claim currently believed missing is re-verified
    against the live chain every time it is asked about, at most once
    per height (see StepReceipt.verified_at_height) so reloading the
    same page twice inside one block does no repeat chain I/O.

    There is no scheduled sweep doing this in the background any more:
    the previous design polled a fixed batch of receipts, any address,
    every worker pass, whether or not anyone was looking. A receipt
    nobody's trust ever depends on now costs nothing at all, which is
    the entire point of computing this lazily instead of eagerly.
    """
    import market as market_mod
    import swap_engine as swap_engine_mod
    from trade_storage import ensure_tables as _ensure

    _ensure()
    engine = _lightweight_engine(node)
    checked = 0
    for r in market_mod.receipts_for_addr(addr):
        if checked >= MAX_CHAIN_CHECKS_PER_LOOKUP:
            break
        # False is permanent either way: a false "settled" claim can
        # never become true (its tx_hash is fixed), and a "missed" claim
        # caught false means the payment was already found, which cannot
        # un-happen (barring a reorg, the same accepted, narrower gap
        # noted above). True is permanent for "settled" but not for
        # "missed", which is re-checked at most once per height.
        if r.verified is False:
            continue
        if r.verified is True and (r.outcome != "missed"
                                   or r.verified_at_height == current_height):
            continue
        try:
            r.verified = swap_engine_mod.verify_receipt_against_chain(engine, r)
        except swap_engine_mod.Unreachable:
            continue
        r.verified_at_height = current_height
        r.save()
        checked += 1
    return checked


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
    this node's own Trade rows against the peer (see local_tally) already
    hold the number the peer's own node would derive too. Standing is
    public on both sides too: anyone's address age and balance are chain
    facts, not something told to you.

    A defection is no longer only a private opinion, either: get_detail's
    node argument folds in any gossiped step receipt this node can
    independently verify against the chain (market.py's step-receipts
    section), for sessions between this pair that this node was not
    itself party to. That is a checked fact, not the peer's word, so
    trusting it here carries none of the risk a bare broadcast opinion
    would: a false claim fails verification regardless of who signed it,
    and, unlike this node's own Trade rows, a "missed" claim is
    re-checked on demand rather than trusted forever (see
    _verify_addr_receipts). Bilateral history and network-verified
    history are simply added together before either half of this
    function reads them.
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
