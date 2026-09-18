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
from crypto import canonical_json
from trade_storage import Order, Trade, Increment, ensure_tables, LEG_SETTLED

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

    _verify_signature(order)
    return True


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


def remaining_ticks(order_row):
    return max(order_row.lapse_total - delivered_ticks(order_row.order_id), 0)


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


def get_order(order_id):
    ensure_tables()
    return Order.get_or_none(Order.order_id == order_id)


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
