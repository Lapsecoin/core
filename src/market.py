"""The order book: signed offers, gossiped rather than written on chain.

An order is a statement that somebody will trade at a price, signed with
the same FALCON-512 key that controls the LAPSE address it names. It
carries no funds and changes no ledger, so it needs no consensus and
costs no block space; it is an advertisement that happens to be
unforgeable.

Every node keeps its own copy of the book, built from what reaches it, so
there is no server to ask and nothing to take down. Two nodes may hold
slightly different books and both be correct, in the same way two nodes
can hold different mempools.

What a signature does and does not prove
----------------------------------------
It proves the maker controls the address and meant these exact terms.
It does not prove they hold the funds, and it cannot: the balance can
change a second later. So an order is a claim to be checked, not a
promise, and the check is on the chain at the moment of trading rather
than here. Treating a signature as proof of solvency is how a book fills
with offers nobody can honour.

Remaining size comes from the chain
-----------------------------------
A partially filled order is not re-broadcast with a smaller number. Every
step of every trade is tagged with the order's session on both chains, so
any node can work out how much has actually been delivered by reading
public data. That removes a whole class of disagreement: there is no
"current" version of an order to be out of date about.
"""

import logging
import time
import uuid

import crypto
import swap as swap_mod
import trust as trust_mod
from crypto import canonical_json
from params import TICKS_PER_LAPSE
from trade_storage import (
    Claim, FillRequest, FillResponse, Order, Trade, Increment,
    ensure_tables, LEG_SETTLED, TRADE_COMPLETED,
)

log = logging.getLogger("ec.market")

# Fields covered by the maker's signature. Listed explicitly rather than
# taken from whatever the dict happens to hold, so an order carrying an
# extra key cannot be signed as one thing and read as another.
SIGNED_FIELDS = (
    "order_id", "maker_lapse_addr", "maker_xlm_addr", "direction",
    "lapse_total", "price_stroops_per_lapse", "min_fill", "max_fill",
    "expiry_block", "pubkey",
)

# An order this node will not keep, however well signed. A book is a
# public surface that anyone may push rows into, so it needs its own
# bounds rather than trusting that a signature implies good faith.
MAX_ORDERS_PER_MAKER = 20
MAX_EXPIRY_HORIZON_BLOCKS = 100_000      # ~4.5 months at two minutes

# The book's own ceiling, independent of any one maker. A maker is free:
# generate a keypair, post twenty orders, generate another. The per-maker
# cap alone bounds nothing against that, only how much any single address
# can claim; this bounds the book itself regardless of how many addresses
# an attacker is willing to mint. Generous relative to any real market
# this feature is likely to see for a long time, so it costs nothing
# against genuine usage and only ever fires against sustained abuse that
# outpaces expiry and pruning.
MAX_ORDERS_TOTAL = 50_000


class OrderRejected(Exception):
    """An order that will not be stored or relayed, and why."""


# ---------------------------------------------------------------------------
# Building and signing
# ---------------------------------------------------------------------------

def build_order(maker_lapse_addr, maker_xlm_addr, direction, lapse_total,
                price_stroops_per_lapse, expiry_block, pubkey_hex,
                min_fill=0, max_fill=0):
    """The unsigned body of an order, in canonical field order."""
    if direction not in ("buy", "sell"):
        raise ValueError("direction must be 'buy' or 'sell'")
    return {
        "order_id": str(uuid.uuid4()),
        "maker_lapse_addr": maker_lapse_addr,
        "maker_xlm_addr": maker_xlm_addr,
        "direction": direction,
        "lapse_total": int(lapse_total),
        "price_stroops_per_lapse": int(price_stroops_per_lapse),
        "min_fill": int(min_fill),
        "max_fill": int(max_fill),
        "expiry_block": int(expiry_block),
        "pubkey": pubkey_hex,
    }


def _signing_bytes(order):
    return canonical_json({k: order[k] for k in SIGNED_FIELDS})


def sign_order(order, keyfile_path, kek):
    """Sign an order in place with the maker's LapseCoin key."""
    signature = crypto.sign_with_keyfile(_signing_bytes(order), keyfile_path, kek)
    order["signature"] = signature.hex()
    return order


def cancellation_for(order_id, pubkey_hex, keyfile_path, kek):
    """A signed withdrawal of an order.

    Signed over the same key that made it, so only the maker can pull
    their own offer. Already-delivered steps stand; this only withdraws
    what is unfilled, which is why it carries no amount.
    """
    body = {"cancel": order_id, "pubkey": pubkey_hex}
    signature = crypto.sign_with_keyfile(canonical_json(body), keyfile_path, kek)
    body["signature"] = signature.hex()
    return body


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_order(order, current_height=None):
    """Check an order arriving from the network. Raises OrderRejected.

    Every field is checked before the signature, because a malformed
    order should cost a type check rather than a FALCON verification;
    signature checking is the expensive part and this is an unauthenticated
    surface anyone can send to.
    """
    if not isinstance(order, dict):
        raise OrderRejected("not an object")

    missing = [f for f in SIGNED_FIELDS if f not in order]
    if missing:
        raise OrderRejected(f"missing field(s): {missing}")
    if "signature" not in order:
        raise OrderRejected("missing signature")

    unexpected = set(order) - set(SIGNED_FIELDS) - {"signature"}
    if unexpected:
        # Refused rather than ignored: an unknown field is not covered by
        # the signature, so accepting one means storing content the maker
        # never signed.
        raise OrderRejected(f"unexpected field(s): {sorted(unexpected)}")

    if order["direction"] not in ("buy", "sell"):
        raise OrderRejected("direction must be 'buy' or 'sell'")

    for field in ("lapse_total", "price_stroops_per_lapse", "min_fill",
                  "max_fill", "expiry_block"):
        if not isinstance(order[field], int) or isinstance(order[field], bool):
            raise OrderRejected(f"{field} must be an integer")
    if order["lapse_total"] <= 0:
        raise OrderRejected("lapse_total must be positive")
    if order["price_stroops_per_lapse"] <= 0:
        raise OrderRejected("price must be positive")
    if order["min_fill"] < 0 or order["max_fill"] < 0:
        raise OrderRejected("fill bounds must not be negative")
    if order["min_fill"] > order["lapse_total"]:
        raise OrderRejected("min_fill exceeds the order size")
    if order["max_fill"] and order["max_fill"] < order["min_fill"]:
        raise OrderRejected("max_fill is below min_fill")

    if not crypto.is_valid_address(order["maker_lapse_addr"]):
        raise OrderRejected("maker_lapse_addr is not a valid address")

    import xlm as xlm_mod
    if not xlm_mod.is_valid_address(order["maker_xlm_addr"]):
        raise OrderRejected("maker_xlm_addr is not a valid Stellar address")

    if current_height is not None:
        if order["expiry_block"] <= current_height:
            raise OrderRejected("already expired")
        if order["expiry_block"] > current_height + MAX_EXPIRY_HORIZON_BLOCKS:
            # An order that never expires is a standing advertisement, which
            # is the shape this design deliberately avoids, and it would sit
            # in every node's book indefinitely.
            raise OrderRejected("expiry is too far ahead")

    # Last, and cheaper than the signature check that follows but not as
    # cheap as everything above it: two database counts. Checked against
    # the address the order merely claims to be from, which is safe even
    # though nothing has proven that claim yet, since refusing early here
    # can only reject work that would have been rejected anyway (a forged
    # address still fails the signature check right after this), while a
    # genuine maker already at its own limit is refused without this node
    # ever paying for that check.
    _check_admission(order["maker_lapse_addr"])

    _verify_signature(order)
    return True


