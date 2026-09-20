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

# Below this, a balance does not count as stake at all - not "a little",
# zero. A linear ramp from zero looks harmless but is exactly backwards
# for Sybil resistance: it rewards spreading a fixed budget across many
# addresses a little each, since sqrt/linear curves have their steepest
# marginal value near zero. A hard floor makes splitting strictly worse
# than concentrating, the same shape AGE_FLOOR_BLOCKS already gives age.
# Simulated against a spray of thin-funded addresses before picking this
# (see the design notes this was built from); tune alongside
# STAKE_FULL_TICKS if LAPSE's expected value changes materially.
STAKE_MIN_TICKS = 5 * 100_000_000

# How many blocks since an address's balance last grew by a non-dust
# amount before that balance counts as stake. Exists because a snapshot
# balance check, however high its floor, can always be satisfied by
# moving ONE pool of capital through a queue of addresses one at a time
# right before each is used - the floor stops spreading thin, nothing
# stops reusing thin in sequence instead. This is what actually closes
# that: an address topped up seconds ago scores exactly like an empty
# one regardless of the amount, so fielding N simultaneously-usable
# identities needs N x the capital genuinely parked for this long, not
# one pool cycled through N addresses for free. Same span as
# AGE_FLOOR_BLOCKS on purpose - it is measuring the same kind of claim
# ("this took genuine time to accumulate"), just anchored to the most
# recent significant inbound transfer instead of to first appearance.
SEASONING_FLOOR_BLOCKS = AGE_FLOOR_BLOCKS

# The smallest inbound transfer that counts as "topping up" for
# SEASONING_FLOOR_BLOCKS purposes. Far below STAKE_MIN_TICKS on purpose:
# this only exists to stop a griefer resetting a stranger's seasoning
# clock by sending them dust, not to gate real funding events.
SEASONING_SIGNIFICANCE_TICKS = 100_000_000 // 100   # 0.01 LAPSE

# How many of an address's most recent transactions
# blocks_since_last_significant_topup will look through before giving up
# and treating it as fully seasoned. Bounds the cost of the lookup the
# same way trust._verify_addr_receipts' own MAX_CHAIN_CHECKS_PER_LOOKUP
# bounds that one: a real trader's recent history is short, and an
# address with none, or none significant, in this many is a genuine
# "nothing recent", not a scan this call should keep paying to confirm.
SEASONING_SCAN_LIMIT = 50

# Ceiling on the history component, applied after decay. Diversity-
# weighting (see history_component) makes faking N independent-looking
# relationships cost the same as building N real ones, but says nothing
# about magnitude: two self-owned addresses cycling a large, genuinely-
# held balance back and forth can still rack up an arbitrarily large
# raw number, cheaply, since the LAPSE leg of a trade carries no
# mandatory fee (tx.py: fee is sender-chosen, swap legs use fee=0) and
# the same balance round-trips intact every time rather than being
# spent. exposure_cap_stroops already refuses to let a raw score push a
# step past MAX_TRUST_MULTIPLIER regardless of magnitude; this applies
# the identical discipline to the score itself, which matters
# separately because swap.opening_mover compares two raw scores directly
# with no ceiling of its own downstream of it.
HISTORY_SATURATION_CEILING = 50.0

# How much of the total score pure stake (age + seasoned balance, no
# proven delivery at all) can supply on its own. Deliberately well below
# what real delivered history can reach (a trader with a genuine, modest
# track record should always outscore a merely well-capitalized
# stranger), but large enough that two established, mutually-unfamiliar
# parties get real, distinguishable numbers on first contact instead of
# both hitting exactly zero - which is the actual defect this whole
# formula exists to fix (see score()'s own docstring).
STANDING_WEIGHT = 0.3

