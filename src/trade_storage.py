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
#
# There used to be a further status, TRADE_ABANDONED, entered once a
# stall ran long enough to slash standing and reversed by a dedicated
# recheck pass if a late payment settled it after all. That was a stored
# verdict needing active maintenance to keep correct. A trade now simply
# stays STALLED for as long as it is missing a leg, however long that
# is: the ordinary advance loop keeps retrying it (nothing special about
# "stalled for an hour" versus "stalled for a minute"), and whether it
# currently counts against the peer's standing is computed fresh every
# time from these same rows (see swap_engine.is_delinquent), never
# written down. See trust.py's module docstring for why that matters.
TRADE_STALLED = "stalled"
TRADE_COMPLETED = "completed"


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
    # How much worse a rate this node will privately accept, beyond the
    # price above, when swap_engine.auto_match_orders goes looking for an
    # existing counter-order to match this one against on its own (see
    # that function for the full crossing rule). Deliberately absent from
    # market.SIGNED_FIELDS: it is never signed, never part of the wire
    # format order_to_wire builds, and never read off an inbound order
    # (market.store_order only ever receives it from this node's own
    # posting flow, market_routes._place_order). A node's copy of
    # somebody else's order always carries the column's bare default, 0,
    # which is correct and harmless: nothing here is ever consulted for
    # an order this node did not itself make. The whole point is that no
    # message this node ever sends carries this number, so no peer can
    # ever learn this node would have accepted less than it asked for.
    auto_match_margin_stroops = IntegerField(default=0)


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

    xlm_total/direction/maker_xlm_addr make an ACCEPTED response, paired
    with its own FillRequest (which already carries the taker's two
    addresses and lapse_total), a fully self-sufficient record of one
    trade's terms - nothing about the Order that produced it needs to
    survive for anyone to independently recompute the whole schedule,
    every step's memo, and every step's deadline_height (see
    swap.build_schedule, swap.session_tag, swap_engine.deadline_height).
    This is what let step-receipt gossip be removed outright (see
    market.py's former "step receipts" section, and prune_fill_responses/
    prune_fill_requests, which now keep an ACCEPTED pair indefinitely
    instead of pruning it within the hour): a receipt only ever existed
    to hand a third party a pointer to data that would otherwise have
    been thrown away, and its own verdict was always independently
    recomputed from the chain regardless of what the reporter claimed
    (see swap_engine.verify_trade_against_chain). Once the terms
    themselves are what's kept, the pointer, and the entire signed-
    claim-plus-admission-control apparatus around it, has nothing left
    to do.
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
    # The order's own terms, echoed here at accept time for the same
    # reason lapse_total already was: an order can expire and be pruned
    # (market.prune_expired) long before anyone needs to recompute an
    # old trade's schedule from what's left in this table.
    xlm_total = IntegerField(default=0)
    direction = TextField(default="")           # the ORDER's own "buy"/"sell"
    maker_xlm_addr = TextField(default="")
    # Which side opens the first increment (see swap.opening_mover), fixed
    # at accept time and signed here rather than left for a reader to
    # recompute later: trust scores move as new trades settle, so a
    # bystander re-deriving this from "current" trust could disagree with
    # what the two parties actually used to build their schedule.
    maker_opens = BooleanField(default=True)
    signature = TextField()
    received_at = FloatField()


TRADE_TABLES = [Order, Trade, Increment, FillRequest, FillResponse]

_initialised = False


def _migrate_fill_response_trade_terms():
    """Backfill FillResponse's xlm_total/direction/maker_xlm_addr for a
    database created before they existed. create_tables(safe=True)
    below only creates missing tables, not missing columns on one that
    already exists, so an upgrade needs this the same way storage.py's
    own _migrate_addrindex_memo does for the chain database. A response
    from before this column existed was pruned within the hour under
    the old rules regardless, so there is nothing meaningful to backfill
    into it - defaulting is exactly as informative as the row already
    was by the time anyone could ask about it again.
    """
    cols = {r[1] for r in db.execute_sql("PRAGMA table_info(fillresponse)").fetchall()}
    if "xlm_total" not in cols:
        db.execute_sql("ALTER TABLE fillresponse ADD COLUMN "
                       "xlm_total INTEGER NOT NULL DEFAULT 0")
        db.execute_sql("ALTER TABLE fillresponse ADD COLUMN "
                       "direction TEXT NOT NULL DEFAULT ''")
        db.execute_sql("ALTER TABLE fillresponse ADD COLUMN "
                       "maker_xlm_addr TEXT NOT NULL DEFAULT ''")
    if "maker_opens" not in cols:
        db.execute_sql("ALTER TABLE fillresponse ADD COLUMN "
                       "maker_opens INTEGER NOT NULL DEFAULT 1")


def _migrate_order_auto_match_margin():
    """Backfill Order.auto_match_margin_stroops for a database created
    before that column existed. Existing rows get 0, "match only at
    least as good as my own posted price", which is exactly what a
    fresh order (and every order this node did not itself post) already
    carries, so nothing changes for anyone who upgrades without
    revisiting their open orders."""
    cols = {r[1] for r in db.execute_sql("PRAGMA table_info(\"order\")").fetchall()}
    if "auto_match_margin_stroops" not in cols:
        db.execute_sql("ALTER TABLE \"order\" ADD COLUMN "
                       "auto_match_margin_stroops INTEGER NOT NULL DEFAULT 0")


def init_tables():
    """Create the swap tables if absent. Safe to call repeatedly.

    Separate from the chain schema's migration path on purpose: these
    tables hold no consensus data, so a node that has never traded simply
    has empty ones and nothing about the chain depends on them.
    """
    global _initialised
    db.create_tables(TRADE_TABLES, safe=True)
    _migrate_fill_response_trade_terms()
    _migrate_order_auto_match_margin()
    _initialised = True


def ensure_tables():
    if not _initialised:
        init_tables()