def _check_admission(maker_lapse_addr):
    """Refuse before the expensive check rather than after it. Raises
    OrderRejected. store_order enforces the per-maker limit again as the
    actual gate before a write; this exists purely so a maker already at
    capacity, or a book already at its own ceiling, costs this node a
    database count instead of a FALCON verification."""
    ensure_tables()
    if Order.select().count() >= MAX_ORDERS_TOTAL:
        raise OrderRejected(f"the order book is full (limit {MAX_ORDERS_TOTAL})")
    live = (Order.select()
            .where(Order.maker_lapse_addr == maker_lapse_addr,
                   Order.cancelled == False)  # noqa: E712
            .count())
    if live >= MAX_ORDERS_PER_MAKER:
        raise OrderRejected(
            f"maker already has {live} live orders here "
            f"(limit {MAX_ORDERS_PER_MAKER})")


def _verify_signature(order):
    try:
        pubkey = bytes.fromhex(order["pubkey"])
        signature = bytes.fromhex(order["signature"])
    except (ValueError, TypeError):
        raise OrderRejected("pubkey and signature must be hex")
    if crypto.public_key_to_address(pubkey) != order["maker_lapse_addr"]:
        raise OrderRejected("pubkey does not match maker_lapse_addr")
    if not crypto.verify(_signing_bytes(order), signature, pubkey):
        raise OrderRejected("signature does not verify")


def verify_cancellation(body):
    """Check a signed cancellation. Raises OrderRejected."""
    if not isinstance(body, dict) or "cancel" not in body:
        raise OrderRejected("not a cancellation")
    for field in ("pubkey", "signature"):
        if field not in body:
            raise OrderRejected(f"missing {field}")
    try:
        pubkey = bytes.fromhex(body["pubkey"])
        signature = bytes.fromhex(body["signature"])
    except (ValueError, TypeError):
        raise OrderRejected("pubkey and signature must be hex")
    signed = canonical_json({"cancel": body["cancel"], "pubkey": body["pubkey"]})
    if not crypto.verify(signed, signature, pubkey):
        raise OrderRejected("signature does not verify")
    return crypto.public_key_to_address(pubkey)


def is_cancellation(item):
    return isinstance(item, dict) and "cancel" in item


# ---------------------------------------------------------------------------
# Identity, for gossip dedup
# ---------------------------------------------------------------------------

def order_hash(item):
    """The dedup identity of an order or cancellation.

    Over the signed content, not the whole item, so the same order cannot
    be given a new identity by padding it with junk. That would be dedup
    switched off: each padded copy would look new and be re-flooded by
    every node that saw it.
    """
    if is_cancellation(item):
        return crypto.sha256_hex(canonical_json(
            {"cancel": item["cancel"], "pubkey": item.get("pubkey", "")}))
    body = {k: item.get(k) for k in SIGNED_FIELDS}
    return crypto.sha256_hex(canonical_json(body))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_order(order):
    """Persist a verified order. Returns False if it was already known.

    on_conflict_ignore rather than replace: an order is immutable once
    signed, so a second copy carries nothing new, and replacing would let
    a relayed duplicate reset locally-derived columns.
    """
    ensure_tables()
    existing = Order.get_or_none(Order.order_id == order["order_id"])
    if existing is not None:
        return False

    if Order.select().count() >= MAX_ORDERS_TOTAL:
        raise OrderRejected(f"the order book is full (limit {MAX_ORDERS_TOTAL})")
    live = (Order.select()
            .where(Order.maker_lapse_addr == order["maker_lapse_addr"],
                   Order.cancelled == False)  # noqa: E712
            .count())
    if live >= MAX_ORDERS_PER_MAKER:
        raise OrderRejected(
            f"maker already has {live} live orders here "
            f"(limit {MAX_ORDERS_PER_MAKER})")

    Order.create(
        order_id=order["order_id"],
        maker_lapse_addr=order["maker_lapse_addr"],
        maker_xlm_addr=order["maker_xlm_addr"],
        direction=order["direction"],
        lapse_total=order["lapse_total"],
        price_stroops_per_lapse=order["price_stroops_per_lapse"],
        min_fill=order["min_fill"],
        max_fill=order["max_fill"] or order["lapse_total"],
        expiry_block=order["expiry_block"],
        pubkey=order["pubkey"],
        signature=order["signature"],
        created_at=time.time(),
        received_at=time.time(),
        verified=True,
    )
    return True


