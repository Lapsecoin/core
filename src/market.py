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
    FillRequest, FillResponse, Order, Trade, Increment, StepReceipt,
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

def store_order(order, auto_match_margin_stroops=0):
    """Persist a verified order. Returns False if it was already known.

    on_conflict_ignore rather than replace: an order is immutable once
    signed, so a second copy carries nothing new, and replacing would let
    a relayed duplicate reset locally-derived columns.

    auto_match_margin_stroops is local-only bookkeeping for this node's
    own auto-matcher (see swap_engine.auto_match_orders): how much worse
    a rate this node will privately accept beyond the price above, when
    this is one of its own orders. It is not a field of `order` and
    never will be, on purpose: every caller relaying a gossiped order
    (node.py's inbound handlers) calls this without it, so a remote
    order always gets the harmless default, 0, never a value the order
    itself claims. Only market_routes._place_order, posting this node's
    own order, ever passes something else.
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
        auto_match_margin_stroops=max(int(auto_match_margin_stroops), 0),
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

    That last guarantee depends on the response actually having come
    from the order's own maker, and nothing before this function checks
    that: verify_fill_response only proves the response is genuinely
    signed by *somebody* (see its own docstring and
    node._handle_inbound_fill_response), because a plain relay cannot
    always tell who the right signer is and must not be made to. Anyone
    can mint a fresh, free keypair and sign an 'accepted' response
    naming somebody else's real order_id; that response is admitted and
    stored exactly like a genuine one. Trusting it here, where nothing
    downstream re-checks identity, would let a stranger shrink any
    order's advertised size to zero for the whole network, including
    the real maker's own node, for the cost of one signature. So this
    is where that check has to live: only a response whose maker_pubkey
    actually resolves to this order's maker_lapse_addr counts.
    """
    ensure_tables()
    order_row = get_order(order_id)
    if order_row is None:
        return 0
    known_sessions = {t.session_id for t in
                      Trade.select(Trade.session_id).where(Trade.order_id == order_id)}
    responses = (FillResponse.select()
                .where(FillResponse.order_id == order_id,
                       FillResponse.accepted == True))          # noqa: E712
    return sum(r.lapse_total for r in responses
              if r.session_id not in known_sessions
              and _signed_by_maker(r.maker_pubkey, order_row.maker_lapse_addr))


def _signed_by_maker(pubkey_hex, maker_lapse_addr):
    """Whether a hex pubkey resolves to the given address. False, not an
    exception, on anything unparseable: a malformed maker_pubkey should
    never reach here past verify_fill_response's own hex check, but this
    is a read path with no reason to raise over stored data."""
    try:
        pubkey = bytes.fromhex(pubkey_hex)
    except (ValueError, TypeError):
        return False
    return crypto.public_key_to_address(pubkey) == maker_lapse_addr


def remaining_ticks(order_row):
    committed = delivered_ticks(order_row.order_id) + reserved_ticks(order_row.order_id)
    return max(order_row.lapse_total - committed, 0)


