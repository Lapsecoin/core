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
back. The same goes for a trade this node was never a party to: there is
no signed "missed" claim to trust or distrust either, gossiped or
otherwise. An accepted (FillRequest, FillResponse) pair already signs
everything a schedule is built from, so this node reconstructs it and
checks it against both chains itself, with its own margin, fresh, every
time anything asks for that address's standing (see
_network_tally_by_counterparty and swap_engine.verify_trade_against_chain),
never accepting anyone's word for what happened.
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
# same way trust._network_tally_by_counterparty's own MAX_CHAIN_CHECKS_PER_LOOKUP
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
    is_delinquent), and independently reconstructs and verifies every
    accepted (FillRequest, FillResponse) pair naming addr for a session
    this node was not itself a party to (see _network_tally_by_counterparty
    and swap_engine.verify_trade_against_chain). Without node, standing is
    the bilateral figure built only from trades this node personally ran
    with addr, with no stall of its own counted as delinquent (current
    height unknown) and nothing network-sourced folded in; a caller that
    never passes node keeps behaving exactly as before. See
    _network_tally_by_counterparty for why a pair this node cannot
    currently check against the chain contributes nothing either way
    rather than being guessed at.
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
        (net_abandoned, net_by_counterparty, net_last_completed,
         net_last_abandoned_at, net_last_abandon_session) = \
            _network_tally_by_counterparty(node, addr, current_height)
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
        # A self-query's own local_tally never has anything here (see
        # _network_tally_by_counterparty's own docstring for why), so
        # whichever of the two is more recent is never a real conflict
        # between two independent verdicts, only "which source actually
        # has data for this address".
        if net_last_abandoned_at >= last_abandoned_at:
            last_abandoned_at = net_last_abandoned_at
            last_abandon_session = net_last_abandon_session

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


# How many accepted (FillRequest, FillResponse) pairs
# _network_tally_by_counterparty will chain-check in one call. A real
# counterparty has a handful of trades naming it; this exists for the
# address that does not. market.py's own per-signer accept caps
# (MAX_ACCEPTED_RESPONSES_PER_MAKER/_TOTAL) mean spamming one victim
# address still costs an attacker their own accept slots, but nothing
# stops them paying that cost from many cheaply-generated addresses, all
# naming the same victim as taker. Without this cap, the first trust
# lookup for that address (a market page render, easily triggered by
# anyone, not just the victim) would make one chain call per such pair,
# synchronously, in that one request. This is the difference between a
# handful of lookups and hundreds: bounded per call, not per address.
MAX_CHAIN_CHECKS_PER_LOOKUP = 20