def apply_cancellation(order_id, canceller_addr):
    """Withdraw an order's unfilled remainder. Returns True if it applied."""
    ensure_tables()
    row = Order.get_or_none(Order.order_id == order_id)
    if row is None:
        return False
    if row.maker_lapse_addr != canceller_addr:
        raise OrderRejected("only the maker may cancel their own order")
    if row.cancelled:
        return False
    row.cancelled = True
    row.save()
    return True


def delivered_ticks(order_id):
    """How much of an order has actually been delivered, per the chain.

    Counts only steps whose legs both settled, so a trade in flight does
    not reduce the advertised size until it genuinely has.
    """
    ensure_tables()
    sessions = [t.session_id for t in
                Trade.select(Trade.session_id).where(Trade.order_id == order_id)]
    if not sessions:
        return 0
    rows = (Increment
            .select(Increment.lapse_amount)
            .where(Increment.session_id.in_(sessions),
                   Increment.out_state == LEG_SETTLED,
                   Increment.in_state == LEG_SETTLED))
    return sum(r.lapse_amount for r in rows)


def reserved_ticks(order_id):
    """How much of an order is spoken for by an accepted fill response
    this node has no completed-trade record for.

    delivered_ticks alone is only accurate for this order's own maker:
    it counts Trade rows, and a node only ever has a Trade row for a
    fill it was itself a party to. A node that is neither the maker nor
    any taker of this order has zero such rows regardless of how much
    of it strangers have actually filled, and would otherwise report
    the order as fully untouched forever. An accepted fill response
    fixes this because, unlike a Trade row, it is gossiped to the whole
    network exactly like an order is (see market.FillResponse,
    node._handle_inbound_fill_response): any node can see every live
    response against any order, not only its own.

    Only *accepted* responses count: unlike the old claim-based design,
    the maker has already looked at this specific fill and explicitly
    committed to it before the response ever went out, so there is no
    reason to also count a pending, unanswered request "just in case" -
    that used to be the only signal available and had to be treated
    conservatively for exactly that reason. It still cannot overcount
    an order that will genuinely be honoured: an accepted response is
    the maker's own attested exposure, and this node's own copy of it
    ages out (see FILL_RESPONSE_MAX_AGE_SECONDS) the same way a claim's
    did if the trade it names is not the one this node ever settles.
    """
    ensure_tables()
    known_sessions = {t.session_id for t in
                      Trade.select(Trade.session_id).where(Trade.order_id == order_id)}
    responses = (FillResponse.select()
                .where(FillResponse.order_id == order_id,
                       FillResponse.accepted == True))          # noqa: E712
    return sum(r.lapse_total for r in responses
              if r.session_id not in known_sessions)


def remaining_ticks(order_row):
    committed = delivered_ticks(order_row.order_id) + reserved_ticks(order_row.order_id)
    return max(order_row.lapse_total - committed, 0)


def validate_fill(order_row, lapse_total):
    """Check a proposed fill size against an order's own terms. Raises
    OrderRejected with a human-readable reason if it does not fit.

    Shared by the taker's own request (market_routes._start_trade) and
    the maker's independent re-check of a claim
    (swap_engine.discover_trades): the same three bounds apply to a fill
    regardless of which side proposes it, and a maker must never take a
    taker's word that its own order permits what a claim states, any
    more than a taker's own request is trusted without this check.
    """
    if lapse_total <= 0:
        raise OrderRejected("fill amount must be positive")
    remaining = remaining_ticks(order_row)
    if lapse_total > remaining:
        raise OrderRejected("that is more than the order has left")
    if order_row.min_fill and lapse_total < order_row.min_fill:
        raise OrderRejected(
            f"this order will not go below {order_row.min_fill} ticks")
    max_fill = order_row.max_fill or order_row.lapse_total
    if lapse_total > max_fill:
        raise OrderRejected("that is more than this order's max fill")


def open_orders(current_height, exclude_maker=None):
    """Every order still on offer here, newest first."""
    ensure_tables()
    query = (Order.select()
             .where(Order.cancelled == False,          # noqa: E712
                    Order.expiry_block > current_height)
             .order_by(Order.received_at.desc()))
    if exclude_maker:
        query = query.where(Order.maker_lapse_addr != exclude_maker)
    return [row for row in query if remaining_ticks(row) > 0]


def orders_by_maker(addr, current_height):
    """Every one of this maker's own orders still on the book, whether or
    not anything remains to fill.

    Distinct from open_orders: that one is a taker's view of what is
    available to trade against, so it drops anything already fully
    delivered. This is a maker managing their own orders, who still wants
    to see one that just finished.
    """
    ensure_tables()
    return list(Order.select()
                .where(Order.maker_lapse_addr == addr,
                       Order.cancelled == False,          # noqa: E712
                       Order.expiry_block > current_height)
                .order_by(Order.received_at.desc()))


def orders_by_maker_with_claims(addr):
    """This maker's own orders that have at least one live claim against
    them, regardless of whether the order itself is still open.

    Discovery-specific (see swap_engine.discover_trades), and
    deliberately not filtered by cancelled or expiry the way
    orders_by_maker is: a claim that arrived, and was already paid for,
    before this node cancelled its own order still deserves completion.
    Cancelling withdraws what is unfilled, not what already has money
    moving against it, and filtering this query the same way
    orders_by_maker is would strand exactly that taker.
    """
    ensure_tables()
    order_ids = [row.order_id for row in
                Claim.select(Claim.order_id).distinct()]
    if not order_ids:
        return []
    return list(Order.select()
                .where(Order.maker_lapse_addr == addr,
                       Order.order_id.in_(order_ids)))


def get_order(order_id):
    ensure_tables()
    return Order.get_or_none(Order.order_id == order_id)


