"""Durable state for the swap engine.

Written so that killing the process at any instant cannot cost anyone
money. Two rules carry that weight, and everything else here follows from
them.

The chain is the truth; this database is a cache
------------------------------------------------
Nothing here is believed about whether a payment happened. Before any
send, the engine looks for an already-settled payment carrying this
session's tag and this increment's number, and only builds a transaction
when the chain says none exists (see swap.ensure_sent). So even a
database deleted outright between the submit and the confirmation cannot
cause a double payment: the rebuilt state comes from two public ledgers,
not from these rows.

That is deliberately stronger than relying on fsync. SQLite runs here in
WAL mode with synchronous=normal, which survives a process crash but can
lose the last commits to a power cut. Rather than raise the durability
setting for the whole node and pay for it on every block, the recovery
path is made not to need it.

Intent is written before the action, never after
------------------------------------------------
A signed transaction is persisted before it is submitted, never the other
way round. The envelope carries its own sequence number (Stellar) or
nonce (LapseCoin), so re-submitting that exact stored envelope applies at
most once. Recovery therefore re-sends what is on disk instead of
building something new, and building something new is the only way to pay
twice.

The window that remains is the one between signing and the row being
committed, and in that window nothing has been sent, so the worst case is
a transaction that never existed.
"""

import logging

from peewee import (
    Model, IntegerField, TextField, FloatField, BooleanField,
)

from storage import db

log = logging.getLogger("ec.trade_storage")


class _TradeBase(Model):
    class Meta:
        database = db


# ---------------------------------------------------------------------------
# Leg states
# ---------------------------------------------------------------------------

# Nothing built. The only state from which building is allowed.
LEG_PENDING = "pending"
# Signed and on disk, submission not yet known to have happened. The
# crash-critical state: on restart the chain decides whether this became
# SUBMITTED or is still safe to re-send.
LEG_INTENT = "intent"
# Handed to the network. Awaiting settlement at the required depth.
LEG_SUBMITTED = "submitted"
# Verified on chain. Terminal and never revisited except by a reorg on the
# LapseCoin side, which un-sets it (see swap.reconcile).
LEG_SETTLED = "settled"
# Provably unable to apply: its sequence or nonce was consumed by another
# transaction, or it expired. Must be rebuilt, never retried.
LEG_DEAD = "dead"

ACTIVE_LEG_STATES = (LEG_PENDING, LEG_INTENT, LEG_SUBMITTED)


# ---------------------------------------------------------------------------
# Trade states
# ---------------------------------------------------------------------------

TRADE_ACTIVE = "active"
# Deadline missed, but not blamed. A counterparty restarting a node looks
# exactly like this, so it carries no penalty and resumes on its own.
TRADE_STALLED = "stalled"
TRADE_COMPLETED = "completed"
# Deadline missed by a wide margin with evidence the counterparty was
# reachable throughout. Only this slashes; see trust.record_abandonment.
TRADE_ABANDONED = "abandoned"
# Ended early by agreement or by the user, with delivered increments
# standing.
TRADE_CLOSED = "closed"


class Order(_TradeBase):
    """A signed order, gossiped rather than written on chain.

    Stored by every node that receives it, so the book is local and needs
    no server. The proposer's signature is what makes a row trustworthy;
    `verified` records that it was checked on arrival so the check is not
    repeated on every page render.
    """
    order_id = TextField(primary_key=True)
    maker_lapse_addr = TextField(index=True)
    maker_xlm_addr = TextField()
    # What the maker does with LAPSE: "sell" gives LAPSE for XLM.
    direction = TextField()
    lapse_total = IntegerField()          # ticks
    # Stroops of XLM per whole LAPSE. Integer so a quote is exact and two
    # nodes never disagree about a price by a rounding step.
    price_stroops_per_lapse = IntegerField()
    min_fill = IntegerField(default=0)    # ticks
    max_fill = IntegerField(default=0)    # ticks, 0 means the whole order
    expiry_block = IntegerField()
    pubkey = TextField()
    signature = TextField()
    created_at = FloatField()
    received_at = FloatField()
    cancelled = BooleanField(default=False)
    verified = BooleanField(default=False)


class Trade(_TradeBase):
    """One agreed fill of an order, executed as a run of increments."""
    session_id = TextField(primary_key=True)
    order_id = TextField(index=True)

    # Which side this node is. The maker posted the order; the taker
    # accepted it. Everything about who sends first keys off this.
    role = TextField()                     # "maker" | "taker"

    my_lapse_addr = TextField()
    my_xlm_addr = TextField()
    peer_lapse_addr = TextField(index=True)
    peer_xlm_addr = TextField()

    # What this node sends across the whole trade. The other asset is what
    # it receives, so one field decides both.
    i_send = TextField()                   # "lapse" | "xlm"
    lapse_total = IntegerField()           # ticks, the LAPSE side of the fill
    xlm_total = IntegerField()             # stroops, the XLM side

    increment_count = IntegerField()
    # Snapshotted when the trade starts rather than read live, so changing
    # the setting midway cannot retroactively un-settle a delivered
    # increment or move the goalposts on a counterparty.
    confirm_depth = IntegerField()

    status = TextField(default=TRADE_ACTIVE, index=True)
    created_at = FloatField()
    updated_at = FloatField()
    # Set when the trade first misses a deadline, cleared when it recovers.
    # Distance from here is what separates a slow peer from an absent one.
    stalled_since = FloatField(default=0.0)
    note = TextField(default="")