TICKS_PER_LAPSE = 100_000_000


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def history_component(per_counterparty, last_completed_at, now=None):
    """Standing earned by trades that actually settled, weighted by how
    many DISTINCT counterparties they were settled with.

    per_counterparty: an iterable of (ticks, count) pairs, one entry per
    distinct address this trade history is drawn from - this node's own
    bilateral experience counts as exactly one such entry (see
    get_detail), and each network-verified counterparty another (see
    _network_tally_by_counterparty). There is deliberately no separate
    "local" vs "network" formula any more: both are just entries in the
    same list, weighted identically, because the mechanism that matters
    (concavity rewarding diversity) does not care which is which.

    Square root in volume and logarithmic in count *within* one
    counterparty, so neither one large trade nor a burst of tiny ones
    with the SAME partner buys standing out of proportion to it. Summed,
    not pooled, ACROSS counterparties: sqrt(a) + sqrt(b) > sqrt(a+b) for
    positive a, b, so spreading the same total volume/count across more
    distinct partners is worth strictly more than concentrating it in
    one, with no separately-tuned "diversity bonus" needed on top - the
    same concavity that dampens one relationship's own inflation is what
    rewards spreading across several. (An earlier version of this
    function pooled every counterparty's ticks/count into one sqrt/log1p
    call, which a two-address wash-trading ring could inflate for free:
    the LapseCoin leg of a trade carries no mandatory fee, so the same
    balance can round-trip between two self-owned addresses indefinitely.
    Diversity-weighting does not make that impossible - nothing on-chain
    can prove two addresses are controlled by different people - but it
    does make faking N independent-looking relationships cost the same
    age+balance+seasoning setup as N real ones, closing the specific
    shortcut of reusing the same two keys forever.)

    Decays with time since the most recent completed trade, then
    saturates at HISTORY_SATURATION_CEILING: without a ceiling here, a
    wash-trading ring willing to cycle a large, genuinely-held balance
    many times can still post an arbitrarily large raw number even after
    diversity-weighting, and unlike the exposure cap (which already
    refuses to let a raw score push a step past MAX_TRUST_MULTIPLIER
    regardless of magnitude), swap.opening_mover compares two raw scores
    directly with nothing downstream to cap it.
    """
    pairs = [(ticks, count) for ticks, count in per_counterparty if count > 0]
    if not pairs:
        return 0.0
    now = time.time() if now is None else now
    total = sum(math.sqrt(max(ticks, 0) / TICKS_PER_LAPSE) * math.log1p(count)
               for ticks, count in pairs)
    age = max(now - (last_completed_at or 0), 0)
    decayed = total * (0.5 ** (age / DECAY_HALFLIFE_SECONDS))
    return min(decayed, HISTORY_SATURATION_CEILING)


def stake_component(address_age_blocks, balance_ticks, blocks_since_last_topup=0):
    """What the address would cost to replace, as a multiplier in [0, 1].

    Age and balance are multiplied rather than added, so both are
    required: a freshly minted address holding a fortune scores zero,
    and so does an ancient empty one. Either alone is cheap to
    manufacture, which is precisely the case this exists to price.

    balance_ticks only counts once BOTH of its own gates pass (see
    _balance_factor): it is above STAKE_MIN_TICKS, and it has sat there
    for at least SEASONING_FLOOR_BLOCKS. The floor alone stops spreading
    a fixed budget thin across many simultaneous addresses; seasoning is
    what stops the cheaper version of the same attack, moving ONE pool
    of capital through a queue of already-aged addresses one at a time,
    which a floor-only check cannot tell apart from a genuinely-held
    balance since both look identical in an instantaneous snapshot.
    """
    if address_age_blocks < AGE_FLOOR_BLOCKS:
        return 0.0
    span = max(AGE_FULL_BLOCKS - AGE_FLOOR_BLOCKS, 1)
    age = min((address_age_blocks - AGE_FLOOR_BLOCKS) / span, 1.0)
    return age * _balance_factor(balance_ticks, blocks_since_last_topup)