def already_known(item):
    """Whether this exact order or cancellation has already been handled
    here, cheaply and without a signature check.

    Backed by the database rather than gossip's own seen-cache. Those
    answer different questions: this one is "have we already verified and
    stored this", gossip's is "have we already put this on the wire in the
    public phase". Answering the first from the second looks harmless but
    is not: it marks an item as flooded before this node has actually
    flooded it, so the one call downstream that would have done so finds
    it already marked and silently sends nothing. That is invisible on two
    directly connected nodes and total past the first hop on a real
    network, which is exactly the shape a propagation bug takes when
    nothing measures delivery across topology (see
    gossip.Gossip.mark_seen and the harness in test_gossip.py).
    """
    if not isinstance(item, dict):
        return False
    ensure_tables()
    if is_cancellation(item):
        row = get_order(item.get("cancel", ""))
        return bool(row and row.cancelled)
    row = get_order(item.get("order_id", ""))
    return row is not None


def prune_expired(current_height):
    """Drop orders the chain has aged out. Returns how many went.

    Kept as housekeeping rather than filtered at read time so a node that
    traded once does not carry the book forever.
    """
    ensure_tables()
    return (Order.delete()
            .where(Order.expiry_block <= current_height)
            .execute())


# ---------------------------------------------------------------------------
# Depth, for display
# ---------------------------------------------------------------------------

def book_depth(current_height, exclude_maker=None):
    """The book as two price-sorted sides.

    Buyers are sorted best-price-first meaning highest, sellers
    lowest-first, so in both cases the top of the list is the best
    available deal for whoever is reading it.
    """
    buys, sells = [], []
    for row in open_orders(current_height, exclude_maker):
        entry = {
            "order_id": row.order_id,
            "maker": row.maker_lapse_addr,
            "price": row.price_stroops_per_lapse,
            "total": row.lapse_total,
            "remaining": remaining_ticks(row),
            "min_fill": row.min_fill,
            "max_fill": row.max_fill,
            "expiry_block": row.expiry_block,
        }
        (buys if row.direction == "buy" else sells).append(entry)
    buys.sort(key=lambda e: e["price"], reverse=True)
    sells.sort(key=lambda e: e["price"])
    return {"buys": buys, "sells": sells}


def best_prices(current_height, exclude_maker=None):
    """Top of book each way, and the spread between them.

    None where a side is empty, which on a new coin is the ordinary case
    rather than an error, and the UI should say so plainly instead of
    showing a price that does not exist.
    """
    depth = book_depth(current_height, exclude_maker)
    best_buy = depth["buys"][0]["price"] if depth["buys"] else None
    best_sell = depth["sells"][0]["price"] if depth["sells"] else None
    spread = (best_sell - best_buy) if (best_buy and best_sell) else None
    return {"best_buy": best_buy, "best_sell": best_sell, "spread": spread,
            "buy_depth": sum(e["remaining"] for e in depth["buys"]),
            "sell_depth": sum(e["remaining"] for e in depth["sells"])}


# ---------------------------------------------------------------------------
# Ticker: what LAPSE actually traded for, from this node's own history
# ---------------------------------------------------------------------------
#
# Both chains carry a matching session memo for every step of every trade,
# so the price a completed trade actually executed at is public and cheap
# to compute: it needs nothing beyond the Trade row a completed session
# already leaves behind here. What it is not is a network-wide feed: this
# node only ever sees trades it was itself a party to, so it can only ever
# report on its own history, never on the book as a whole. The UI must say
# so, not present this as a market-wide rate.
#
# Wash trading (a maker and taker under one operator's control, trading
# with themselves) is cheap here and impossible to rule out; there is no
# escrow or fee that makes it cost anything. A signed order and a signed
# claim stand behind every Trade row already (see the module docstring
# and the claim section above), so nothing further is required to "count"
# a trade, but that alone does not stop wash trading between two
# addresses controlled by the same person. What actually blunts it: a
# median rather than a mean, since a handful of self-traded outliers can
# only pull a mean arbitrarily far but can move a median only by
# outnumbering genuine trades, and weighting each sample by this node's
# own trust score for the counterparty, so a pair of fresh, unstaked
# addresses trading with each other back and forth counts for as little
# as trust.score already makes a fresh identity worth.

# A trade with a zero-trust counterparty still happened and still belongs
# in the sample; it is simply worth as little as any other zero-score
# observation, which is what floors it at rather than at zero weight
# (a true zero would let it vanish from the total and, if every sample
# happened to be zero-score, leave nothing to divide by).
_TICKER_MIN_WEIGHT = 1e-9


def executed_trade_prices(node, limit=200):
    """(price_stroops_per_lapse, weight) for this node's own most recent
    completed trades, most recent first. See the module section above
    for what "weight" means and why this is inherently a local, not a
    network-wide, view.
    """
    ensure_tables()
    rows = (Trade.select()
            .where(Trade.status == TRADE_COMPLETED)
            .order_by(Trade.updated_at.desc())
            .limit(limit))
    out = []
    for row in rows:
        if row.lapse_total <= 0 or row.xlm_total <= 0:
            continue
        price = row.xlm_total * TICKS_PER_LAPSE // row.lapse_total
        if price <= 0:
            continue
        detail = trust_mod.get_detail(
            row.peer_lapse_addr,
            trust_mod.address_age_blocks(node, row.peer_lapse_addr),
            node.view.state.get_balance(row.peer_lapse_addr))
        weight = max(detail["score"], _TICKER_MIN_WEIGHT)
        out.append((price, weight))
    return out


def _weighted_median(samples):
    """The value at the 50th percentile by cumulative weight.

    Falls back to the plain (unweighted) median only in the degenerate
    case every sample carries the floor weight, since a weighted median
    over equal weights is just the ordinary median with extra steps.
    """
    ordered = sorted(samples, key=lambda s: s[0])
    total = sum(w for _p, w in ordered)
    half = total / 2
    cum = 0.0
    for price, weight in ordered:
        cum += weight
        if cum >= half:
            return price
    return ordered[-1][0]


def ticker_price(node, limit=200):
    """A weighted median of this node's own recently completed trades, or
    None with nothing yet to show. See the module section above for what
    this number is and, as importantly, what it is not.
    """
    samples = executed_trade_prices(node, limit)
    if not samples:
        return None
    return _weighted_median(samples)