def validate_fill(order_row, lapse_total):
    """Check a proposed fill size against an order's own terms. Raises
    OrderRejected with a human-readable reason if it does not fit.

    Shared by the taker's own request (market_routes._open_trade) and
    the maker's independent re-check of it (swap_engine.answer_fill_requests):
    the same three bounds apply to a fill regardless of which side
    proposes it, and a maker must never take a taker's word that its own
    order permits what a request states.
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
    """Every order still on offer here, newest first.

    Excludes an order whose remaining is below its own min_fill, not
    just one at zero: validate_fill would reject any request against
    that remainder anyway (see its own min_fill check), so listing it
    would only ever cost a taker a wasted, guaranteed-declined fill
    request. Such an order is not cancelled or otherwise touched, a
    maker still sees the true remainder on their own orders page
    (orders_by_maker); this only ever affects what a taker is offered
    to act on.
    """
    ensure_tables()
    query = (Order.select()
             .where(Order.cancelled == False,          # noqa: E712
                    Order.expiry_block > current_height)
             .order_by(Order.received_at.desc()))
    if exclude_maker:
        query = query.where(Order.maker_lapse_addr != exclude_maker)
    return [row for row in query
           if remaining_ticks(row) >= max(row.min_fill, 1)]


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
    """This maker's own orders that have at least one live fill request
    against them, regardless of whether the order itself is still open.

    Used by swap_engine.answer_fill_requests, and deliberately not
    filtered by cancelled or expiry the way orders_by_maker is: a
    request that arrived (and was accepted) before this node cancelled
    its own order still deserves completion. Cancelling withdraws what
    is unfilled, not what already has an agreed fill against it, and
    filtering this query the same way orders_by_maker is would strand
    exactly that taker.
    """
    ensure_tables()
    order_ids = [row.order_id for row in
                FillRequest.select(FillRequest.order_id).distinct()]
    if not order_ids:
        return []
    return list(Order.select()
                .where(Order.maker_lapse_addr == addr,
                       Order.order_id.in_(order_ids)))


def get_order(order_id):
    ensure_tables()
    return Order.get_or_none(Order.order_id == order_id)


def order_to_wire(row):
    """A stored Order row as the exact signed dict verify_order/store_order
    expect, for a market backfill response (see node._market_provider):
    the same shape gossip already carries, just read back out of storage
    instead of arriving fresh."""
    body = {f: getattr(row, f) for f in SIGNED_FIELDS}
    body["signature"] = row.signature
    return body


def maker_committed(maker_addr, current_height, exclude_order_id=None):
    """What this maker has promised across every one of its own live
    orders, split by which asset each direction obligates: (lapse, xlm).

    A single order's own post-time check (market_routes._place_order)
    only ever looks at that one order against the balance at that
    moment; it says nothing about a second order posted later that is
    also individually affordable but, combined with the first, is not.
    Both figures here are free for *any* node to compute, maker or not:
    the order book and this maker's LAPSE balance are both already-local
    data, and this makes no network call on its own for either return
    value (the balance to compare against is the caller's job to fetch,
    since the LAPSE one is a local read the caller already has and the
    XLM one may or may not be worth a Horizon call depending on who is
    asking - see market_routes._maker_lapse_overcommitted, which spends
    nothing extra, versus _my_orders, which is the maker's own page and
    can afford one XLM balance read same as the rest of it does).

    exclude_order_id leaves one order out of its own total, for checking
    whether an order is over-committed *given every other one already
    posted*, without that order's own ask double-counting against itself.
    """
    ensure_tables()
    lapse_committed = 0
    xlm_committed = 0
    for row in orders_by_maker(maker_addr, current_height):
        if row.order_id == exclude_order_id:
            continue
        remaining = remaining_ticks(row)
        if row.direction == "sell":
            lapse_committed += remaining
        else:
            xlm_committed += swap_mod.xlm_for_lapse(
                remaining, row.price_stroops_per_lapse)
    return lapse_committed, xlm_committed


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
    """The book as two price-sorted sides, for the aggregate stats
    (best_prices' total depth and order counts) and the Market page's
    own best-5 preview.

    Buyers are sorted best-price-first meaning highest, sellers
    lowest-first, so in both cases the top of the list is the best
    available deal for whoever is reading it.

    Unlike list_orders, this does compute remaining_ticks for every
    open order on both sides, not just a handful: a total available-to-
    buy/wanted figure is a sum across the whole side, which has no
    SQL-only shortcut the way ordering by a stored column does (see
    list_orders). Bounded in practice by MAX_ORDERS_TOTAL, and this is
    the one place in this module that cost is still paid in full; a
    market big enough for that to matter would need a maintained
    remaining/delivered figure on the Order row itself to avoid it,
    which is a real, more invasive change, not one this function can
    make on its own.
    """
    buys, sells = [], []
    for row in open_orders(current_height, exclude_maker):
        entry = _order_entry(row)
        (buys if row.direction == "buy" else sells).append(entry)
    buys.sort(key=lambda e: e["price"], reverse=True)
    sells.sort(key=lambda e: e["price"])
    return {"buys": buys, "sells": sells}


# How many rows the order book page shows at a time. The book itself has
# no such limit (MAX_ORDERS_TOTAL is the only ceiling, and that exists
# to bound storage and gossip, not display), so a genuinely active
# market needs paging rather than one page trying to render everything:
# besides the obvious readability problem, each row's maker gets a
# trust badge (market_routes.trust_badge), which can mean real chain
# I/O (see trust._verify_addr_receipts); bounding a page to this many
# rows is what keeps one page load's worst case bounded too.
BOOK_PAGE_SIZE = 20

# Ways to order one side of the book for display.
BOOK_SORTS = ("price", "amount", "recent")

# "amount" needs each candidate's remaining size to rank it, and
# remaining is not a stored column (see remaining_ticks: it is derived
# from Trade and FillResponse rows every time it is asked), so SQL
# cannot sort or page by it directly the way it can for "price" or
# "recent". Ranking it exactly across a genuinely large book would mean
# computing that derived value for every open order on that side just
# to answer one page. Bounded instead to this many of the most recently
# received candidates: comfortably more than any order book this node
# is actually likely to carry, and, unlike the book's own ceiling
# (MAX_ORDERS_TOTAL, network-wide across both sides), the cost of an
# "amount" page never grows past this number regardless of how large
# the book gets. A "largest first" page is therefore "largest among
# recent candidates", not a literal global maximum; documented rather
# than silently approximated.
AMOUNT_SORT_CANDIDATE_WINDOW = 500


def _order_entry(row):
    """One Order row as a book_depth/list_orders display entry. The one
    place remaining_ticks (a live computation, not a stored column) is
    actually paid for, so every caller here is deliberate about calling
    it only for the rows it is about to show, not every row it merely
    queried.
    """
    return {
        "order_id": row.order_id,
        "maker": row.maker_lapse_addr,
        "price": row.price_stroops_per_lapse,
        "total": row.lapse_total,
        "remaining": remaining_ticks(row),
        "min_fill": row.min_fill,
        "max_fill": row.max_fill,
        "expiry_block": row.expiry_block,
        "received_at": row.received_at,
    }


def _open_orders_query(current_height, direction, exclude_maker=None):
    """The indexed, SQL-only half of open_orders' own filter (cancelled,
    not expired, the right side, not this node's own): everything that
    can be decided without a live computation. Callers still need to
    apply open_orders' remaining-based rules (nonzero, at least
    min_fill) themselves, on whatever subset they actually fetch.
    """
    ensure_tables()
    query = (Order.select()
             .where(Order.direction == direction,
                    Order.cancelled == False,          # noqa: E712
                    Order.expiry_block > current_height))
    if exclude_maker:
        query = query.where(Order.maker_lapse_addr != exclude_maker)
    return query


def list_orders(current_height, direction, exclude_maker=None,
                sort="price", offset=0, limit=BOOK_PAGE_SIZE):
    """One page of one side of the book (direction: "buy" or "sell", the
    maker's own posted side, matching Order.direction), for the order
    book page. Returns (page_rows, total_count) so a page can say "21-40
    of 137" without a second query.

    "price" and "recent" are both stored, indexed columns, so both the
    ordering and the paging are pushed straight to SQL: the cost of a
    page never grows with how many orders are actually on the book, only
    with the page size. "amount" cannot be, see
    AMOUNT_SORT_CANDIDATE_WINDOW for why and what it does instead.
    remaining_ticks (needed either way, to show the row and to drop a
    remainder below its own min_fill, see open_orders) is computed only
    for rows actually being considered for this page, never for the
    whole side.

    total counts every order matching direction/cancelled/expiry
    regardless of sort, which can run slightly ahead of how many rows
    would actually ever display (a remainder below its own min_fill
    counts here but is filtered out of every page): computing the exact
    figure would mean the same whole-side remaining scan this function
    exists to avoid, for a number that only ever appears as "N orders on
    this side", not a promise that a full listing would show exactly N.
    """
    ensure_tables()
    if sort not in BOOK_SORTS:
        sort = "price"
    query = _open_orders_query(current_height, direction, exclude_maker)
    total = query.count()

    if sort == "amount":
        candidates = [_order_entry(r) for r in
                     query.order_by(Order.received_at.desc())
                          .limit(AMOUNT_SORT_CANDIDATE_WINDOW)]
        candidates = [e for e in candidates if e["remaining"] >= max(e["min_fill"], 1)]
        candidates.sort(key=lambda e: e["remaining"], reverse=True)
        return candidates[offset:offset + limit], total

    if sort == "recent":
        query = query.order_by(Order.received_at.desc())
    else:
        query = query.order_by(Order.price_stroops_per_lapse.asc()
                               if direction == "sell" else
                               Order.price_stroops_per_lapse.desc())

    page = [_order_entry(r) for r in query.offset(offset).limit(limit)]
    page = [e for e in page if e["remaining"] >= max(e["min_fill"], 1)]
    return page, total


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
# A starting reference for a market with neither a resting order nor a
# completed trade to derive a price from: 1 XLM per 1000 LAPSE, i.e.
# 10,000 stroops per LAPSE. Not a quote from anyone and never treated
# as one; it exists only so a brand new market shows a real number and
# a first order's price field starts from something instead of a blank
# field, and it stops mattering the instant either a real order or a
# real trade exists, since both always win over it (see
# market_routes._suggested_price and the Market page's own price
# display).
DEFAULT_PRICE_STROOPS_PER_LAPSE = 10_000
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
# fill request/response stand behind every Trade row already (see the
# fill-request section below), so nothing further is required to "count"
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
            node.view.state.get_balance(row.peer_lapse_addr), node=node)
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
# Fill requests and responses: agree before a stroop moves, not after
# ---------------------------------------------------------------------------
#
# A taker used to send a real first payment on a schedule it derived
# itself, speculatively, before the maker had looked at it at all. If the
# maker's own remaining size or exposure cap did not actually leave room
# for it, the taker's payment landed into a trade that would never be
# created, and nothing paid it back. That design also needed a second,
# separate message (a claim) just to carry the taker's address on
# whichever chain the payment did not arrive on, since a maker learning
# of a trade only from an incoming payment has no other way to learn it.
#
# A request and a response, both gossiped exactly like an order (dandelion
# stem/fluff, not a direct connection to a specific peer, so nothing here
# reveals whose IP maps to which address any more than an order already
# does), let the maker decide *before* anything moves and reserve the
# capacity it just promised the instant it accepts. Two takers racing the
# same order are now arbitrated by the one node that actually knows the
# truth about it, not by whichever one's payment happens to land first.
# The request already carries both of the taker's addresses, so no
# separate claim is needed, and step 1 is no longer forced onto the taker
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
    # The two facts a bystander needs to recompute this trade's step-1
    # deadline from public data alone, without asking anyone or trusting
    # anyone's clock: the LapseCoin height the maker saw at accept time,
    # and the confirmation depth it is holding this trade to. Both were
    # previously private, per-node bookkeeping (Trade.accepted_height,
    # Trade.confirm_depth); signing them here is what turns "abandoned"
    # from a private verdict into one anyone can check. Required (not
    # optional) on every response, accepted or not, so the schema stays
    # one shape rather than two: see swap_engine.deadline_height.
    "accepted_height", "confirm_depth",
)

# Mirrors MAX_ORDERS_PER_MAKER / MAX_ORDERS_TOTAL for the same reason:
# a taker is as free to mint keypairs as a maker is, so a per-address
# cap alone bounds nothing against one willing to do that, and the book
# itself needs its own ceiling regardless of how many addresses an
# attacker mints.
MAX_FILL_REQUESTS_PER_TAKER = 20
MAX_FILL_REQUESTS_TOTAL = 10_000
FILL_REQUEST_MAX_AGE_SECONDS = 3600

MAX_FILL_RESPONSES_TOTAL = 10_000
# Mirrors MAX_ORDERS_PER_MAKER / MAX_FILL_REQUESTS_PER_TAKER for the same
# reason, and its absence used to be a real hole: nothing else here
# stops one signer from spending the *entire* global
# MAX_FILL_RESPONSES_TOTAL budget on free, self-signed responses (see
# reserved_ticks' own history for why a response need not even answer a
# real request to be admitted), which would refuse every genuine maker's
# accept network-wide, not merely pollute one order's book. Counted by
# the signer's own pubkey, the one thing here that costs a keypair to
# change, exactly like every other per-signer cap in this module.
MAX_FILL_RESPONSES_PER_MAKER = 200
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

    Deliberately self-contained, the same division verify_order draws:
    only whether this is well-formed and genuinely signed by the
    address it names. Whether it
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
    # reasoning as market._check_admission.
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
    """Every live fill request against one order, oldest first, for the
    maker to act on.

    First-come-first-served: capacity shrinks as each one is answered
    (see market.reserved_ticks), so processing order decides who gets
    what is left when several requests together exceed it. Oldest first
    is the only ordering that cannot be gamed by a later request racing
    to be seen before an earlier, honestly-first one.
    """
    ensure_tables()
    return list(FillRequest.select()
               .where(FillRequest.order_id == order_id)
               .order_by(FillRequest.received_at))


def requests_by_taker(taker_lapse_addr):
    """Every live fill request this node itself sent, for the taker side
    to check for an answer."""
    ensure_tables()
    return list(FillRequest.select().where(
        FillRequest.taker_lapse_addr == taker_lapse_addr))


def prune_fill_requests(now=None):
    ensure_tables()
    now = time.time() if now is None else now
    cutoff = now - FILL_REQUEST_MAX_AGE_SECONDS
    return FillRequest.delete().where(FillRequest.received_at <= cutoff).execute()


def build_fill_response(request_id, order_id, session_id, lapse_total, accepted,
                        maker_pubkey_hex, accepted_height, confirm_depth,
                        increment_count=None, reason=""):
    """The unsigned body of a fill response, in canonical field order.

    increment_count must be given when accepting (it is the schedule
    length the maker is committing to) and must be omitted otherwise;
    verify_fill_response enforces that pairing on the way back in.

    accepted_height and confirm_depth are required on every response,
    declined included, so the schema is one shape: a bystander who only
    ever sees declines for an order still parses the same fields it would
    for an accept. They carry no meaning on a decline (nothing is agreed,
    nothing to anchor a deadline to) but keeping them present and
    consistently typed is simpler than a schema that changes shape by
    outcome.
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
        "accepted_height": int(accepted_height),
        "confirm_depth": int(confirm_depth),
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
    the same division verify_order draws). A taker acting on a response
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

    for field in ("accepted_height", "confirm_depth"):
        if not isinstance(resp[field], int) or isinstance(resp[field], bool):
            raise FillResponseRejected(f"{field} must be an integer")
    if resp["accepted_height"] < 0:
        raise FillResponseRejected("accepted_height must not be negative")
    if resp["confirm_depth"] < swap_mod.MIN_CONFIRM_DEPTH_FLOOR:
        raise FillResponseRejected(
            f"confirm_depth must be at least {swap_mod.MIN_CONFIRM_DEPTH_FLOOR}")

    # Last, and cheaper than the signature check that follows: same
    # reasoning as market._check_admission. Keyed on the claimed
    # maker_pubkey itself, ahead of the signature check that proves it
    # is genuinely who signed this, for the same reason _check_admission
    # runs before an order's own signature check: a maker already at its
    # own limit is refused without this node paying for a FALCON
    # verification first, and a forged pubkey is refused here for free
    # rather than after an expensive check that would have rejected it
    # anyway once _verify_fill_response_signature ran.
    _check_fill_response_admission(resp["maker_pubkey"])

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