class Increment(_TradeBase):
    """One step of a trade: this node sends one leg and receives the other.

    Both legs are tracked separately because they settle on different
    chains with different rules, and because which one moves first
    alternates (see swap.first_mover). An increment is done only when both
    are settled.
    """
    id = TextField(primary_key=True)       # "<session_id>:<n>"
    session_id = TextField(index=True)
    n = IntegerField()

    lapse_amount = IntegerField()          # ticks moving this step
    xlm_amount = IntegerField()            # stroops moving this step

    # True when this node sends before the counterparty does. Whoever moves
    # first carries this step's risk, so it alternates across increments
    # rather than resting on one party for the whole trade.
    i_move_first = BooleanField()

    # --- the leg this node sends -------------------------------------
    out_state = TextField(default=LEG_PENDING)
    # The signed transaction, written before submission. This exact blob is
    # what recovery re-submits; rebuilding instead is what would pay twice.
    out_envelope = TextField(default="")
    out_tx_hash = TextField(default="", index=True)
    # Sequence (Stellar) or nonce (LapseCoin) baked into out_envelope, kept
    # so recovery can tell "already applied" from "this can never apply"
    # without parsing the blob back out.
    out_seq = IntegerField(default=0)
    out_submitted_at = FloatField(default=0.0)
    out_settled_at = FloatField(default=0.0)
    out_detail = TextField(default="")

    # --- the leg this node receives ----------------------------------
    in_state = TextField(default=LEG_PENDING)
    in_tx_hash = TextField(default="")
    in_settled_at = FloatField(default=0.0)

    created_at = FloatField()
    # When this step stops being merely slow. Generous by design: a peer
    # reloading a long chain at startup must never look like a defector.
    deadline_at = FloatField(default=0.0)


class PeerRecord(_TradeBase):
    """What this node has observed about a counterparty, for trust.

    Independent per node and derived from public data, so nothing here
    needs to be agreed with anyone. Two nodes may hold different numbers
    and both be right about what they saw.
    """
    lapse_addr = TextField(primary_key=True)
    completed_count = IntegerField(default=0)
    completed_lapse = IntegerField(default=0)   # ticks, lifetime
    abandoned_count = IntegerField(default=0)
    last_completed_at = FloatField(default=0.0)
    last_abandoned_at = FloatField(default=0.0)
    # Evidence for a slash, kept so the number is explainable rather than
    # a verdict with no stated reason.
    last_abandon_session = TextField(default="")


class Claim(_TradeBase):
    """A taker's signed announcement of a fill it is about to pay for.

    Exists for exactly one reason: a maker discovering a trade from an
    incoming payment (see swap_engine.discover_trades) can read the
    sender's address on whichever chain the payment arrived on, but has
    no way to learn the taker's address on the *other* chain, since
    nothing links a LapseCoin key to a Stellar one and nothing should
    (see trust.mutual_scores on why that link is not published for its
    own sake either). Stellar's memo is 28 bytes, already spent on the
    order and session reference, with no room left for a second address.

    So the taker also gossips this, exactly the way an order is gossiped:
    signed with the same LapseCoin key that controls taker_lapse_addr, so
    a relay cannot forge one and nobody can claim an address they do not
    control. It proves exactly that and nothing more. A maker matching an
    incoming payment to a claim still independently re-derives the whole
    schedule and checks it against its own exposure cap before creating
    anything (see swap_engine.discover_trades); the claim only ever
    supplies an address and a stated fill size, both a claim to be
    checked rather than a promise, same as an order.
    """
    session_id = TextField(primary_key=True)
    order_id = TextField(index=True)
    taker_lapse_addr = TextField()
    taker_xlm_addr = TextField()
    # The fill size the taker is committing to. xlm_total is not carried
    # separately: it is deterministic from this and the order's own
    # price, so conveying it too would just be a second number that could
    # disagree with the first instead of one that cannot.
    lapse_total = IntegerField()
    increment_count = IntegerField()
    pubkey = TextField()
    signature = TextField()
    received_at = FloatField()


TRADE_TABLES = [Order, Trade, Increment, PeerRecord, Claim]

_initialised = False


def init_tables():
    """Create the swap tables if absent. Safe to call repeatedly.

    Separate from the chain schema's migration path on purpose: these
    tables hold no consensus data, so a node that has never traded simply
    has empty ones and nothing about the chain depends on them.
    """
    global _initialised
    db.create_tables(TRADE_TABLES, safe=True)
    _initialised = True


def ensure_tables():
    if not _initialised:
        init_tables()