def _network_tally_by_counterparty(node, addr, current_height):
    """(abandoned_count, {counterparty_addr: (ticks, count)}, last_completed_at,
    last_abandoned_at, last_abandon_session), independently reconstructed
    from every accepted (FillRequest, FillResponse) pair on file naming
    addr, excluding any session this node already has a local Trade row
    for (see local_tally: those are already counted above, and counting
    them again from a pair this node itself was party to would double
    them).

    Nothing here is taken on anyone's word. Each pair is walked fresh
    through swap_engine.verify_trade_against_chain, which reconstructs
    the whole schedule from the pair's own signed fields and checks every
    step against both chains with THIS node's own margin
    (ABANDON_AFTER_BLOCKS) on THIS node's own current view of them - the
    entire replacement for what a gossiped, signed "missed" claim used to
    assert about itself. Bounded to MAX_CHAIN_CHECKS_PER_LOOKUP pairs per
    call for the same reason that bound used to apply to receipts: a real
    counterparty has a handful of trades naming it, and this keeps a
    spammed address's lookup cost bounded per call rather than scanning
    every pair on file. There is no cache of past verdicts to consult
    first, on purpose: this is cheap enough to just always redo (see
    verify_trade_against_chain's own docstring), and a stored verdict is
    exactly the "trust a claim until told otherwise" shape this whole
    redesign exists to avoid.

    at_fault_addr (verify_trade_against_chain's own third return value)
    is checked against addr before anything is added to `abandoned`,
    which is the fix for a real hole the old signed-receipt design had:
    a receipt's addr_a/addr_b named both parties of a session with no
    fault distinction of its own, so looking up the VICTIM's own standing
    could count their counterparty's defection against them. Here, a
    pair only ever counts against whichever specific address
    verify_trade_against_chain names as owing the outstanding leg, never
    the other one.

    last_abandoned_at/last_abandon_session exist so a node asking about
    its OWN address (see market_routes.trades' my_standing, and the
    Trades page's own alert) can name the specific session it currently
    stands delinquent on, not just report a bare count: local_tally's
    own fields of the same name only ever cover trades this node ran
    itself, and a self-query's Trade rows never have peer_lapse_addr
    equal to this node's own address, so for a self-query essentially all
    of the real signal - including which session it was - comes from
    here. Timestamped with the pair's own resp.received_at (when this
    node's own copy of the accept was gossiped) rather than "now": no
    chain fact records the moment a step actually went overdue, and
    unlike a wall-clock "just discovered", this stays the same answer
    every time the same pair is re-verified.

    Grouped by counterparty rather than pooled into one flat total,
    because that grouping is the entire input history_component's
    diversity-weighting needs.
    """
    import crypto
    import market as market_mod
    import swap as swap_mod
    import swap_engine as swap_engine_mod
    from trade_storage import Trade

    known_sessions = {t.session_id for t in
                      Trade.select(Trade.session_id).where(Trade.peer_lapse_addr == addr)}

    engine = _lightweight_engine(node)
    abandoned = 0
    last_abandoned_at = 0.0
    last_abandon_session = ""
    by_counterparty = {}
    last_completed_at = 0.0
    checked = 0
    for req, resp in market_mod.accepted_fills_for_addr(addr):
        if req.session_id in known_sessions:
            continue
        if checked >= MAX_CHAIN_CHECKS_PER_LOOKUP:
            break
        checked += 1

        try:
            maker_addr = crypto.public_key_to_address(bytes.fromhex(resp.maker_pubkey))
        except (ValueError, TypeError):
            continue
        counterparty = req.taker_lapse_addr if addr == maker_addr else maker_addr

        try:
            settled_steps, _total, at_fault_addr = \
                swap_engine_mod.verify_trade_against_chain(engine, req, resp)
        except swap_engine_mod.Unreachable:
            continue

        if settled_steps > 0:
            schedule = swap_mod.build_schedule(
                resp.lapse_total, resp.xlm_total, resp.increment_count)
            ticks = sum(lapse_amount for lapse_amount, _xlm in schedule[:settled_steps])
            peer_ticks, peer_count = by_counterparty.get(counterparty, (0, 0))
            by_counterparty[counterparty] = (peer_ticks + ticks, peer_count + 1)
            last_completed_at = max(last_completed_at, resp.received_at)

        if at_fault_addr == addr:
            abandoned += 1
            if resp.received_at >= last_abandoned_at:
                last_abandoned_at = resp.received_at
                last_abandon_session = req.session_id

    return (abandoned, by_counterparty, last_completed_at,
           last_abandoned_at, last_abandon_session)


def _lightweight_engine(node):
    """Just enough of a swap_engine.Engine for verify_trade_against_chain:
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
    per-address lookup" reasoning as trust._network_tally_by_counterparty's
    own MAX_CHAIN_CHECKS_PER_LOOKUP.
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
    node argument reconstructs and independently verifies every accepted
    (FillRequest, FillResponse) pair against the chain (see
    _network_tally_by_counterparty and swap_engine.verify_trade_against_
    chain), for sessions between this pair that this node was not itself
    party to. That is a checked fact, not the peer's word: nobody's claim
    is ever taken as true, only what the chain itself shows, re-derived
    fresh on every call rather than cached and trusted forever.

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