# ---------------------------------------------------------------------------
# Fill claims: how a taker tells a maker where to pay, with no handshake
# ---------------------------------------------------------------------------
#
# A maker discovers a trade from the sender of an incoming payment, but
# that only ever reveals the taker's address on the chain the payment
# arrived on. The other address, needed for the maker's own reciprocating
# leg, has nowhere to go: Stellar's memo is 28 bytes and already spent on
# the order and session reference, and nothing links a LapseCoin key to a
# Stellar one (nothing should; see trust.mutual_scores on why that
# pairing is not published for its own sake). So the taker also gossips a
# claim, signed the same way an order is: proof of controlling the
# address it names, nothing more. A maker matching a payment to a claim
# still independently re-derives the schedule and checks it against its
# own exposure cap before creating anything (swap_engine.discover_trades)
# rather than trusting a single field of what a stranger sent it.

CLAIM_SIGNED_FIELDS = (
    "order_id", "session_id", "taker_lapse_addr", "taker_xlm_addr",
    "lapse_total", "increment_count", "pubkey",
)

# A taker is as free to generate keypairs as a maker is, so claims need
# their own bound, mirroring MAX_ORDERS_PER_MAKER, rather than trusting
# that a valid signature implies good faith.
MAX_CLAIMS_PER_TAKER = 20

# The claim table's own ceiling, mirroring MAX_ORDERS_TOTAL for the same
# reason: a per-address cap alone bounds nothing against an attacker
# willing to mint addresses. Smaller than the order book's, since claims
# are pruned within the hour (CLAIM_MAX_AGE_SECONDS) while orders can
# live for months, so sustained abuse has far less time to accumulate.
MAX_CLAIMS_TOTAL = 10_000

# How long an unmatched claim is kept. The claim and the payment it
# precedes propagate over two independent channels (gossip and a public
# chain) at very different speeds, so this has to be generous relative to
# either; short enough that one nobody ever followed through on does not
# accumulate forever.
CLAIM_MAX_AGE_SECONDS = 3600


class ClaimRejected(Exception):
    """A claim that will not be stored or relayed, and why."""


def build_claim(order_id, session_id, taker_lapse_addr, taker_xlm_addr,
                lapse_total, increment_count, pubkey_hex):
    """The unsigned body of a claim, in canonical field order."""
    return {
        "order_id": order_id,
        "session_id": session_id,
        "taker_lapse_addr": taker_lapse_addr,
        "taker_xlm_addr": taker_xlm_addr,
        "lapse_total": int(lapse_total),
        "increment_count": int(increment_count),
        "pubkey": pubkey_hex,
    }


def _claim_signing_bytes(claim):
    return canonical_json({k: claim[k] for k in CLAIM_SIGNED_FIELDS})


def sign_claim(claim, keyfile_path, kek):
    """Sign a claim in place with the taker's LapseCoin key."""
    signature = crypto.sign_with_keyfile(_claim_signing_bytes(claim), keyfile_path, kek)
    claim["signature"] = signature.hex()
    return claim


def verify_claim(claim):
    """Check a claim arriving from the network. Raises ClaimRejected.

    Deliberately self-contained, the same division verify_order draws:
    this checks only that the claim is well-formed and genuinely signed
    by the address it names. Whether it makes sense against a *specific*
    order (remaining size, exposure cap, min/max fill against this node's
    own trust view of this taker) is for the maker's own discovery pass
    to decide once it actually has that order and that view in hand (see
    swap_engine.discover_trades); baking it in here would mean every peer
    that merely relays this claim re-deriving business logic that applies
    to, at most, the one node that posted the matching order.
    """
    if not isinstance(claim, dict):
        raise ClaimRejected("not an object")

    missing = [f for f in CLAIM_SIGNED_FIELDS if f not in claim]
    if missing:
        raise ClaimRejected(f"missing field(s): {missing}")
    if "signature" not in claim:
        raise ClaimRejected("missing signature")

    unexpected = set(claim) - set(CLAIM_SIGNED_FIELDS) - {"signature"}
    if unexpected:
        raise ClaimRejected(f"unexpected field(s): {sorted(unexpected)}")

    if not isinstance(claim["order_id"], str) or not claim["order_id"]:
        raise ClaimRejected("order_id must be a non-empty string")
    if not isinstance(claim["session_id"], str) or not claim["session_id"]:
        raise ClaimRejected("session_id must be a non-empty string")

    for field in ("lapse_total", "increment_count"):
        if not isinstance(claim[field], int) or isinstance(claim[field], bool):
            raise ClaimRejected(f"{field} must be an integer")
    if claim["lapse_total"] <= 0:
        raise ClaimRejected("lapse_total must be positive")
    if not (swap_mod.MIN_INCREMENTS <= claim["increment_count"] <= swap_mod.MAX_INCREMENTS):
        raise ClaimRejected(
            f"increment_count must be between {swap_mod.MIN_INCREMENTS} "
            f"and {swap_mod.MAX_INCREMENTS}")

    if not crypto.is_valid_address(claim["taker_lapse_addr"]):
        raise ClaimRejected("taker_lapse_addr is not a valid address")

    import xlm as xlm_mod
    if not xlm_mod.is_valid_address(claim["taker_xlm_addr"]):
        raise ClaimRejected("taker_xlm_addr is not a valid Stellar address")

    # Last, and cheaper than the signature check that follows: see
    # market._check_admission's reasoning, applied to claims instead of
    # orders. store_claim enforces the per-taker limit again as the
    # actual gate before a write.
    _check_claim_admission(claim["taker_lapse_addr"])

    _verify_claim_signature(claim)
    return True


def _check_claim_admission(taker_lapse_addr):
    ensure_tables()
    if Claim.select().count() >= MAX_CLAIMS_TOTAL:
        raise ClaimRejected(f"the claim book is full (limit {MAX_CLAIMS_TOTAL})")
    live = (Claim.select()
            .where(Claim.taker_lapse_addr == taker_lapse_addr)
            .count())
    if live >= MAX_CLAIMS_PER_TAKER:
        raise ClaimRejected(
            f"taker already has {live} live claims here "
            f"(limit {MAX_CLAIMS_PER_TAKER})")