def _check_fill_response_admission(maker_pubkey_hex):
    """Refuse before the expensive signature check, and again as the
    actual gate before a write, exactly like _check_admission and
    _check_fill_request_admission. See MAX_FILL_RESPONSES_PER_MAKER for
    why a per-signer cap has to exist here at all."""
    ensure_tables()
    if FillResponse.select().count() >= MAX_FILL_RESPONSES_TOTAL:
        raise FillResponseRejected(
            f"the response book is full (limit {MAX_FILL_RESPONSES_TOTAL})")
    live = (FillResponse.select()
            .where(FillResponse.maker_pubkey == maker_pubkey_hex)
            .count())
    if live >= MAX_FILL_RESPONSES_PER_MAKER:
        raise FillResponseRejected(
            f"this signer already has {live} responses here "
            f"(limit {MAX_FILL_RESPONSES_PER_MAKER})")


def store_fill_response(resp):
    """Persist a verified fill response. Returns False if already known."""
    ensure_tables()
    if FillResponse.get_or_none(FillResponse.request_id == resp["request_id"]) is not None:
        return False
    _check_fill_response_admission(resp["maker_pubkey"])
    FillResponse.create(
        request_id=resp["request_id"], order_id=resp["order_id"],
        session_id=resp["session_id"], lapse_total=resp["lapse_total"],
        accepted=resp["accepted"], increment_count=resp["increment_count"],
        reason=resp["reason"], maker_pubkey=resp["maker_pubkey"],
        accepted_height=resp["accepted_height"], confirm_depth=resp["confirm_depth"],
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


# ---------------------------------------------------------------------------
# Step receipts: a compact, checkable outcome, gossiped like an order
# ---------------------------------------------------------------------------
#
# Everything a receipt needs to be checked, rather than merely believed, is
# already public by the time a step could possibly settle or be missed: the
# order, the FillRequest, and the FillResponse are all signed and already
# gossiped to the whole network, and FillResponse now carries accepted_height
# and confirm_depth (see FILL_RESPONSE_SIGNED_FIELDS), so any node holding
# that trio can deterministically recompute this step's schedule, its memo,
# and its deadline_height (swap_engine.deadline_height) without asking
# anyone anything.
#
# A receipt is therefore a pointer, not a verdict. Its signature identifies
# who is reporting, for admission control and dedup, nothing more; its
# weight comes entirely from whether the tx_hash it names (for a "settled"
# claim) or the absence it names (for a "missed" one, checked against
# storage.get_tx_by_addr_and_memo / xlm.find_payment, both targeted lookups
# and never a chain scan) actually holds up. A false claim, either
# direction, is contradicted by one lookup; see
# swap_engine.verify_receipt_against_chain, which does that lookup, and
# trust.py, which only ever runs it lazily, for a specific address, when
# something actually needs that address's trust.

RECEIPT_SIGNED_FIELDS = (
    "receipt_id", "order_id", "session_id", "n", "reporter_lapse_addr",
    "addr_a", "addr_b", "asset", "from_addr", "to_addr", "amount", "memo",
    "outcome", "tx_hash", "deadline_height", "checked_at_height", "pubkey",
)

RECEIPT_OUTCOMES = ("settled", "missed")

# Same reasoning as MAX_ORDERS_PER_MAKER/MAX_ORDERS_TOTAL: a reporter is as
# free to mint keypairs as a maker is. Larger than the fill-request budget
# since a receipt, unlike a request or response, is meant to be kept
# indefinitely rather than pruned within the hour (see StepReceipt's own
# docstring), so the same node accumulates far more of these over its
# lifetime than it ever holds live requests at once.
MAX_RECEIPTS_PER_REPORTER = 500
MAX_RECEIPTS_TOTAL = 200_000


class ReceiptRejected(Exception):
    """A receipt that will not be stored or relayed, and why."""


def build_receipt(order_id, session_id, n, reporter_lapse_addr, addr_a, addr_b,
                  asset, from_addr, to_addr, amount, memo, outcome,
                  deadline_height, checked_at_height, pubkey_hex, tx_hash=""):
    """The unsigned body of a step receipt, in canonical field order.

    addr_a/addr_b are sorted by the caller (see
    swap_engine.build_receipt_for) so the same pair always occupies the
    same two fields regardless of which address is reporting or which
    leg this receipt is about.
    """
    if outcome not in RECEIPT_OUTCOMES:
        raise ValueError(f"outcome must be one of {RECEIPT_OUTCOMES}")
    return {
        "receipt_id": str(uuid.uuid4()),
        "order_id": order_id,
        "session_id": session_id,
        "n": int(n),
        "reporter_lapse_addr": reporter_lapse_addr,
        "addr_a": addr_a,
        "addr_b": addr_b,
        "asset": asset,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "amount": int(amount),
        "memo": memo,
        "outcome": outcome,
        "tx_hash": tx_hash,
        "deadline_height": int(deadline_height),
        "checked_at_height": int(checked_at_height),
        "pubkey": pubkey_hex,
    }


def _receipt_signing_bytes(receipt):
    return canonical_json({k: receipt[k] for k in RECEIPT_SIGNED_FIELDS})


def sign_receipt(receipt, keyfile_path, kek):
    signature = crypto.sign_with_keyfile(_receipt_signing_bytes(receipt), keyfile_path, kek)
    receipt["signature"] = signature.hex()
    return receipt


def verify_receipt(receipt):
    """Check a receipt arriving from the network. Raises ReceiptRejected.

    Structural and signature checks only, the same division verify_order
    and verify_fill_response draw: whether this is well-formed and
    genuinely signed by the address it names. Whether it actually holds up
    against chain data is a separate, lazy step (see
    swap_engine.verify_receipt_against_chain), never done here, since this
    is the unauthenticated surface any peer can flood and paying for two
    chain lookups per relayed item here would be exactly the "watch
    everything" cost this design avoids.
    """
    if not isinstance(receipt, dict):
        raise ReceiptRejected("not an object")

    missing = [f for f in RECEIPT_SIGNED_FIELDS if f not in receipt]
    if missing:
        raise ReceiptRejected(f"missing field(s): {missing}")
    if "signature" not in receipt:
        raise ReceiptRejected("missing signature")

    unexpected = set(receipt) - set(RECEIPT_SIGNED_FIELDS) - {"signature"}
    if unexpected:
        raise ReceiptRejected(f"unexpected field(s): {sorted(unexpected)}")

    for field in ("receipt_id", "order_id", "session_id", "reporter_lapse_addr",
                 "addr_a", "addr_b", "asset", "from_addr", "to_addr", "memo",
                 "outcome", "pubkey"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise ReceiptRejected(f"{field} must be a non-empty string")
    if not isinstance(receipt["tx_hash"], str):
        raise ReceiptRejected("tx_hash must be a string")

    for field in ("n", "amount", "deadline_height", "checked_at_height"):
        if not isinstance(receipt[field], int) or isinstance(receipt[field], bool):
            raise ReceiptRejected(f"{field} must be an integer")
    if receipt["n"] < 1:
        raise ReceiptRejected("n must be at least 1")
    if receipt["amount"] <= 0:
        raise ReceiptRejected("amount must be positive")

    if receipt["asset"] not in ("lapse", "xlm"):
        raise ReceiptRejected("asset must be 'lapse' or 'xlm'")
    if receipt["outcome"] not in RECEIPT_OUTCOMES:
        raise ReceiptRejected(f"outcome must be one of {RECEIPT_OUTCOMES}")
    if receipt["outcome"] == "settled" and not receipt["tx_hash"]:
        raise ReceiptRejected("a settled receipt must name a tx_hash")
    if receipt["outcome"] == "missed" and receipt["tx_hash"]:
        raise ReceiptRejected("a missed receipt must not name a tx_hash")
    if receipt["checked_at_height"] < receipt["deadline_height"]:
        raise ReceiptRejected("checked_at_height precedes the claimed deadline")

    if receipt["addr_a"] >= receipt["addr_b"]:
        raise ReceiptRejected("addr_a and addr_b must be sorted and distinct")
    if receipt["reporter_lapse_addr"] not in (receipt["addr_a"], receipt["addr_b"]):
        raise ReceiptRejected("reporter must be one of the two subject addresses")
    if not crypto.is_valid_address(receipt["addr_a"]):
        raise ReceiptRejected("addr_a is not a valid address")
    if not crypto.is_valid_address(receipt["addr_b"]):
        raise ReceiptRejected("addr_b is not a valid address")

    _check_receipt_admission(receipt["reporter_lapse_addr"])
    _verify_receipt_signature(receipt)
    return True


def _check_receipt_admission(reporter_lapse_addr):
    ensure_tables()
    if StepReceipt.select().count() >= MAX_RECEIPTS_TOTAL:
        raise ReceiptRejected(f"the receipt store is full (limit {MAX_RECEIPTS_TOTAL})")
    live = (StepReceipt.select()
            .where(StepReceipt.reporter_lapse_addr == reporter_lapse_addr)
            .count())
    if live >= MAX_RECEIPTS_PER_REPORTER:
        raise ReceiptRejected(
            f"reporter already has {live} receipts here "
            f"(limit {MAX_RECEIPTS_PER_REPORTER})")


def _verify_receipt_signature(receipt):
    try:
        pubkey = bytes.fromhex(receipt["pubkey"])
        signature = bytes.fromhex(receipt["signature"])
    except (ValueError, TypeError):
        raise ReceiptRejected("pubkey and signature must be hex")
    if crypto.public_key_to_address(pubkey) != receipt["reporter_lapse_addr"]:
        raise ReceiptRejected("pubkey does not match reporter_lapse_addr")
    if not crypto.verify(_receipt_signing_bytes(receipt), signature, pubkey):
        raise ReceiptRejected("signature does not verify")


def receipt_hash(receipt):
    body = {k: receipt.get(k) for k in RECEIPT_SIGNED_FIELDS}
    return crypto.sha256_hex(canonical_json(body))


def already_known_receipt(receipt):
    if not isinstance(receipt, dict):
        return False
    ensure_tables()
    rid = receipt.get("receipt_id")
    if not isinstance(rid, str):
        return False
    return StepReceipt.get_or_none(StepReceipt.receipt_id == rid) is not None


def store_receipt(receipt):
    """Persist a verified receipt. Returns False if already known."""
    ensure_tables()
    if StepReceipt.get_or_none(StepReceipt.receipt_id == receipt["receipt_id"]) is not None:
        return False
    if StepReceipt.select().count() >= MAX_RECEIPTS_TOTAL:
        raise ReceiptRejected(f"the receipt store is full (limit {MAX_RECEIPTS_TOTAL})")
    live = (StepReceipt.select()
            .where(StepReceipt.reporter_lapse_addr == receipt["reporter_lapse_addr"])
            .count())
    if live >= MAX_RECEIPTS_PER_REPORTER:
        raise ReceiptRejected(
            f"reporter already has {live} receipts here "
            f"(limit {MAX_RECEIPTS_PER_REPORTER})")
    StepReceipt.create(
        receipt_id=receipt["receipt_id"], order_id=receipt["order_id"],
        session_id=receipt["session_id"], n=receipt["n"],
        reporter_lapse_addr=receipt["reporter_lapse_addr"],
        addr_a=receipt["addr_a"], addr_b=receipt["addr_b"],
        asset=receipt["asset"], from_addr=receipt["from_addr"],
        to_addr=receipt["to_addr"], amount=receipt["amount"],
        memo=receipt["memo"], outcome=receipt["outcome"],
        tx_hash=receipt["tx_hash"], deadline_height=receipt["deadline_height"],
        checked_at_height=receipt["checked_at_height"], pubkey=receipt["pubkey"],
        signature=receipt["signature"], received_at=time.time(), verified=None)
    return True


def receipts_for_addr(addr):
    """Every receipt on file naming addr as one of its two subjects,
    verified or not. Callers that care about trust must run each
    unverified one through swap_engine.verify_receipt_against_chain
    before counting it; see trust.py."""
    ensure_tables()
    return list(StepReceipt.select()
               .where((StepReceipt.addr_a == addr) | (StepReceipt.addr_b == addr)))


def receipt_to_wire(row):
    """A stored StepReceipt row as the exact signed dict verify_receipt/
    store_receipt expect, for a market backfill response; see
    order_to_wire."""
    body = {f: getattr(row, f) for f in RECEIPT_SIGNED_FIELDS}
    body["signature"] = row.signature
    return body


def recent_orders(limit):
    ensure_tables()
    return list(Order.select()
               .where(Order.cancelled == False)          # noqa: E712
               .order_by(Order.received_at.desc())
               .limit(limit))


def recent_receipts(limit):
    ensure_tables()
    return list(StepReceipt.select()
               .order_by(StepReceipt.received_at.desc())
               .limit(limit))