def _balance_factor(balance_ticks, blocks_since_last_topup):
    if blocks_since_last_topup < SEASONING_FLOOR_BLOCKS:
        return 0.0
    if balance_ticks < STAKE_MIN_TICKS:
        return 0.0
    span = max(STAKE_FULL_TICKS - STAKE_MIN_TICKS, 1)
    return min((balance_ticks - STAKE_MIN_TICKS) / span, 1.0)


def score(per_counterparty, abandoned_count, last_completed_at,
          address_age_blocks=0, balance_ticks=0, blocks_since_last_topup=0,
          now=None):
    """A peer's standing. Zero for anyone who has ever walked away.

    Two terms, not one: a standing floor from stake alone
    (STANDING_WEIGHT * stake), and delivered history multiplied by stake
    exactly as before (history's own Sybil defence: a record built on a
    throwaway address is worth nothing, however long it is, because
    stake is what makes it non-transferable to a fresh identity).

    The old version was history_component(...) * stake_component(...)
    with nothing else, which meant ANY address with zero completed
    trades - a first contact, which is most of them - hit exactly 0.0
    regardless of how established it otherwise was, discarding real
    age/balance information at precisely the moment it mattered most
    for deciding who should have to move first (swap.opening_mover).
    The standing term restores that: two established-but-unfamiliar
    strangers now get real, distinguishable numbers instead of both
    tying at zero, without letting pure capital ever outscore genuine,
    proven delivery (STANDING_WEIGHT keeps the floor well under what
    history can reach).
    """
    if abandoned_count > 0:
        return 0.0
    stake = stake_component(address_age_blocks, balance_ticks, blocks_since_last_topup)
    history = history_component(per_counterparty, last_completed_at, now=now)
    return STANDING_WEIGHT * stake + history * stake


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


def get_detail(addr, address_age_blocks=0, balance_ticks=0,
               blocks_since_last_topup=0, now=None, node=None):
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
    before. See _network_tally_by_counterparty for what "verified" means
    here and why a receipt this node cannot currently check contributes
    nothing either way rather than being guessed at.
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

    # This node's own bilateral experience with addr is exactly one
    # counterparty entry in the diversity-weighted sum history_component
    # runs over (see that function): it is not diversity-derated the way
    # a network-sourced entry conceptually could be, because this is
    # first-hand, personally-verified experience, not a claim relayed by
    # someone else. Every network-verified counterparty below is another
    # entry in the same list.
    per_counterparty = []
    if completed_count > 0:
        per_counterparty.append((completed_lapse, completed_count))

    # Broken out from the merged totals below so a caller (see
    # market_take.html's Track record card) can say plainly which part
    # of this is this node's own experience and which part is this node
    # independently verifying what other traders have gossiped, rather
    # than presenting one blended number as if it all came from the same
    # place.
    net_abandoned = net_count = net_lapse = 0
    if node is not None:
        _verify_addr_receipts(node, addr, current_height)
        net_abandoned, net_by_counterparty, net_last_completed = \
            _network_tally_by_counterparty(addr)
        for peer_lapse, peer_count in net_by_counterparty.values():
            per_counterparty.append((peer_lapse, peer_count))
        net_count = sum(c for _l, c in net_by_counterparty.values())
        net_lapse = sum(l for l, _c in net_by_counterparty.values())
        if net_abandoned or net_count:
            known = True
        abandoned_count += net_abandoned
        completed_count += net_count
        completed_lapse += net_lapse
        last_completed_at = max(last_completed_at, net_last_completed)

    history = history_component(per_counterparty, last_completed_at, now=now)
    stake = stake_component(address_age_blocks, balance_ticks, blocks_since_last_topup)
    return {
        "score": score(per_counterparty, abandoned_count, last_completed_at,
                       address_age_blocks, balance_ticks,
                       blocks_since_last_topup, now),
        "completed_count": completed_count,
        "completed_lapse": completed_lapse,
        "network_completed_count": net_count,
        "network_abandoned_count": net_abandoned,
        "distinct_counterparties": len(per_counterparty),
        "abandoned_count": abandoned_count,
        "last_completed_at": last_completed_at,
        "last_abandoned_at": last_abandoned_at,
        "last_abandon_session": last_abandon_session,
        "history": history,
        "stake": stake,
        "known": known,
        # Exposed so mutual_scores can re-run the exact same trade
        # history through this node's OWN stake instead of the
        # subject's, to approximate the subject's opinion of this node
        # without needing to ask them (see mutual_scores).
        "per_counterparty": per_counterparty,
    }