def _verify_claim_signature(claim):
    try:
        pubkey = bytes.fromhex(claim["pubkey"])
        signature = bytes.fromhex(claim["signature"])
    except (ValueError, TypeError):
        raise ClaimRejected("pubkey and signature must be hex")
    if crypto.public_key_to_address(pubkey) != claim["taker_lapse_addr"]:
        raise ClaimRejected("pubkey does not match taker_lapse_addr")
    if not crypto.verify(_claim_signing_bytes(claim), signature, pubkey):
        raise ClaimRejected("signature does not verify")


def claim_hash(claim):
    """The dedup identity of a claim, over the signed content only, for
    the same reason order_hash is: padding must not mint a new identity."""
    body = {k: claim.get(k) for k in CLAIM_SIGNED_FIELDS}
    return crypto.sha256_hex(canonical_json(body))


def store_claim(claim):
    """Persist a verified claim. Returns False if it was already known."""
    ensure_tables()
    if Claim.get_or_none(Claim.session_id == claim["session_id"]) is not None:
        return False

    if Claim.select().count() >= MAX_CLAIMS_TOTAL:
        raise ClaimRejected(f"the claim book is full (limit {MAX_CLAIMS_TOTAL})")
    live = (Claim.select()
            .where(Claim.taker_lapse_addr == claim["taker_lapse_addr"])
            .count())
    if live >= MAX_CLAIMS_PER_TAKER:
        raise ClaimRejected(
            f"taker already has {live} live claims here "
            f"(limit {MAX_CLAIMS_PER_TAKER})")

    Claim.create(
        session_id=claim["session_id"], order_id=claim["order_id"],
        taker_lapse_addr=claim["taker_lapse_addr"],
        taker_xlm_addr=claim["taker_xlm_addr"],
        lapse_total=claim["lapse_total"],
        increment_count=claim["increment_count"],
        pubkey=claim["pubkey"], signature=claim["signature"],
        received_at=time.time())
    return True


def get_claim(session_id):
    ensure_tables()
    return Claim.get_or_none(Claim.session_id == session_id)


def claims_for_order(order_id):
    """Every live claim against one order, for the maker's discovery pass."""
    ensure_tables()
    return list(Claim.select().where(Claim.order_id == order_id))


def already_known_claim(claim):
    """Whether this exact claim has already been verified and stored,
    cheaply and without a signature check. Same role as already_known,
    for the same reason: see its docstring."""
    if not isinstance(claim, dict):
        return False
    ensure_tables()
    return get_claim(claim.get("session_id", "")) is not None


def prune_claims(now=None):
    """Drop claims old enough that whatever they precede either already
    happened (a Trade exists) or was never going to. Returns how many."""
    ensure_tables()
    now = time.time() if now is None else now
    cutoff = now - CLAIM_MAX_AGE_SECONDS
    return Claim.delete().where(Claim.received_at <= cutoff).execute()


# ---------------------------------------------------------------------------
# Fill requests and responses: agree before a stroop moves, not after
# ---------------------------------------------------------------------------
#
# The claim section above exists to solve one problem: a maker discovering
# a trade only from an incoming payment has no way to learn the taker's
# address on the *other* chain. Solving only that problem left a bigger one
# in place: the taker sends a real first payment on a schedule it derived
# itself, speculatively, before the maker has looked at it at all. If the
# maker's own remaining size or exposure cap does not actually leave room
# for it, the taker's payment lands into a trade that will never be
# created, and nothing pays it back.
#
# A request and a response, both gossiped exactly like an order (dandelion
# stem/fluff, not a direct connection to a specific peer, so nothing here
# reveals whose IP maps to which address any more than an order already
# does), let the maker decide *before* anything moves and reserve the
# capacity it just promised the instant it accepts. Two takers racing the
# same order are now arbitrated by the one node that actually knows the
# truth about it, not by whichever one's payment happens to land first.
# This also removes the reason a claim's address exchange was needed in
# the first place, and the reason step 1 used to be forced onto the taker
# regardless of trust (see swap_engine's module docstring and
# market_routes._open_trade): the maker now learns a trade exists, and
# agrees to it, before either side has sent anything.

FILL_REQUEST_SIGNED_FIELDS = (
    "request_id", "order_id", "session_id", "taker_lapse_addr",
    "taker_xlm_addr", "lapse_total", "pubkey",
)

FILL_RESPONSE_SIGNED_FIELDS = (
    "request_id", "order_id", "session_id", "lapse_total",
    "accepted", "increment_count", "reason", "maker_pubkey",
)

# Same bounds and same reasoning as the claim book's (MAX_CLAIMS_PER_TAKER
# / MAX_CLAIMS_TOTAL): a taker is as free to mint keypairs as a maker is,
# so a per-address cap alone bounds nothing against one willing to do
# that, and the book itself needs its own ceiling regardless of how many
# addresses an attacker mints.
MAX_FILL_REQUESTS_PER_TAKER = 20
MAX_FILL_REQUESTS_TOTAL = 10_000
FILL_REQUEST_MAX_AGE_SECONDS = 3600

MAX_FILL_RESPONSES_TOTAL = 10_000
FILL_RESPONSE_MAX_AGE_SECONDS = 3600


class FillRequestRejected(Exception):
    """A fill request that will not be stored or relayed, and why."""


class FillResponseRejected(Exception):
    """A fill response that will not be stored or relayed, and why."""


def build_fill_request(order_id, session_id, taker_lapse_addr, taker_xlm_addr,
                       lapse_total, pubkey_hex):
    """The unsigned body of a fill request, in canonical field order."""
    return {
        "request_id": str(uuid.uuid4()),
        "order_id": order_id,
        "session_id": session_id,
        "taker_lapse_addr": taker_lapse_addr,
        "taker_xlm_addr": taker_xlm_addr,
        "lapse_total": int(lapse_total),
        "pubkey": pubkey_hex,
    }


