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
    # The LapseCoin height this side saw when the trade was accepted. On
    # the maker's row this is also the value carried in the signed
    # FillResponse (see market.FILL_RESPONSE_SIGNED_FIELDS), which is what
    # makes step 1's deadline something any observer can recompute from
    # public data rather than from this node's own clock: see
    # swap_engine.deadline_height.
    accepted_height = IntegerField(default=0)

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
    # When this step stops being merely slow, by this node's own clock.
    # This is a local UX signal only (it drives the "stalled" badge and
    # nothing else): generous by design, since a peer reloading a long
    # chain at startup must never look like a defector.
    deadline_at = FloatField(default=0.0)
    # The LapseCoin height at which BOTH legs of this step were observed
    # settled. 0 until then. This is what a deterministic, chain-anchored
    # deadline for the *next* step is built from (see
    # swap_engine.deadline_height), so the trust-affecting verdict is a
    # fact anyone can recompute rather than this node's private clock.
    completed_height = IntegerField(default=0)
    # This step's own deadline, in the same terms: known up front for step
    # 1 (Trade.accepted_height + a fixed number of blocks) and filled in
    # once step n-1's completed_height is known for every step after it.
    # 0 means "not yet computable", never "no deadline".
    deadline_height = IntegerField(default=0)


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


class FillRequest(_TradeBase):
    """A taker's proposal to fill part of an order, sent and answered
    before either side risks a single stroop.

    Supersedes the old pay-first-and-hope design: a taker used to send a
    real first payment speculatively and the maker discovered the trade
    afterward from the chain, which meant a stale or racing view of an
    order's remaining size could cost a taker a real payment into a fill
    the maker was always going to refuse (see market.reserved_ticks'
    history). Here the maker decides explicitly, before anything moves,
    and reserves the capacity the instant it accepts (see FillResponse),
    so two takers racing the same order are arbitrated by the one node
    that actually knows the truth about it rather than by whoever pays
    first.

    Signed with the taker's LapseCoin key and gossiped exactly like an
    order, so a relay cannot forge one and nobody can propose a fill
    from an address they do not control. session_id is generated by the
    taker up front (see swap.new_session_id) so both sides already agree
    on it without a further round trip once accepted.
    """
    request_id = TextField(primary_key=True)
    order_id = TextField(index=True)
    session_id = TextField(unique=True)
    taker_lapse_addr = TextField()
    taker_xlm_addr = TextField()
    lapse_total = IntegerField()
    pubkey = TextField()
    signature = TextField()
    received_at = FloatField()


class FillResponse(_TradeBase):
    """The maker's signed answer to one FillRequest, gossiped exactly
    like the request was.

    Self-contained on purpose: lapse_total and order_id are echoed here
    rather than requiring a reader to still have the original request on
    hand, since a request this node relayed but does not itself care
    about is not something it keeps around once its own claim book's
    aging rules would drop it. A node with only the response can still
    tell how much of the order it names is now spoken for (see
    market.reserved_ticks), which is what makes an accepted response,
    not merely a pending request, the number remaining_ticks trusts.

    An accepted response is the maker's own act of committing to a
    schedule; it always carries increment_count, so nothing about how
    many steps a trade will run is decided anywhere but here, once, by
    the side actually accepting the exposure.
    """
    request_id = TextField(primary_key=True)
    order_id = TextField(index=True)
    session_id = TextField()
    lapse_total = IntegerField()
    accepted = BooleanField()
    increment_count = IntegerField(null=True)   # set only when accepted
    reason = TextField(default="")
    maker_pubkey = TextField()
    # The maker's own signed height and confirm_depth at accept time: the
    # public anchor a bystander recomputes every step's deadline_height
    # from (see swap_engine.deadline_height). Present on every response,
    # declined included, so the schema is one shape regardless of outcome.
    accepted_height = IntegerField(default=0)
    confirm_depth = IntegerField(default=0)
    signature = TextField()
    received_at = FloatField()


class StepReceipt(_TradeBase):
    """A compact, signed claim that one step of one trade settled or was
    missed, gossiped exactly like an order so any node can eventually see
    it, verify it, and fold it into trust for an address it never itself
    traded with.

    The signature identifies who is reporting, for admission control and
    dedup, nothing more: the claim's actual weight comes from being
    checkable against public chain data, not from trusting the reporter.
    A false "settled" claim fails the tx-hash lookup it points to; a false
    "missed" claim is contradicted the moment a checker finds the payment
    it says does not exist. See swap_engine.verify_receipt_against_chain.

    Deliberately not pruned on FillRequest/FillResponse's short timer:
    unlike those, which exist only to arbitrate a brief window of live
    capacity, a receipt is the reputation record itself and is meant to
    outlive the trade it describes.
    """
    receipt_id = TextField(primary_key=True)
    order_id = TextField(index=True)
    session_id = TextField(index=True)
    n = IntegerField()
    # Whoever is reporting: the party who can see the outcome directly,
    # i.e. the one who sent the leg (for "settled") or the one still
    # owed it (for "missed"). Signed with this address's LapseCoin key.
    reporter_lapse_addr = TextField(index=True)
    # The two addresses this receipt is *about*, sorted so a given pair
    # always lands in the same two columns regardless of which one sent
    # this particular leg; both indexed so a trust lookup for either
    # address is a plain indexed query, not a string scan.
    addr_a = TextField(index=True)
    addr_b = TextField(index=True)
    asset = TextField()                  # "lapse" | "xlm"
    from_addr = TextField()
    to_addr = TextField()
    amount = IntegerField()
    memo = TextField()
    outcome = TextField()                # "settled" | "missed"
    tx_hash = TextField(default="")      # set when outcome == "settled"
    # The LapseCoin height the leg was due by, and the height at which a
    # "missed" claim asserts it still had not arrived. Both public and
    # independently recomputable; see swap_engine.deadline_height.
    deadline_height = IntegerField(default=0)
    checked_at_height = IntegerField(default=0)
    pubkey = TextField()
    signature = TextField()
    received_at = FloatField()
    # This node's own re-check of the claim against the chain(s): None
    # until looked at, then True/False. Lazy on purpose, see trust.py:
    # a receipt sits here unverified, at the cost of one row, until
    # something actually needs this specific address's trust.
    verified = BooleanField(null=True, default=None)


TRADE_TABLES = [Order, Trade, Increment, PeerRecord, FillRequest, FillResponse,
                StepReceipt]

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