def _network_tally_by_counterparty(addr):
    """(abandoned_count, {counterparty_addr: (ticks, count)}, last_completed_at)
    from already-verified step receipts naming addr, excluding any
    session this node already has a local Trade row for (see
    local_tally: those are already counted above, and counting them
    again from a receipt this node itself likely emitted would double
    them).

    Grouped by counterparty rather than pooled into one flat total,
    because that grouping is the entire input history_component's
    diversity-weighting needs: a receipt names both parties of the
    session it describes, so "who else was in this session" is read
    straight off addr_a/addr_b, not inferred.

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
    from trade_storage import Trade

    known_sessions = {t.session_id for t in
                      Trade.select(Trade.session_id).where(Trade.peer_lapse_addr == addr)}

    abandoned = 0
    # session_id -> [counterparty, ticks, last_completed_at]: accumulated
    # per session first, since a single trade can carry several
    # "settled" receipts (one per step's LAPSE leg), all naming the same
    # counterparty; only once collapsed to one entry per session does
    # counting distinct sessions per counterparty mean anything.
    sessions = {}
    for r in market_mod.receipts_for_addr(addr):
        if r.verified is not True or r.session_id in known_sessions:
            continue
        if r.outcome == "missed":
            abandoned += 1
            continue
        if r.outcome != "settled" or r.asset != "lapse":
            continue
        counterparty = r.addr_b if r.addr_a == addr else r.addr_a
        entry = sessions.setdefault(r.session_id, [counterparty, 0, 0.0])
        entry[1] += r.amount
        entry[2] = max(entry[2], r.received_at)

    by_counterparty = {}
    last_completed_at = 0.0
    for counterparty, ticks, received_at in sessions.values():
        peer_ticks, peer_count = by_counterparty.get(counterparty, (0, 0))
        by_counterparty[counterparty] = (peer_ticks + ticks, peer_count + 1)
        last_completed_at = max(last_completed_at, received_at)
    return abandoned, by_counterparty, last_completed_at



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

    Cheap to bulk-produce: AddrIndex indexes an address the moment it
    appears as sender OR any recipient of ANY transaction (storage.py's
    _index_block), so a single multi-output transaction, at whatever fee
    the sender chooses to pay (possibly zero), can backdate thousands of
    addresses' first-seen height at once. That is exactly why age alone
    is never trusted on its own downstream of this (see stake_component):
    it bounds how fast a *single* identity can be escalated to "old
    enough", nothing about how many can be warehoused in parallel for
    later. blocks_since_last_significant_topup is the signal that
    actually costs per-identity capital, not merely patience.
    """
    heights = node.storage.get_tx_heights_for_addr(addr)
    if not heights:
        return 0
    first_seen = min(height for height, _tx_hash in heights)
    return max(node.view.chain[-1]["height"] - first_seen, 0)