def _fill_request_signing_bytes(req):
    return canonical_json({k: req[k] for k in FILL_REQUEST_SIGNED_FIELDS})


def sign_fill_request(req, keyfile_path, kek):
    """Sign a fill request in place with the taker's LapseCoin key."""
    signature = crypto.sign_with_keyfile(_fill_request_signing_bytes(req), keyfile_path, kek)
    req["signature"] = signature.hex()
    return req


def verify_fill_request(req):
    """Check a fill request arriving from the network. Raises
    FillRequestRejected.

    Self-contained the same way verify_claim is: only whether this is
    well-formed and genuinely signed by the address it names. Whether it
    fits a *specific* order (remaining size, this node's own exposure
    cap for this taker) is for whichever node actually holds that
    order's maker key to decide (see node._handle_inbound_fill_request);
    every other peer that merely relays this is not in a position to
    judge that and must not be made to.
    """
    if not isinstance(req, dict):
        raise FillRequestRejected("not an object")

    missing = [f for f in FILL_REQUEST_SIGNED_FIELDS if f not in req]
    if missing:
        raise FillRequestRejected(f"missing field(s): {missing}")
    if "signature" not in req:
        raise FillRequestRejected("missing signature")

    unexpected = set(req) - set(FILL_REQUEST_SIGNED_FIELDS) - {"signature"}
    if unexpected:
        raise FillRequestRejected(f"unexpected field(s): {sorted(unexpected)}")

    if not isinstance(req["request_id"], str) or not req["request_id"]:
        raise FillRequestRejected("request_id must be a non-empty string")
    if not isinstance(req["order_id"], str) or not req["order_id"]:
        raise FillRequestRejected("order_id must be a non-empty string")
    if not isinstance(req["session_id"], str) or not req["session_id"]:
        raise FillRequestRejected("session_id must be a non-empty string")
    if not isinstance(req["lapse_total"], int) or isinstance(req["lapse_total"], bool):
        raise FillRequestRejected("lapse_total must be an integer")
    if req["lapse_total"] <= 0:
        raise FillRequestRejected("lapse_total must be positive")

    if not crypto.is_valid_address(req["taker_lapse_addr"]):
        raise FillRequestRejected("taker_lapse_addr is not a valid address")

    import xlm as xlm_mod
    if not xlm_mod.is_valid_address(req["taker_xlm_addr"]):
        raise FillRequestRejected("taker_xlm_addr is not a valid Stellar address")

    # Last, and cheaper than the signature check that follows: same
    # reasoning as market._check_claim_admission.
    _check_fill_request_admission(req["taker_lapse_addr"])

    _verify_fill_request_signature(req)
    return True


def _check_fill_request_admission(taker_lapse_addr):
    ensure_tables()
    if FillRequest.select().count() >= MAX_FILL_REQUESTS_TOTAL:
        raise FillRequestRejected(
            f"the request book is full (limit {MAX_FILL_REQUESTS_TOTAL})")
    live = (FillRequest.select()
            .where(FillRequest.taker_lapse_addr == taker_lapse_addr)
            .count())
    if live >= MAX_FILL_REQUESTS_PER_TAKER:
        raise FillRequestRejected(
            f"taker already has {live} live requests here "
            f"(limit {MAX_FILL_REQUESTS_PER_TAKER})")


def _verify_fill_request_signature(req):
    try:
        pubkey = bytes.fromhex(req["pubkey"])
        signature = bytes.fromhex(req["signature"])
    except (ValueError, TypeError):
        raise FillRequestRejected("pubkey and signature must be hex")
    if crypto.public_key_to_address(pubkey) != req["taker_lapse_addr"]:
        raise FillRequestRejected("pubkey does not match taker_lapse_addr")
    if not crypto.verify(_fill_request_signing_bytes(req), signature, pubkey):
        raise FillRequestRejected("signature does not verify")


def fill_request_hash(req):
    body = {k: req.get(k) for k in FILL_REQUEST_SIGNED_FIELDS}
    return crypto.sha256_hex(canonical_json(body))


def already_known_fill_request(req):
    if not isinstance(req, dict):
        return False
    ensure_tables()
    rid = req.get("request_id")
    if not isinstance(rid, str):
        return False
    return FillRequest.get_or_none(FillRequest.request_id == rid) is not None


def store_fill_request(req):
    """Persist a verified fill request. Returns False if already known."""
    ensure_tables()
    if FillRequest.get_or_none(FillRequest.request_id == req["request_id"]) is not None:
        return False

    if FillRequest.select().count() >= MAX_FILL_REQUESTS_TOTAL:
        raise FillRequestRejected(
            f"the request book is full (limit {MAX_FILL_REQUESTS_TOTAL})")
    live = (FillRequest.select()
            .where(FillRequest.taker_lapse_addr == req["taker_lapse_addr"])
            .count())
    if live >= MAX_FILL_REQUESTS_PER_TAKER:
        raise FillRequestRejected(
            f"taker already has {live} live requests here "
            f"(limit {MAX_FILL_REQUESTS_PER_TAKER})")

    FillRequest.create(
        request_id=req["request_id"], order_id=req["order_id"],
        session_id=req["session_id"], taker_lapse_addr=req["taker_lapse_addr"],
        taker_xlm_addr=req["taker_xlm_addr"], lapse_total=req["lapse_total"],
        pubkey=req["pubkey"], signature=req["signature"], received_at=time.time())
    return True


def get_fill_request(request_id):
    ensure_tables()
    return FillRequest.get_or_none(FillRequest.request_id == request_id)


def requests_for_order(order_id):
    """Every live fill request against one order, for the maker to act on."""
    ensure_tables()
    return list(FillRequest.select().where(FillRequest.order_id == order_id))


def prune_fill_requests(now=None):
    ensure_tables()
    now = time.time() if now is None else now
    cutoff = now - FILL_REQUEST_MAX_AGE_SECONDS
    return FillRequest.delete().where(FillRequest.received_at <= cutoff).execute()


def build_fill_response(request_id, order_id, session_id, lapse_total, accepted,
                        maker_pubkey_hex, increment_count=None, reason=""):
    """The unsigned body of a fill response, in canonical field order.

    increment_count must be given when accepting (it is the schedule
    length the maker is committing to) and must be omitted otherwise;
    verify_fill_response enforces that pairing on the way back in.
    """
    return {
        "request_id": request_id,
        "order_id": order_id,
        "session_id": session_id,
        "lapse_total": int(lapse_total),
        "accepted": bool(accepted),
        "increment_count": int(increment_count) if increment_count is not None else None,
        "reason": reason,
        "maker_pubkey": maker_pubkey_hex,
    }


def _fill_response_signing_bytes(resp):
    return canonical_json({k: resp[k] for k in FILL_RESPONSE_SIGNED_FIELDS})


def sign_fill_response(resp, keyfile_path, kek):
    """Sign a fill response in place with the maker's LapseCoin key."""
    signature = crypto.sign_with_keyfile(_fill_response_signing_bytes(resp), keyfile_path, kek)
    resp["signature"] = signature.hex()
    return resp


def verify_fill_response(resp, expected_maker_addr=None):
    """Check a fill response arriving from the network. Raises
    FillResponseRejected.

    expected_maker_addr is None for a plain relay check (any peer
    forwarding this cannot know or verify who was supposed to send it,
    the same division verify_claim draws). A taker acting on a response
    to its *own* outstanding request must always pass its order's real
    maker_lapse_addr here: without this check, anyone could sign a
    well-formed 'accepted' response with their own key for someone
    else's order and have it mistaken for that order's actual maker
    agreeing, since a signature alone only proves who signed it, not
    that they were the right one to.
    """
    if not isinstance(resp, dict):
        raise FillResponseRejected("not an object")

    missing = [f for f in FILL_RESPONSE_SIGNED_FIELDS if f not in resp]
    if missing:
        raise FillResponseRejected(f"missing field(s): {missing}")
    if "signature" not in resp:
        raise FillResponseRejected("missing signature")

    unexpected = set(resp) - set(FILL_RESPONSE_SIGNED_FIELDS) - {"signature"}
    if unexpected:
        raise FillResponseRejected(f"unexpected field(s): {sorted(unexpected)}")

    if not isinstance(resp["request_id"], str) or not resp["request_id"]:
        raise FillResponseRejected("request_id must be a non-empty string")
    if not isinstance(resp["order_id"], str) or not resp["order_id"]:
        raise FillResponseRejected("order_id must be a non-empty string")
    if not isinstance(resp["session_id"], str) or not resp["session_id"]:
        raise FillResponseRejected("session_id must be a non-empty string")
    if not isinstance(resp["lapse_total"], int) or isinstance(resp["lapse_total"], bool):
        raise FillResponseRejected("lapse_total must be an integer")
    if resp["lapse_total"] <= 0:
        raise FillResponseRejected("lapse_total must be positive")
    if not isinstance(resp["accepted"], bool):
        raise FillResponseRejected("accepted must be a boolean")

    if resp["accepted"]:
        if not isinstance(resp["increment_count"], int) or isinstance(resp["increment_count"], bool):
            raise FillResponseRejected("increment_count must be an integer when accepted")
        if not (swap_mod.MIN_INCREMENTS <= resp["increment_count"] <= swap_mod.MAX_INCREMENTS):
            raise FillResponseRejected(
                f"increment_count must be between {swap_mod.MIN_INCREMENTS} "
                f"and {swap_mod.MAX_INCREMENTS}")
    elif resp["increment_count"] is not None:
        raise FillResponseRejected("increment_count must be null when not accepted")

    if not isinstance(resp["reason"], str):
        raise FillResponseRejected("reason must be a string")

    try:
        pubkey = bytes.fromhex(resp["maker_pubkey"])
        signature = bytes.fromhex(resp["signature"])
    except (ValueError, TypeError):
        raise FillResponseRejected("pubkey and signature must be hex")

    maker_addr = crypto.public_key_to_address(pubkey)
    if expected_maker_addr is not None and maker_addr != expected_maker_addr:
        raise FillResponseRejected("not signed by the maker this request was sent to")
    if not crypto.verify(_fill_response_signing_bytes(resp), signature, pubkey):
        raise FillResponseRejected("signature does not verify")
    return True


def fill_response_hash(resp):
    body = {k: resp.get(k) for k in FILL_RESPONSE_SIGNED_FIELDS}
    return crypto.sha256_hex(canonical_json(body))


def already_known_fill_response(resp):
    if not isinstance(resp, dict):
        return False
    ensure_tables()
    rid = resp.get("request_id")
    if not isinstance(rid, str):
        return False
    return FillResponse.get_or_none(FillResponse.request_id == rid) is not None


def store_fill_response(resp):
    """Persist a verified fill response. Returns False if already known."""
    ensure_tables()
    if FillResponse.get_or_none(FillResponse.request_id == resp["request_id"]) is not None:
        return False
    if FillResponse.select().count() >= MAX_FILL_RESPONSES_TOTAL:
        raise FillResponseRejected(
            f"the response book is full (limit {MAX_FILL_RESPONSES_TOTAL})")
    FillResponse.create(
        request_id=resp["request_id"], order_id=resp["order_id"],
        session_id=resp["session_id"], lapse_total=resp["lapse_total"],
        accepted=resp["accepted"], increment_count=resp["increment_count"],
        reason=resp["reason"], maker_pubkey=resp["maker_pubkey"],
        signature=resp["signature"], received_at=time.time())
    return True


def get_fill_response(request_id):
    ensure_tables()
    return FillResponse.get_or_none(FillResponse.request_id == request_id)


def prune_fill_responses(now=None):
    ensure_tables()
    now = time.time() if now is None else now
    cutoff = now - FILL_RESPONSE_MAX_AGE_SECONDS
    return FillResponse.delete().where(FillResponse.received_at <= cutoff).execute()