def blocks_since_last_significant_topup(node, addr):
    """How many blocks since addr last received a non-dust transfer -
    the seasoning signal stake_component gates a balance on. See
    SEASONING_FLOOR_BLOCKS for why a live balance snapshot needs this at
    all: without it, a balance floor can always be satisfied by moving
    one pool of capital through a queue of pre-aged addresses one at a
    time, right before each is used.

    float("inf") ("never") covers two different, equally fine cases: an
    address with no inbound transfers in its recent history at all, and
    one whose entire balance came from mining a block reward. The
    latter is never seasoned by *this* check because a block reward is
    credited via state.credit()/apply_reward_distribution directly (see
    chainstate._apply_builder_reward), never through a transaction, so
    it never appears in AddrIndex for this function to find - but
    winning a block is real, sequential, unparallelizable work, so
    treating "nothing recent to find" as fully seasoned is correct
    there for a different reason than for a genuinely untouched address:
    earning it already took longer than any seasoning window could ask.

    Bounded to the SEASONING_SCAN_LIMIT most recent transactions
    touching addr, newest first (storage.py already orders this way),
    stopping at the first qualifying transfer found: a real trader's
    recent history is short, and this is the same "bound the cost of a
    per-address lookup" reasoning as trust._verify_addr_receipts'
    MAX_CHAIN_CHECKS_PER_LOOKUP.
    """
    import tx as tx_mod

    heights = node.storage.get_tx_heights_for_addr(addr)
    chain = node.view.chain
    tip = chain[-1]["height"]
    for height, tx_hash in heights[:SEASONING_SCAN_LIMIT]:
        if not 0 <= height < len(chain):
            continue
        for candidate in chain[height]["transactions"]:
            if tx_mod.tx_hash(candidate) != tx_hash:
                continue
            paid = sum(o["amount"] for o in candidate.get("outputs", [])
                      if o.get("to") == addr)
            if paid >= SEASONING_SIGNIFICANCE_TICKS:
                return tip - height
    return float("inf")


def mutual_scores(node, peer_addr):
    """(my_trust_of_peer, peer_trust_of_me), for swap.opening_mover.

    Both halves have to be computable without anything the peer says,
    since a self-reported score is exactly what a peer would inflate to
    win the "opens second" side of the coin flip. The completed-trade
    count between two specific addresses cannot be lied about this way:
    both sides watched the same legs settle on the same public chains, so
    this node's own Trade rows against the peer (see local_tally) already
    hold the number the peer's own node would derive too. Standing is
    public on both sides too: anyone's address age, balance and
    seasoning are chain facts, not something told to you.

    A defection is no longer only a private opinion, either: get_detail's
    node argument folds in any gossiped step receipt this node can
    independently verify against the chain (market.py's step-receipts
    section), for sessions between this pair that this node was not
    itself party to. That is a checked fact, not the peer's word, so
    trusting it here carries none of the risk a bare broadcast opinion
    would: a false claim fails verification regardless of who signed it,
    and, unlike this node's own Trade rows, a "missed" claim is
    re-checked on demand rather than trusted forever (see
    _verify_addr_receipts).

    peer_trust_of_me reuses get_detail(peer_addr, ...)'s own
    per_counterparty breakdown (the same trades, since both sides
    watched them settle) run back through this node's OWN stake instead
    of the peer's: the same approximation the pre-diversity-weighting
    version of this function already made by reusing the peer's
    completed_count/completed_lapse totals, just now correctly shaped
    for the per-counterparty formula.
    """
    peer_age = address_age_blocks(node, peer_addr)
    peer_balance = node.view.state.get_balance(peer_addr)
    peer_seasoning = blocks_since_last_significant_topup(node, peer_addr)
    detail = get_detail(peer_addr, peer_age, peer_balance, peer_seasoning, node=node)
    my_trust_of_peer = detail["score"]

    my_age = address_age_blocks(node, node.addr)
    my_balance = node.view.state.get_balance(node.addr)
    my_seasoning = blocks_since_last_significant_topup(node, node.addr)
    peer_trust_of_me = score(
        detail["per_counterparty"],
        detail["abandoned_count"], detail["last_completed_at"],
        my_age, my_balance, my_seasoning)
    return my_trust_of_peer, peer_trust_of_me
