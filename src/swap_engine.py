"""Executing a swap: sends, confirmations, recovery.

swap.py decides the amounts; this drives them. The chains are reached
through two small adapters rather than directly, so every path here
including the ones that only happen after a crash can be tested against
fakes instead of against mainnet.

The rule that makes a restart safe
----------------------------------
Before building any transaction, ask the chain whether this exact step
was already paid. Only if it says no is anything built, and what gets
built is written down before it is sent.

That ordering is the whole design. It means the worst a crash can do is
leave a transaction that was signed and never sent, which costs nothing.
Rebuilding a payment that already went out is the only way to pay twice,
and the chain check plus the stored envelope between them make that
unreachable: if the payment settled the chain says so, and if it is still
in flight the stored envelope carries the same sequence number, so
re-sending it applies at most once.

Losing this database entirely is therefore survivable. State rebuilds
from two public ledgers, which is why nothing here treats a stored row as
evidence that money moved.

Confirmation, and why one block is not enough
---------------------------------------------
Stellar is final on inclusion, so an XLM leg is settled or it is not.

LapseCoin is not. Its draw window hands a height to whichever candidate
proves the better VDF result, so the tip changes hands as a matter of
routine (node._reorg_to_sibling), and the codebase already treats a
one-block reorg as ordinary traffic rather than an incident
(node.REORG_NOTABLE_DEPTH). A leg confirmed at depth one can therefore be
un-confirmed by the chain working exactly as designed. Two is the floor
here, and a settled leg is re-checked on every pass so a reorg that does
take one back is noticed rather than assumed away.

Blame
-----
A missed deadline is not evidence of anything. A node restarting, a slow
block, or a peer's network dropping all look identical to abandonment
from outside, so a missed deadline only stalls a trade, and a stalled
trade resumes by itself. Blame needs the deadline missed by a wide margin
and the peer observably reachable throughout. Getting this wrong in the
other direction costs an honest user their standing for owning an
unreliable connection, which is worse than waiting longer to punish a
real defector.
"""

import logging
import threading
import time

import market as market_mod
import swap
import trust as trust_mod
import xlm as xlm_mod
from trade_storage import (
    Increment, Trade, StepReceipt,
    LEG_PENDING, LEG_INTENT, LEG_SUBMITTED, LEG_SETTLED, LEG_DEAD,
    TRADE_ACTIVE, TRADE_COMPLETED, TRADE_STALLED,
    ensure_tables,
)

log = logging.getLogger("ec.swap_engine")

# Depth at which a LapseCoin leg counts as settled. Two, not one: see the
# module docstring. A user may raise this and not lower it. Sourced from
# swap.py, the one place this floor is defined, since market.py also
# needs it (to validate a signed confirm_depth) and cannot import this
# module without a cycle.
MIN_CONFIRM_DEPTH = swap.MIN_CONFIRM_DEPTH_FLOOR
DEFAULT_CONFIRM_DEPTH = swap.MIN_CONFIRM_DEPTH_FLOOR

# How long one step may take before the trade is marked stalled. Built
# from the chain's own pace rather than a fixed number of seconds, so it
# tracks whatever the network is actually doing.
#
# A step costs one confirmation window on the slow side plus a few seconds
# on the fast one, and the multiplier is deliberately generous: the cost
# of waiting too long is a slow trade, and the cost of not waiting long
# enough is blaming somebody whose node was restarting.
STEP_TIMEOUT_MULTIPLIER = 6

# Past this, a stall starts counting against the peer's standing (see
# is_delinquent). Roughly an hour on a two-minute chain. Long on purpose:
# reloading a long chain at startup is slow, and that must never read as
# defection.
#
# Expressed in seconds here only because an hour is an easier number to
# set and sanity-check than "30 blocks"; ABANDON_AFTER_BLOCKS below,
# derived from it once, is the number is_delinquent actually compares
# against a public height with, since a wall-clock second is this node's
# own and a block is everyone's.
ABANDON_AFTER_SECONDS = 3600
ABANDON_AFTER_BLOCKS = ABANDON_AFTER_SECONDS // 120


class SwapError(Exception):
    """A step could not be advanced. Usually transient."""


class Unreachable(SwapError):
    """A chain could not be consulted, so nothing about it is known.

    Kept distinct from a failure on purpose. Treating "could not ask" as
    "did not happen" is what strands a payment that is about to settle,
    and treating it as "did happen" is worse.
    """


# ---------------------------------------------------------------------------
# Chain adapters
# ---------------------------------------------------------------------------

class LapseAdapter:
    """The LapseCoin side, over a running node.

    Reads go through the node's published view, which is a consistent
    snapshot, so a chain that moves mid-pass cannot produce a half-read
    answer.
    """

    asset = "lapse"

    def __init__(self, node):
        self.node = node

    def balance(self, addr):
        return self.node.view.state.get_balance(addr)

    def height(self):
        """The current LapseCoin height: the shared clock every step's
        deadline is anchored to, on either chain a leg happens to move on
        (see deadline_height)."""
        return self.node.view.height

    def account_exists(self, addr):
        """Always True: a LapseCoin address can receive a payment
        whether or not it has ever held a balance, unlike Stellar's
        accounts. Present so callers can treat both adapters the same
        way rather than special-casing which chain they are on."""
        return True

    def find_payment(self, from_addr, to_addr, memo, min_amount):
        """A settled or pending payment matching every agreed term.

        Returns (tx_hash, confirmations) or None. Checks the sender, the
        recipient, the memo tying it to this step, and the amount, because
        this is the call that decides whether money moved; matching on
        fewer terms is how an unrelated transfer gets counted as a step.

        The mempool is searched too, at zero confirmations, so a payment
        this node just made is found rather than rebuilt.
        """
        import tx as tx_mod

        for candidate in self.node.mempool.all_txs():
            if self._matches(candidate, from_addr, to_addr, memo, min_amount):
                return tx_mod.tx_hash(candidate), 0

        chain = self.node.view.chain
        tip = chain[-1]["height"]
        # Looks up by the exact memo (this step's own session tag) rather
        # than walking every transaction from_addr has ever made: this
        # call already knows precisely which payment it is checking for
        # (see storage.Storage.get_tx_by_addr_and_memo).
        for block_height, tx_hash in self.node.storage.get_tx_by_addr_and_memo(from_addr, memo):
            if not 0 <= block_height < len(chain):
                continue
            for candidate in chain[block_height]["transactions"]:
                if tx_mod.tx_hash(candidate) != tx_hash:
                    continue
                if self._matches(candidate, from_addr, to_addr, memo, min_amount):
                    return tx_hash, tip - block_height + 1
        return None

    @staticmethod
    def _matches(candidate, from_addr, to_addr, memo, min_amount):
        if candidate.get("from") != from_addr:
            return False
        if candidate.get("memo") != memo:
            return False
        paid = sum(out["amount"] for out in candidate.get("outputs", [])
                   if out.get("to") == to_addr)
        return paid >= min_amount

    def confirmations(self, tx_hash):
        """Depth of a transaction, 0 while unconfirmed, None if unknown.

        None is the answer that matters after a reorg: a transaction that
        was confirmed and is now nowhere reports None rather than a stale
        depth, which is what lets a settled leg be taken back.
        """
        if self.node.mempool.get(tx_hash) is not None:
            return 0
        block_height = self.node.storage.get_tx_height(tx_hash)
        if block_height is None:
            return None
        chain = self.node.view.chain
        if not 0 <= block_height < len(chain):
            return None
        return chain[-1]["height"] - block_height + 1

    def build(self, to_addr, amount, memo, kek):
        """Sign a payment without sending it.

        Returned rather than submitted so the caller can persist it first.
        """
        tx_dict, _fee = self.node.build_and_sign_tx(
            [{"to": to_addr, "amount": amount}], fee=0, kek=kek, memo=memo)
        import tx as tx_mod
        return tx_dict, tx_mod.tx_hash(tx_dict), tx_dict["nonce"]

    def submit(self, tx_dict):
        ok, result = self.node.submit_tx_from_api(tx_dict)
        return ok, result

    def sequence_consumed(self, addr, seq):
        """Whether this nonce is spent, so a stored envelope can never apply.

        Distinguishes a transaction still waiting from one that can only
        be rebuilt; retrying the latter forever is a trade that silently
        never progresses.
        """
        return self.node.view.state.get_nonce(addr) >= seq

    def forget_sequence(self, addr):
        """No-op: a nonce always comes fresh from local node state, so
        there is nothing cached here for a dead envelope to invalidate."""


class SequenceAllocator:
    """Hands out Stellar sequence numbers without re-reading Horizon.

    Horizon's account endpoint is only eventually consistent with a
    submission this same process just made: reading it again immediately
    after, for a second trade sharing the same address, can still return
    the pre-submission value. Two builds would then embed the same
    sequence and one submission would fail with tx_bad_seq for no reason
    but a read race against ourselves, on a wallet nothing else was
    touching.

    The fix is to never ask Horizon twice for one address inside a
    process's own bookkeeping: read it once, then hand out successive
    values from memory. That is correct exactly because the engine
    submits each leg before moving on to the next trade, so by the time
    a second allocation for the same address happens, the first one's
    transaction has already been accepted or has already failed; either
    way Horizon is no longer the question.

    It is not a substitute for the chain's own answer, only a way to
    avoid asking a question whose answer is momentarily unreliable.
    Something outside this process (a manual withdrawal, another
    instance of this same wallet) can still spend a sequence number this
    allocator does not know about; that is what reset() is for, called
    once the chain proves a stored envelope can never apply (see
    Engine._resend).
    """

    def __init__(self):
        self._next = {}

    def allocate(self, addr):
        if addr not in self._next:
            self._next[addr] = xlm_mod.get_sequence(addr)
        seq = self._next[addr]
        self._next[addr] = seq + 1
        return seq

    def reset(self, addr):
        self._next.pop(addr, None)


class XLMAdapter:
    """The Stellar side. Thin: xlm.py already does the work."""

    asset = "xlm"

    def __init__(self, keyfile_path):
        self.keyfile_path = keyfile_path
        self._sequences = SequenceAllocator()

    def balance(self, addr):
        return xlm_mod.get_spendable_stroops(addr)

    def account_exists(self, addr):
        """Whether the destination can receive a plain payment at all.

        A Stellar address with no account behind it (never funded) can
        only be reached by a create-account operation, never a plain
        payment; see ensure_sent's use of this before building an XLM
        leg.
        """
        try:
            return xlm_mod.account_exists(addr)
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e

    def find_payment(self, from_addr, to_addr, memo, min_amount):
        try:
            tx_hash = xlm_mod.find_payment(to_addr, memo, min_amount,
                                           from_address=from_addr)
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e
        if tx_hash is None:
            return None
        # Final on inclusion, so anything found is fully settled. Reported
        # as a large depth so one confirmation rule covers both chains
        # without the caller special-casing which one it is looking at.
        return tx_hash, 10**9

    def confirmations(self, tx_hash):
        try:
            return 10**9 if xlm_mod.transaction_succeeded(tx_hash) else None
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e

    def build(self, to_addr, amount, memo, seed, create_account=False):
        try:
            sequence = self._sequences.allocate(
                xlm_mod.Keypair.from_secret(seed).public_key)
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e
        if create_account:
            xdr, tx_hash = xlm_mod.build_create_account(
                seed, to_addr, amount, memo, sequence)
        else:
            xdr, tx_hash = xlm_mod.build_payment(
                seed, to_addr, amount, memo, sequence)
        return xdr, tx_hash, sequence

    def submit(self, xdr):
        try:
            ok, _tx_hash, detail = xlm_mod.submit_envelope(xdr)
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e
        return ok, detail

    def sequence_consumed(self, addr, seq):
        try:
            return xlm_mod.get_sequence(addr) >= seq
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e
        except xlm_mod.XLMError:
            return False

    def forget_sequence(self, addr):
        self._sequences.reset(addr)


# ---------------------------------------------------------------------------
# Step timing
# ---------------------------------------------------------------------------

def step_timeout_seconds(confirm_depth, block_seconds=120):
    """How long a step may take before the trade is called stalled.

    Scaled off the confirmation window the trade actually uses, so raising
    the depth lengthens the patience to match instead of turning every
    step into a false stall.

    Wall-clock, and used only for the local "stalled" UX badge
    (_update_timing). It is not what decides abandonment; see
    step_timeout_blocks and deadline_height for the chain-anchored number
    that actually governs blame.
    """
    return confirm_depth * block_seconds * STEP_TIMEOUT_MULTIPLIER


def step_timeout_blocks(confirm_depth):
    """The same patience as step_timeout_seconds, in LapseCoin blocks.

    Exact, not approximate: step_timeout_seconds is confirm_depth *
    120 * STEP_TIMEOUT_MULTIPLIER, and a LapseCoin block is 120 seconds by
    protocol, so dividing back out leaves confirm_depth *
    STEP_TIMEOUT_MULTIPLIER blocks with nothing left over to round.

    This, not a wall-clock timestamp, is what a deadline is built from
    (see deadline_height): a number of blocks is public and the same for
    every observer, where "now + N seconds" is whatever this node's own
    clock happened to read when it computed it.
    """
    return confirm_depth * STEP_TIMEOUT_MULTIPLIER


def deadline_height(base_height, confirm_depth):
    """The LapseCoin height by which a step is due, given the height its
    clock started from (the trade's accepted_height for step 1, or the
    previous step's completed_height for every step after it).

    A pure function of two public numbers (a FillResponse's signed
    accepted_height and confirm_depth, or a StepReceipt's own
    completed_height), so any node holding the relevant gossiped messages
    computes the identical deadline this node does, with nothing asked of
    anyone's clock or word.
    """
    return base_height + step_timeout_blocks(confirm_depth)


def is_delinquent(trade, current_height):
    """Whether this trade currently counts against its counterparty's
    standing: this node paid a step, by the chain's own clock the
    counterparty has had ABANDON_AFTER_BLOCKS past that step's own
    deadline_height to reciprocate, and still has not.

    A pure function of Increment rows and a height, both public facts,
    recomputed fresh every time this is asked (see trust.local_tally)
    rather than decided once and written down. That is the entire point:
    there used to be a TRADE_ABANDONED status set here and a separate
    recheck_abandoned pass to reverse it if a late payment arrived, which
    is maintenance a stored verdict needs and a computed one does not.
    The moment the missing leg settles, in_state flips to LEG_SETTLED
    (the ordinary advance loop already does that, see _update_timing) and
    this simply returns False on the very next call, nothing to reverse.

    Only ever true for a still-STALLED trade: one that finished, however
    late, is TRADE_COMPLETED and this never runs against it again.

    This node not itself owing the next move is not a case this needs to
    rule out separately: the first not-fully-settled increment is either
    one this node has paid and is waiting on (checked below) or one this
    node has not paid yet, and the loop returns False for the latter
    without looking further, exactly as the old consider_abandonment did.

    No longer requires the peer to have reciprocated some earlier step
    first: a Trade row cannot exist without a verified, signed accept
    from the actual maker (market.py's FillRequest/FillResponse
    handshake), so the unsolicited-dust scenario that guard defended
    against is structurally unreachable regardless, and requiring it
    anyway meant a peer defecting on the very first step they owed, the
    common case, could never be judged at all.
    """
    if trade.status != TRADE_STALLED:
        return False
    for inc in (Increment.select()
                .where(Increment.session_id == trade.session_id)
                .order_by(Increment.n)):
        if inc.out_state == LEG_SETTLED and inc.in_state == LEG_SETTLED:
            continue
        if inc.out_state != LEG_SETTLED:
            return False
        if not inc.deadline_height:
            return False
        return current_height >= inc.deadline_height + ABANDON_AFTER_BLOCKS
    return False


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class Engine:
    """Drives active trades forward. One pass per call, never blocking.

    Every pass re-derives what it needs from the chains, so a pass is
    safe to abandon at any point and safe to repeat. There is no state
    carried between passes that a restart could lose.
    """

    def __init__(self, lapse, xlm, secrets_provider, min_confirm_depth=MIN_CONFIRM_DEPTH):
        self.lapse = lapse
        self.xlm = xlm
        # Returns (kek, xlm_seed) or (None, None) when the wallet is
        # locked. Held behind a call rather than stored so a locked node
        # simply cannot send, which is the correct behaviour rather than
        # an error case.
        self.secrets = secrets_provider
        # This node's own current floor for recognising a LapseCoin leg as
        # settled, folded into _required_depth on top of the trade's own
        # (canonical, maker-declared) confirm_depth. A user who raises
        # their settings after a trade already started gets the benefit
        # of it immediately for their own risk-taking, without that
        # changing the one public number (Trade.confirm_depth) a
        # bystander computing this trade's deadline would use.
        self.min_confirm_depth = max(min_confirm_depth, MIN_CONFIRM_DEPTH)

    # -- reading -------------------------------------------------------

    def _out_adapter(self, trade):
        return self.lapse if trade.i_send == "lapse" else self.xlm

    def _in_adapter(self, trade):
        return self.xlm if trade.i_send == "lapse" else self.lapse

    def _out_terms(self, trade, inc):
        """(from, to, memo, amount) for the leg this node sends."""
        memo = swap.session_tag(trade.order_id, trade.session_id, inc.n)
        if trade.i_send == "lapse":
            return (trade.my_lapse_addr, trade.peer_lapse_addr, memo,
                    inc.lapse_amount)
        return (trade.my_xlm_addr, trade.peer_xlm_addr, memo, inc.xlm_amount)

    def _in_terms(self, trade, inc):
        """(from, to, memo, amount) for the leg this node receives."""
        memo = swap.session_tag(trade.order_id, trade.session_id, inc.n)
        if trade.i_send == "lapse":
            return (trade.peer_xlm_addr, trade.my_xlm_addr, memo,
                    inc.xlm_amount)
        return (trade.peer_lapse_addr, trade.my_lapse_addr, memo,
                inc.lapse_amount)

    def _required_depth(self, trade, adapter):
        # Stellar reports a huge depth, so its legs clear any threshold.
        return max(trade.confirm_depth, MIN_CONFIRM_DEPTH, self.min_confirm_depth) \
            if adapter.asset == "lapse" else 1

    # -- the crash-safe send -------------------------------------------

    def ensure_sent(self, trade, inc):
        """Make sure this node's leg of a step has been paid, exactly once.

        Order matters and is the point of the whole function:

        1. Ask the chain whether this step is already paid. This is what
           makes a lost database harmless, and it runs before anything is
           built, every time, not only after a restart.
        2. If a signed envelope is already on disk, re-send that one. It
           carries a sequence number, so it applies at most once.
        3. Only with no payment and no envelope is something new built,
           and it is written down before it is sent.
        """
        if inc.out_state == LEG_SETTLED:
            return
        adapter = self._out_adapter(trade)
        from_addr, to_addr, memo, amount = self._out_terms(trade, inc)

        found = adapter.find_payment(from_addr, to_addr, memo, amount)
        if found:
            tx_hash, depth = found
            self._record_outbound(inc, tx_hash, depth,
                                  self._required_depth(trade, adapter))
            return

        if inc.out_envelope and inc.out_state in (LEG_INTENT, LEG_SUBMITTED):
            if self._resend(trade, inc, adapter, from_addr):
                return

        self._build_and_send(trade, inc, adapter, to_addr, memo, amount)

    def _resend(self, trade, inc, adapter, from_addr):
        """Re-submit the stored envelope. True if it is still in play.

        A rejection here is only terminal when the sequence is provably
        spent by something else, because then this envelope can never
        apply and retrying it forever is a trade that never moves. Any
        other rejection leaves it alone to be retried.
        """
        envelope = self._load_envelope(inc, adapter)
        try:
            ok, detail = adapter.submit(envelope)
        except Unreachable:
            raise
        if ok:
            inc.out_state = LEG_SUBMITTED
            inc.out_submitted_at = time.time()
            inc.out_detail = detail
            inc.save()
            return True
        if adapter.sequence_consumed(from_addr, inc.out_seq):
            log.warning("[swap] %s step %d: stored envelope can no longer "
                        "apply (%s); rebuilding", trade.session_id, inc.n, detail)
            inc.out_state = LEG_DEAD
            inc.out_envelope = ""
            inc.out_detail = detail
            inc.save()
            adapter.forget_sequence(from_addr)
            return False
        inc.out_detail = detail
        inc.save()
        return True

    def _build_and_send(self, trade, inc, adapter, to_addr, memo, amount):
        kek, seed = self.secrets()
        if kek is None and seed is None:
            raise SwapError("wallet is locked")

        if adapter.asset == "lapse":
            envelope, tx_hash, seq = adapter.build(to_addr, amount, memo, kek)
            stored = self._dump_envelope(envelope)
        else:
            send_amount, create_account = self._xlm_send_amount(adapter, to_addr, amount)
            envelope, tx_hash, seq = adapter.build(
                to_addr, send_amount, memo, seed, create_account=create_account)
            stored = envelope

        # Written down before it is sent. A crash between here and the
        # submit leaves a signed transaction that never went out, which
        # costs nothing; the reverse order is what pays twice.
        inc.out_envelope = stored
        inc.out_tx_hash = tx_hash
        inc.out_seq = seq
        inc.out_state = LEG_INTENT
        inc.save()

        ok, detail = adapter.submit(envelope)
        inc.out_detail = detail
        if ok:
            inc.out_state = LEG_SUBMITTED
            inc.out_submitted_at = time.time()
        inc.save()

    @staticmethod
    def _xlm_send_amount(adapter, to_addr, agreed_amount):
        """What to actually put in an XLM leg's envelope, and whether it
        has to be a create-account operation.

        A plain payment to a Stellar address with no account behind it
        fails outright (op_no_destination): a seller who has never
        funded their trading wallet is exactly who a "sell LAPSE without
        owning XLM first" feature exists for, so this is not an edge
        case, it is the normal first payment to a new counterparty.

        Bringing an account into existence costs at least the network's
        own minimum reserve (xlm.ACCOUNT_MIN_BALANCE_STROOPS) regardless
        of what this step was scheduled for, since Stellar has no
        smaller unit an account can be created with. When the agreed
        amount already clears it, nothing changes.

        This is deliberately the one place a step's outbound amount can
        exceed what swap.py's exposure cap planned for, and it is
        bounded: at most once per trade, since the destination account
        then exists for every later step, and by at most one XLM (the
        gap between the agreed amount and the minimum) in the worst
        case. The counterparty's own find_payment accepts paid >= agreed,
        not equality, so the difference simply settles the step it was
        scheduled for; nothing downstream needs to know this happened.

        Sponsored creation (CAP-33) cannot be used here even though it
        would spare the buyer this cost: it requires a signature from the
        new account's own key in the same transaction (its end-sponsoring
        operation is sourced by the sponsored account), which means
        active, real-time cooperation from the seller. Nothing in this
        protocol gives a payer any way
        to obtain that from a counterparty it has never exchanged a
        message with.
        """
        if adapter.account_exists(to_addr):
            return agreed_amount, False
        return max(agreed_amount, xlm_mod.ACCOUNT_MIN_BALANCE_STROOPS), True

    @staticmethod
    def _dump_envelope(tx_dict):
        import json
        return json.dumps(tx_dict, sort_keys=True)

    @staticmethod
    def _load_envelope(inc, adapter):
        if adapter.asset == "lapse":
            import json
            return json.loads(inc.out_envelope)
        return inc.out_envelope

    def _record_outbound(self, inc, tx_hash, depth, required):
        inc.out_tx_hash = tx_hash
        if depth >= required:
            if inc.out_state != LEG_SETTLED:
                inc.out_settled_at = time.time()
            inc.out_state = LEG_SETTLED
        else:
            inc.out_state = LEG_SUBMITTED
        inc.save()

    # -- watching ------------------------------------------------------

    def check_outbound(self, trade, inc):
        """Re-verify this node's own leg against the chain.

        Run even on a leg already marked settled, because on LapseCoin
        that can stop being true: a reorg takes the transaction back and
        the depth becomes unknown. Trusting the stored flag would leave a
        trade proceeding on a payment that no longer exists.
        """
        if not inc.out_tx_hash:
            return
        adapter = self._out_adapter(trade)
        depth = adapter.confirmations(inc.out_tx_hash)
        required = self._required_depth(trade, adapter)
        if depth is None:
            if inc.out_state == LEG_SETTLED:
                log.warning("[swap] %s step %d: a settled payment is no longer "
                            "on chain (reorg); waiting for it again",
                            trade.session_id, inc.n)
            inc.out_state = LEG_SUBMITTED
            inc.out_settled_at = 0.0
            inc.save()
            return
        self._record_outbound(inc, inc.out_tx_hash, depth, required)

    def check_inbound(self, trade, inc):
        """Look for the counterparty's leg of this step.

        Nothing the counterparty says is taken as evidence; this only ever
        believes the chain. Over-payment settles the step, under-payment
        does not, which is what find_payment's amount check enforces.
        """
        adapter = self._in_adapter(trade)
        from_addr, to_addr, memo, amount = self._in_terms(trade, inc)
        required = self._required_depth(trade, adapter)

        if inc.in_state == LEG_SETTLED and inc.in_tx_hash:
            depth = adapter.confirmations(inc.in_tx_hash)
            if depth is None or depth < required:
                log.warning("[swap] %s step %d: the payment received is no "
                            "longer settled (reorg)", trade.session_id, inc.n)
                inc.in_state = LEG_PENDING
                inc.in_settled_at = 0.0
                inc.save()
            return

        found = adapter.find_payment(from_addr, to_addr, memo, amount)
        if not found:
            return
        tx_hash, depth = found
        inc.in_tx_hash = tx_hash
        if depth >= required:
            inc.in_state = LEG_SETTLED
            inc.in_settled_at = time.time()
        inc.save()

    # -- driving -------------------------------------------------------

    def advance(self, trade):
        """One pass over a trade. Returns the step worked on, or None.

        Repeatable and interruptible: every decision is re-derived from
        the chains, so calling this twice does the same thing as calling
        it once, and being killed partway does not lose progress that
        another pass cannot recover.
        """
        ensure_tables()
        steps = list(Increment.select()
                     .where(Increment.session_id == trade.session_id)
                     .order_by(Increment.n))
        for inc in steps:
            if inc.out_state == LEG_SETTLED and inc.in_state == LEG_SETTLED:
                continue
            self._advance_step(trade, inc)
            return inc

        self._complete(trade)
        return None

    def _advance_step(self, trade, inc):
        """Move one step as far as it will go this pass.

        Whoever moves first sends and then waits; whoever moves second
        waits and only then sends. The second position is the safe one,
        which is exactly why it alternates: over a trade neither side
        spends more time exposed than the other.
        """
        if inc.i_move_first:
            self.check_outbound(trade, inc)
            if inc.out_state != LEG_SETTLED:
                self.ensure_sent(trade, inc)
                self.check_outbound(trade, inc)
            if inc.out_state == LEG_SETTLED:
                self.check_inbound(trade, inc)
        else:
            self.check_inbound(trade, inc)
            if inc.in_state == LEG_SETTLED:
                self.check_outbound(trade, inc)
                if inc.out_state != LEG_SETTLED:
                    self.ensure_sent(trade, inc)
                    self.check_outbound(trade, inc)

        self._update_timing(trade, inc)

    def _update_timing(self, trade, inc):
        """Track whether a step is merely slow or genuinely stuck.

        Missing a deadline only stalls; it never blames. The two are
        separated because from outside they look the same, and the cost of
        confusing them falls on an honest user with a slow connection.
        """
        now = time.time()
        done = inc.out_state == LEG_SETTLED and inc.in_state == LEG_SETTLED
        if done:
            if not inc.completed_height:
                inc.completed_height = self.lapse.height()
                inc.save()
                self._propagate_deadline(trade, inc)
            if trade.status == TRADE_STALLED:
                trade.status = TRADE_ACTIVE
                trade.stalled_since = 0.0
            trade.updated_at = now
            trade.save()
            return

        if inc.deadline_at and now > inc.deadline_at:
            if trade.status == TRADE_ACTIVE:
                trade.status = TRADE_STALLED
                trade.stalled_since = now
                trade.note = f"step {inc.n} passed its deadline"
                log.info("[swap] %s step %d is overdue; trade stalled, no "
                         "blame assigned", trade.session_id, inc.n)
            trade.updated_at = now
            trade.save()

    def _propagate_deadline(self, trade, inc):
        """Give the next step a deadline now that this one's completed_height
        is known, so its own deadline_height is never computed later than
        the moment it becomes possible: a receipt about a stuck step n+1
        must be able to point at a real deadline_height, not 0.
        """
        nxt = Increment.get_or_none(Increment.session_id == trade.session_id,
                                    Increment.n == inc.n + 1)
        if nxt is not None and not nxt.deadline_height:
            nxt.deadline_height = deadline_height(inc.completed_height,
                                                  trade.confirm_depth)
            nxt.save()

    def _complete(self, trade):
        if trade.status == TRADE_COMPLETED:
            return
        trade.status = TRADE_COMPLETED
        trade.updated_at = time.time()
        trade.stalled_since = 0.0
        trade.save()
        # Nothing else to update: trust reads completed counts straight
        # off Trade.status, and delinquency is computed fresh from
        # Increment rows (see trust.local_tally, is_delinquent), so this
        # save is the entire effect on this peer's standing.
        log.info("[swap] %s completed: %d steps delivered",
                 trade.session_id, trade.increment_count)

    @staticmethod
    def _peer_ever_reciprocated(trade):
        """Whether the counterparty has settled a leg of their own here.

        Not consulted by is_delinquent (a peer's very first owed step can
        be judged with nothing else to go on, see that function's
        docstring); kept because it is still a true, useful fact for a
        reader to compute, e.g. the Trades page distinguishing a peer who
        has never sent anything at all from one mid-trade who has already
        reciprocated earlier steps. A settled inbound leg is a transaction
        the counterparty signed, carrying this trade's session tag, which
        nobody else could have produced.

        Reading agreement off the chain rather than off a protocol message
        also means it survives everything a message would not: a restart,
        a lost database, a peer this node has never exchanged a datagram
        with. The evidence is public and permanent.
        """
        return (Increment.select()
                .where(Increment.session_id == trade.session_id,
                       Increment.in_state == LEG_SETTLED)
                .exists())


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def reconcile(engine, trade):
    """Rebuild a trade's state from the chains, discarding local claims.

    Run at startup for every unfinished trade, before anything is sent.
    Stored leg states are treated as a hint and nothing more: they are
    overwritten by what the two ledgers say, which is the only account
    that cannot have been lost to a crash or invalidated by a reorg.

    This is also what makes the database expendable. A node that lost its
    trade rows entirely, but still knows the session, recovers every
    settled step from the memos on chain.
    """
    ensure_tables()
    steps = list(Increment.select()
                 .where(Increment.session_id == trade.session_id)
                 .order_by(Increment.n))
    recovered = 0
    for inc in steps:
        before = (inc.out_state, inc.in_state)
        try:
            engine.check_outbound(trade, inc)
            if not inc.out_tx_hash:
                adapter = engine._out_adapter(trade)
                terms = engine._out_terms(trade, inc)
                found = adapter.find_payment(*terms)
                if found:
                    engine._record_outbound(
                        inc, found[0], found[1],
                        engine._required_depth(trade, adapter))
            engine.check_inbound(trade, inc)
        except Unreachable as e:
            # Nothing is known about this step, so nothing is changed.
            # Leaving it untouched is correct: the next pass asks again,
            # and guessing either way here could either re-send a payment
            # or abandon a live one.
            log.info("[swap] %s step %d: could not reconcile (%s); will retry",
                     trade.session_id, inc.n, e)
            continue
        if (inc.out_state, inc.in_state) != before:
            recovered += 1
    if recovered:
        log.info("[swap] %s: %d step(s) corrected from chain data",
                 trade.session_id, recovered)
    return recovered


def reconcile_all(engine):
    """Reconcile every unfinished trade. Called once at startup."""
    ensure_tables()
    unfinished = Trade.select().where(
        Trade.status.in_([TRADE_ACTIVE, TRADE_STALLED]))
    return sum(reconcile(engine, trade) for trade in unfinished)


# ---------------------------------------------------------------------------
# Maker-side: answering fill requests
# ---------------------------------------------------------------------------
#
# A maker decides before anything is paid, not after. A fill against one
# of this node's own orders arrives as a signed FillRequest, gossiped the
# same way an order or a block is (see market.py's fill-request section
# and node._handle_inbound_fill_request): a broadcast, not a direct
# connection, so answering it leaks nothing about either side's address
# that gossiping the order itself did not already leak.
#
# Publishing the answer IS the commitment. An accept reserves capacity
# the instant it is published (market.reserved_ticks counts every
# accepted response, not just ones this node happens to have paid into)
# and opens this side's own Trade row right there, before either party
# has sent a single stroop. There is no claim any more: a request already
# carries the one thing a claim used to supply (the taker's address on
# the other chain), and a maker that has explicitly agreed no longer
# needs anything paid first to learn a session exists.
#
# _answer_one is called from two different threads that share nothing
# but this process's own database: the swap worker's own periodic pass
# (swap_worker.run_once, via answer_fill_requests) and a person clicking
# Accept on the Market page (market_routes.answer_fill_request_action,
# via decide_fill_request), on Werkzeug's own request thread (see
# api._close_db_after_request's docstring: every request runs on a
# fresh thread). Both read this node's own balance and
# _pending_send_total, and both read an order's own remaining_ticks,
# before deciding whether to commit; neither of those reads locks
# anything. Two accepts racing each other, one from each thread, can
# each see the exposure as it stood before the other committed and both
# pass, jointly promising more than this node holds or more than an
# order's own lapse_total allows, since Trade.create for the first one
# is not visible to the second's read until it already made its own
# decision. _ACCEPT_LOCK below makes "read the exposure, then commit
# to it" one atomic step process-wide, the same guarantee a single
# thread already gets for free from running one request at a time.
_ACCEPT_LOCK = threading.Lock()


def answer_fill_requests(engine, node, my_xlm_addr, stranger_cap, confirm_depth,
                         min_trust=0.0):
    """Decide every live fill request against this node's own orders.
    Returns how many were accepted.

    Nothing here waits on a chain except the two balance checks, so a
    locked wallet or an unreachable one just leaves requests unanswered
    for the next pass rather than risking a decision it cannot back.

    min_trust (see settings.SWAP_AUTO_ACCEPT_MIN_TRUST) leaves a request
    pending instead of deciding it, one request at a time, when this
    specific counterparty's trust score falls short (see _answer_one):
    still visible on the Market page with the same trust detail a click
    there would show, answered only by decide_fill_request once a person
    actually clicks Accept or Decline. Passing inf leaves every live
    request pending this way, which is how "review everything by hand"
    is expressed here: there is no separate switch for it. Nothing about
    the decision itself changes in either case, only who makes the call.
    """
    ensure_tables()
    # Not orders_by_maker: that hides a cancelled or expired order, and a
    # request that arrived (and was already accepted) before this node
    # cancelled its own order still deserves completion.
    my_orders = market_mod.orders_by_maker_with_claims(node.addr)
    if not my_orders:
        return 0

    accepted = 0
    for order_row in my_orders:
        for req in market_mod.requests_for_order(order_row.order_id):
            if market_mod.get_fill_response(req.request_id) is not None:
                continue
            if Trade.get_or_none(Trade.session_id == req.session_id) is not None:
                continue
            if _answer_one(engine, node, my_xlm_addr, stranger_cap,
                          confirm_depth, order_row, req, min_trust=min_trust):
                accepted += 1
    if accepted:
        log.info("[swap] accepted %d new fill request(s) as maker", accepted)
    return accepted


def decide_fill_request(engine, node, my_xlm_addr, stranger_cap, confirm_depth,
                        request_id, accept):
    """Answer exactly one fill request against one of this node's own
    orders, by request_id: the manual counterpart to answer_fill_requests,
    for a person clicking Accept or Decline on the Market page instead of
    the trust floor deciding on its own.

    accept=False never touches trust or the exposure cap; a maker may
    decline anyone for any reason (or none) and always could, auto-accept
    or not. accept=True runs through the exact same _answer_one this
    node's own worker would have used automatically: nothing about a
    manual accept is a weaker check than an automatic one, only who
    triggers it. Returns True if it produced an answer, False if there
    was nothing left here to answer (already answered, already a trade,
    the order or request is gone, or a decline failed to find the row at
    all), so a route calling this can tell "handled" from "too late".
    """
    ensure_tables()
    req = market_mod.get_fill_request(request_id)
    if req is None:
        return False
    order_row = market_mod.get_order(req.order_id)
    if order_row is None or order_row.maker_lapse_addr != node.addr:
        return False
    if market_mod.get_fill_response(req.request_id) is not None:
        return False
    if Trade.get_or_none(Trade.session_id == req.session_id) is not None:
        return False

    if not accept:
        kek, _seed = engine.secrets()
        if kek is None:
            return False
        resp = market_mod.build_fill_response(
            request_id=req.request_id, order_id=order_row.order_id,
            session_id=req.session_id, lapse_total=req.lapse_total,
            accepted=False, maker_pubkey_hex=node.pk_hex,
            accepted_height=node.view.height, confirm_depth=confirm_depth,
            increment_count=None, reason="declined by the maker")
        market_mod.sign_fill_response(resp, node.keyfile, kek)
        market_mod.store_fill_response(resp)
        node.publish_fill_response(resp)
        return True

    return _answer_one(engine, node, my_xlm_addr, stranger_cap,
                       confirm_depth, order_row, req)


def _pending_send_total(asset):
    """Everything this node has already promised to send on one asset,
    across every trade still running on it, whether maker or taker.

    A balance read alone is not enough to decide a second order: it is
    the same account backing every trade this node has open, and a
    balance check that only looks at the chain would let two accepted
    orders each pass a solvency check against the same, unspent stroops,
    committing this node to send more than it holds. Every accept is
    itself a Trade row the instant it happens (see _answer_one), so
    summing every active or stalled trade's own unsettled obligation on
    this asset, including ones this very pass just created, is exactly
    what keeps that from happening.
    """
    total = 0
    for trade in Trade.select().where(
            Trade.status.in_([TRADE_ACTIVE, TRADE_STALLED]),
            Trade.i_send == asset):
        owed = trade.lapse_total if asset == "lapse" else trade.xlm_total
        sent = 0
        for inc in Increment.select().where(Increment.session_id == trade.session_id,
                                            Increment.out_state == LEG_SETTLED):
            sent += inc.lapse_amount if asset == "lapse" else inc.xlm_amount
        total += max(owed - sent, 0)
    return total


def _answer_one(engine, node, my_xlm_addr, stranger_cap, confirm_depth,
                order_row, req, min_trust=0.0):
    """Decide one fill request against one order. Returns True if it was
    accepted; a decline is still an answer, just not a trade. Returning
    False can also mean the request is being left pending rather than
    answered at all (see min_trust below), which is not the same thing
    as a decline: nothing here has told the taker anything yet.

    Every request that gets an actual answer, accept or decline, gets it
    exactly once: the caller already filters out anything with a stored
    response, so reaching here means this is the first and only look a
    request that ends up answered gets. A request left pending for
    falling short of min_trust is looked at again on the next call,
    since nothing was recorded to remember that it was already seen.

    min_trust (see settings.SWAP_AUTO_ACCEPT_MIN_TRUST) only ever makes
    this stricter than the hard checks above it (funding, the exposure
    cap): those can still auto-decline a request outright regardless of
    this argument, because there is nothing left to decide by hand once
    a request structurally cannot be honoured. This argument exists so
    decide_fill_request's manual Accept (default min_trust=0.0) can
    answer a request that answer_fill_requests's automatic pass, called
    with a positive floor, left pending for exactly this reason: the
    request was otherwise fine, a person just had to be the one to say
    yes.
    """
    with _ACCEPT_LOCK:
        return _answer_one_locked(engine, node, my_xlm_addr, stranger_cap,
                                  confirm_depth, order_row, req, min_trust)


def _answer_one_locked(engine, node, my_xlm_addr, stranger_cap, confirm_depth,
                       order_row, req, min_trust=0.0):
    """The body of _answer_one, run under _ACCEPT_LOCK. Split out only so
    the lock and the decision it guards are each easy to read on their
    own; see _ACCEPT_LOCK for why every read this makes (this node's own
    balance, _pending_send_total, the order's own remaining_ticks) has
    to be paired with its eventual commit (Trade.create, or the
    FillResponse either way) with nothing else able to run in between.
    """
    kek, _seed = engine.secrets()
    if kek is None:
        return False   # locked; leave it for the next pass to decide

    # Read once and reused for every response this call might send,
    # accept or decline, so the height a bystander would anchor a
    # deadline to (were this an accept) is exactly the height Trade.create
    # below also records, not a slightly later one a block happening to
    # land mid-call would otherwise introduce.
    accepted_height = node.view.height

    def respond(is_accept, increment_count=None, reason=""):
        resp = market_mod.build_fill_response(
            request_id=req.request_id, order_id=order_row.order_id,
            session_id=req.session_id, lapse_total=req.lapse_total,
            accepted=is_accept, maker_pubkey_hex=node.pk_hex,
            accepted_height=accepted_height, confirm_depth=confirm_depth,
            increment_count=increment_count, reason=reason)
        market_mod.sign_fill_response(resp, node.keyfile, kek)
        market_mod.store_fill_response(resp)
        node.publish_fill_response(resp)

    try:
        market_mod.validate_fill(order_row, req.lapse_total)
    except market_mod.OrderRejected as e:
        respond(False, reason=str(e))
        return False

    xlm_total = swap.xlm_for_lapse(req.lapse_total,
                                   order_row.price_stroops_per_lapse)

    # The maker builds the schedule itself now, from its own trust view
    # of this taker, rather than checking one the taker proposed: nobody
    # but the side actually at risk on a step gets to decide how large
    # that step is.
    my_trust_of_taker, taker_trust_of_me = trust_mod.mutual_scores(
        node, req.taker_lapse_addr)
    try:
        schedule, count, _cap = swap.plan(
            req.lapse_total, xlm_total, my_trust_of_taker,
            stranger_cap=stranger_cap)
    except swap.TradeTooLarge:
        respond(False, reason=(
            "more than this node will risk with this counterparty in one go"))
        return False

    # Which asset the maker sends is the mirror of the order's own
    # direction, exactly as market_routes._open_trade derives the
    # taker's from the same field.
    maker_i_send = "lapse" if order_row.direction == "sell" else "xlm"
    if maker_i_send == "lapse":
        have, need = engine.lapse.balance(node.addr), req.lapse_total
    else:
        have, need = engine.xlm.balance(my_xlm_addr), xlm_total
    # A chain balance alone is not this node's own to promise again: it
    # already backs every trade already accepted, this pass and earlier,
    # that has not yet actually sent (see _pending_send_total). Without
    # this, two of this node's own orders on the same asset could each
    # pass this check against the same unspent stroops and this node
    # would end up committed to sending more than it holds.
    already_owed = _pending_send_total(maker_i_send)
    if have - already_owed < need:
        log.warning(
            "[swap] refusing fill request %s from %s: this node cannot "
            "fund its own %s leg (has %d, %d already owed elsewhere, needs %d)",
            req.request_id[:16], req.taker_lapse_addr[:24],
            maker_i_send, have, already_owed, need)
        respond(False, reason="the maker cannot currently fund this fill")
        return False

    if my_trust_of_taker < min_trust:
        # Otherwise acceptable: funded, within the exposure cap, just
        # not from a counterparty with enough standing to clear this
        # node's own auto-accept floor. Left unanswered rather than
        # declined, so it surfaces on the Market page's pending list
        # (market_routes.pending_maker_requests) for decide_fill_request
        # to accept or decline by hand instead.
        return False

    # opening_mover is computed from the same mutual, unforgeable data
    # either side of a dyad can derive on its own (trust.mutual_scores),
    # so it lands on the same answer the taker independently derives for
    # its own Trade row without anything having to say so. Nothing forces
    # step one to either side any more: the reason it used to be forced
    # to the taker (the maker had no other way to learn the trade
    # existed) is gone now that the maker agrees before anything is paid.
    taker_i_open = swap.opening_mover(taker_trust_of_me, my_trust_of_taker)
    if taker_i_open is None:
        taker_i_open = True
    maker_i_open = not taker_i_open

    now = time.time()
    Trade.create(
        session_id=req.session_id, order_id=order_row.order_id, role="maker",
        my_lapse_addr=node.addr, my_xlm_addr=my_xlm_addr,
        peer_lapse_addr=req.taker_lapse_addr, peer_xlm_addr=req.taker_xlm_addr,
        i_send=maker_i_send, lapse_total=req.lapse_total, xlm_total=xlm_total,
        increment_count=count, confirm_depth=confirm_depth,
        accepted_height=accepted_height,
        status=TRADE_ACTIVE, created_at=now, updated_at=now)

    timeout = step_timeout_seconds(confirm_depth)
    for n, (lapse_amount, xlm_amount) in enumerate(schedule, start=1):
        i_move_first = swap.i_move_first(n, maker_i_open)
        Increment.create(
            id=f"{req.session_id}:{n}", session_id=req.session_id, n=n,
            lapse_amount=lapse_amount, xlm_amount=xlm_amount,
            i_move_first=i_move_first, created_at=now, deadline_at=now + timeout,
            deadline_height=(deadline_height(accepted_height, confirm_depth)
                             if n == 1 else 0))

    respond(True, increment_count=count)
    log.info("[swap] %s accepted: %d steps against %s (maker side)",
             req.session_id, count, req.taker_lapse_addr[:24])
    return True


# ---------------------------------------------------------------------------
# Auto-matching: proactively taking a compatible order already in the book
# ---------------------------------------------------------------------------
#
# Posting an order has always meant "I will trade at this price if someone
# takes it"; nothing made this node check whether somebody already had.
# Two orders on opposite sides of the book that already agree on a price
# do not need a person on either side to notice each other and click Buy
# or Sell for themselves: the two posted prices already say both makers
# are willing, and the fill-request handshake above is exactly what
# already lets a trade be initiated by software rather than by hand
# (node._handle_inbound_fill_request treats a request the same regardless
# of what produced it). This is that same taker path, pointed at this
# node's own resting orders instead of waiting for a person to browse the
# book.
#
# Two orders cross outright when the side offering to pay more names a
# price at least as good as the side offering to accept less: a sell at
# 1000 and a buy at 1050 cross (the buyer already offered more than the
# seller asked). A sell at 1000 and a buy at 950 do not cross outright,
# but this node's own order may still privately be willing to meet it
# partway: Order.auto_match_margin_stroops (set only by this node, for
# this node's own orders, never gossiped, see that field's own comment)
# widens the comparison by that many stroops without ever changing, or
# telling anyone, the price actually posted. A request sent this way
# still always fills at the resting counter-order's own price
# (market.validate_fill/xlm_for_lapse), the exact rule a manual taker is
# already held to, so this can never fill anyone at a price they did not
# themselves post, and it is decided by the counter-order's own maker
# through the exact same _answer_one path (trust floor, exposure cap,
# funding) a request typed in by a person goes through. Nothing this
# node ever sends says its margin, or that one was used at all: a
# completed trade at 950 looks, to anyone watching, exactly like a
# maker who simply decided by hand that 950 was good enough, which is
# the whole point of keeping this local rather than making it a term of
# the fill request.

# How many of the best-priced counter-orders to check per own order, per
# pass. Bounded rather than the whole side, same reasoning as
# market.list_orders' own page size: "price" is already sorted best-first
# by SQL, and once one candidate does not cross, nothing further down a
# price-sorted list will either, so this almost never looks past the
# first row. It only widens past that when the very best-priced order
# turns out to be unusable for some other reason (a live request against
# it already sent, or this node's own funding or exposure cap falling
# short), or when list_orders' own page has a dust remainder filtered out
# of it (see that function's docstring); a later pass still catches
# whatever a bounded page happened to miss.
AUTO_MATCH_CANDIDATES = 5


def auto_match_orders(engine, node, my_xlm_addr, stranger_cap):
    """For each of this node's own live orders, look for an existing
    counter-order that already crosses it and send a fill request
    against it, the same thing a person manually taking that order
    would do. Returns how many requests were sent this pass.

    Nothing about safety changes: this side's own funding and exposure
    cap are checked here first, the same way market_routes._open_trade
    checks them for a manual taker, and the counter-order's own maker
    still independently decides through _answer_one. This can only
    ever do what a person watching both sides of the book could
    already have done by hand, sooner and without having to be there.
    """
    ensure_tables()
    kek, _seed = engine.secrets()
    if kek is None:
        return 0
    height = node.view.height
    my_orders = market_mod.orders_by_maker(node.addr, height)
    if not my_orders:
        return 0

    # At most one live request per counter-order from this node: a pass
    # that already asked about one does not ask again on every later
    # pass while that request is still waiting for an answer.
    already_asked = {r.order_id for r in market_mod.requests_by_taker(node.addr)}
    # Same reasoning as swap_engine._pending_send_total, but for
    # requests this very pass is about to send rather than trades
    # already accepted: two crossing candidates found in the same pass
    # must not each be sized against the same untouched balance.
    promised_this_pass = {"lapse": 0, "xlm": 0}

    sent = 0
    for mine in my_orders:
        if market_mod.remaining_ticks(mine) < max(mine.min_fill, 1):
            continue
        counter_direction = "buy" if mine.direction == "sell" else "sell"
        candidates, _total = market_mod.list_orders(
            height, counter_direction, exclude_maker=node.addr,
            sort="price", limit=AUTO_MATCH_CANDIDATES)
        margin = max(mine.auto_match_margin_stroops, 0)
        for theirs in candidates:
            crosses = (theirs["price"] >= mine.price_stroops_per_lapse - margin
                      if mine.direction == "sell" else
                      theirs["price"] <= mine.price_stroops_per_lapse + margin)
            if not crosses:
                break   # price-sorted best-first; nothing further crosses either
            if theirs["order_id"] in already_asked:
                continue
            if _send_auto_match(engine, node, my_xlm_addr, stranger_cap,
                                kek, theirs["order_id"], promised_this_pass):
                sent += 1
                already_asked.add(theirs["order_id"])
    if sent:
        log.info("[swap] auto-matched %d existing order(s) in the book", sent)
    return sent


def _send_auto_match(engine, node, my_xlm_addr, stranger_cap, kek, order_id,
                     promised_this_pass):
    """Send a fill request against one counter-order already known to
    cross, sized to fit everything that already bounds a manual taker.
    Returns True if a request was sent.
    """
    order_row = market_mod.get_order(order_id)
    if order_row is None or order_row.expiry_block <= node.view.height:
        return False

    remaining = market_mod.remaining_ticks(order_row)
    if remaining < max(order_row.min_fill, 1):
        return False

    detail = trust_mod.get_detail(
        order_row.maker_lapse_addr,
        trust_mod.address_age_blocks(node, order_row.maker_lapse_addr),
        node.view.state.get_balance(order_row.maker_lapse_addr), node=node)
    cap = swap.exposure_cap_stroops(detail["score"], stranger_cap)
    max_safe_lapse = swap.lapse_for_xlm(
        swap.max_safe_trade_stroops(cap), order_row.price_stroops_per_lapse)

    lapse_total = min(remaining, order_row.max_fill or remaining, max_safe_lapse)
    if lapse_total < max(order_row.min_fill, 1):
        # Whatever this node can safely and honestly ask for is smaller
        # than this counter-order will accept; nothing to do until
        # either side's trust or this node's own funding changes.
        return False

    xlm_total = swap.xlm_for_lapse(lapse_total, order_row.price_stroops_per_lapse)

    # The mirror of order_row.direction: they are selling LAPSE (this
    # node pays XLM) or buying it (this node pays LAPSE).
    i_send = "xlm" if order_row.direction == "sell" else "lapse"
    if i_send == "xlm":
        have, need = engine.xlm.balance(my_xlm_addr), xlm_total
    else:
        have, need = engine.lapse.balance(node.addr), lapse_total
    already_owed = _pending_send_total(i_send) + promised_this_pass[i_send]
    if have - already_owed < need:
        return False

    try:
        swap.plan(lapse_total, xlm_total, detail["score"], stranger_cap=stranger_cap)
    except swap.TradeTooLarge:
        return False
    try:
        market_mod.validate_fill(order_row, lapse_total)
    except market_mod.OrderRejected:
        return False

    session_id = swap.new_session_id(order_id, node.addr)
    req = market_mod.build_fill_request(
        order_id=order_id, session_id=session_id,
        taker_lapse_addr=node.addr, taker_xlm_addr=my_xlm_addr,
        lapse_total=lapse_total, pubkey_hex=node.pk_hex)
    market_mod.sign_fill_request(req, node.keyfile, kek)
    market_mod.store_fill_request(req)
    node.publish_fill_request(req)
    promised_this_pass[i_send] += need
    log.info("[swap] auto-match: requested %d ticks against order %s (crossed at %d)",
             lapse_total, order_id[:12], order_row.price_stroops_per_lapse)
    return True


# ---------------------------------------------------------------------------
# Taker-side: watching for an answer
# ---------------------------------------------------------------------------
#
# The taker's own Trade row is opened here, once, when a genuine accept
# from the actual maker turns up. Nothing is ever paid before this: a
# request that is declined, or that nobody answers within
# market.FILL_REQUEST_MAX_AGE_SECONDS, is simply pruned with nothing ever
# having moved (see market.prune_fill_requests).

def check_fill_responses(node, confirm_depth):
    """Look at this node's own outstanding fill requests for an answer
    and open the taker side of any that were accepted. Returns how many
    were opened.
    """
    ensure_tables()
    opened = 0
    for req in market_mod.requests_by_taker(node.addr):
        if Trade.get_or_none(Trade.session_id == req.session_id) is not None:
            continue
        resp = market_mod.get_fill_response(req.request_id)
        if resp is None:
            continue
        order_row = market_mod.get_order(req.order_id)
        if order_row is None:
            continue

        # expected_maker_addr is not optional here: without it, anyone
        # could sign a well-formed 'accepted' response with their own key
        # and have it mistaken for this order's actual maker agreeing
        # (see market.verify_fill_response).
        try:
            market_mod.verify_fill_response(
                _response_dict(resp), expected_maker_addr=order_row.maker_lapse_addr)
        except market_mod.FillResponseRejected as e:
            log.warning("[swap] discarding a fill response for %s: %s",
                       req.request_id[:16], e)
            continue

        if not resp.accepted:
            log.info("[swap] fill request %s was declined: %s",
                    req.request_id[:16], resp.reason or "no reason given")
            continue

        if _open_taker_trade(node, req, resp, order_row, confirm_depth):
            opened += 1
    if opened:
        log.info("[swap] opened %d new trade(s) as taker", opened)
    return opened


def _response_dict(resp):
    return {
        "request_id": resp.request_id, "order_id": resp.order_id,
        "session_id": resp.session_id, "lapse_total": resp.lapse_total,
        "accepted": resp.accepted, "increment_count": resp.increment_count,
        "reason": resp.reason, "maker_pubkey": resp.maker_pubkey,
        "accepted_height": resp.accepted_height, "confirm_depth": resp.confirm_depth,
        "signature": resp.signature,
    }


# ---------------------------------------------------------------------------
# Step receipts: emitting this node's own, and checking somebody else's
# ---------------------------------------------------------------------------
#
# Emission needs nothing new watched: it runs exactly where advance()
# already determines a leg settled, and where is_delinquent (above) says
# a stall has crossed into counting against the peer, and only packages
# that already-made determination into a small signed, gossipable
# record. See market.py's step-receipts section for what the record
# proves and why its signature is not what it is trusted for.

def emit_receipts_for_trade(engine, node, kek, trade):
    """This node's own receipts for whatever `trade` has newly settled or
    is currently delinquent (see is_delinquent). Returns how many were
    newly stored.

    Idempotent and cheap to call every pass: each receipt this node would
    produce for a given (session, step, asset, outcome) is looked up
    first and skipped if already on file, so repeating this call changes
    nothing once every applicable receipt already exists. That is what
    makes a "missed" receipt safe to re-offer here on every pass a stall
    remains delinquent, rather than needing to be emitted exactly once
    at a transition: there is no separate stored transition to catch
    here any more (see is_delinquent), so idempotent dedup is what
    prevents this from flooding the network with duplicates instead.
    """
    if kek is None:
        return 0
    ensure_tables()
    addr_a, addr_b = sorted((trade.my_lapse_addr, trade.peer_lapse_addr))
    emitted = 0
    steps = list(Increment.select().where(Increment.session_id == trade.session_id))
    for inc in steps:
        if inc.completed_height:
            emitted += _emit_settled_leg(engine, node, kek, trade, inc, "out",
                                         addr_a, addr_b)
            emitted += _emit_settled_leg(engine, node, kek, trade, inc, "in",
                                         addr_a, addr_b)
    if is_delinquent(trade, engine.lapse.height()):
        for inc in steps:
            if inc.out_state == LEG_SETTLED and inc.in_state != LEG_SETTLED:
                emitted += _emit_missed_leg(engine, node, kek, trade, inc,
                                            addr_a, addr_b)
    return emitted


def _leg_terms(engine, trade, inc, leg):
    """(from, to, memo, amount, asset, tx_hash) for one leg of one step."""
    if leg == "out":
        from_addr, to_addr, memo, amount = engine._out_terms(trade, inc)
        return from_addr, to_addr, memo, amount, trade.i_send, inc.out_tx_hash
    from_addr, to_addr, memo, amount = engine._in_terms(trade, inc)
    in_asset = "xlm" if trade.i_send == "lapse" else "lapse"
    return from_addr, to_addr, memo, amount, in_asset, inc.in_tx_hash


def _emit_settled_leg(engine, node, kek, trade, inc, leg, addr_a, addr_b):
    from_addr, to_addr, memo, amount, asset, tx_hash = _leg_terms(
        engine, trade, inc, leg)
    if not tx_hash:
        return 0
    if (StepReceipt.select()
            .where(StepReceipt.session_id == trade.session_id,
                   StepReceipt.n == inc.n, StepReceipt.outcome == "settled",
                   StepReceipt.asset == asset)
            .exists()):
        return 0
    receipt = market_mod.build_receipt(
        order_id=trade.order_id, session_id=trade.session_id, n=inc.n,
        reporter_lapse_addr=node.addr, addr_a=addr_a, addr_b=addr_b,
        asset=asset, from_addr=from_addr, to_addr=to_addr, amount=amount,
        memo=memo, outcome="settled", tx_hash=tx_hash,
        deadline_height=inc.deadline_height,
        checked_at_height=engine.lapse.height(), pubkey_hex=node.pk_hex)
    market_mod.sign_receipt(receipt, node.keyfile, kek)
    return _store_and_publish(node, receipt)


def _emit_missed_leg(engine, node, kek, trade, inc, addr_a, addr_b):
    # Abandonment is only ever this node's own paid leg going
    # unanswered, so the missing leg is always the inbound one.
    from_addr, to_addr, memo, amount, asset, _tx = _leg_terms(
        engine, trade, inc, "in")
    if (StepReceipt.select()
            .where(StepReceipt.session_id == trade.session_id,
                   StepReceipt.n == inc.n, StepReceipt.outcome == "missed")
            .exists()):
        return 0
    receipt = market_mod.build_receipt(
        order_id=trade.order_id, session_id=trade.session_id, n=inc.n,
        reporter_lapse_addr=node.addr, addr_a=addr_a, addr_b=addr_b,
        asset=asset, from_addr=from_addr, to_addr=to_addr, amount=amount,
        memo=memo, outcome="missed", tx_hash="",
        deadline_height=inc.deadline_height or 0,
        checked_at_height=engine.lapse.height(), pubkey_hex=node.pk_hex)
    market_mod.sign_receipt(receipt, node.keyfile, kek)
    return _store_and_publish(node, receipt)


def _store_and_publish(node, receipt):
    if not market_mod.store_receipt(receipt):
        return 0
    publish = getattr(node, "publish_receipt", None)
    if publish is not None:
        publish(receipt)
    return 1


def verify_receipt_against_chain(engine, receipt):
    """Check one StepReceipt row against the chain it actually claims to
    describe. True or False; raises Unreachable if that chain cannot
    currently be asked, which callers must treat as "still unknown", not
    as a negative (see trust.py: an outage must never zero anyone's
    standing).

    This is the entire trust value of a receipt: its signature says who
    is reporting, nothing about whether it is true. A false "settled"
    claim fails to find the tx_hash it names; a false "missed" claim is
    contradicted the moment the payment it says does not exist turns up
    in the very lookup that checks it.
    """
    adapter = engine.lapse if receipt.asset == "lapse" else engine.xlm
    found = adapter.find_payment(receipt.from_addr, receipt.to_addr,
                                 receipt.memo, receipt.amount)
    if receipt.outcome == "settled":
        if not found:
            return False
        tx_hash, depth = found
        required = MIN_CONFIRM_DEPTH if receipt.asset == "lapse" else 1
        return tx_hash == receipt.tx_hash and depth >= required
    # "missed": true only if no matching payment exists *and* enough time
    # has genuinely passed by this node's own view of the chain, not the
    # reporter's word for checked_at_height.
    if found:
        return False
    return engine.lapse.height() >= receipt.deadline_height + ABANDON_AFTER_BLOCKS


def _open_taker_trade(node, req, resp, order_row, confirm_depth):
    """Turn one accepted response into this side's own Trade. Returns
    True if it was created.

    confirm_depth here is this node's own local floor for recognising a
    leg as settled (Engine._required_depth folds it in); it is not what
    the trade's deadline is anchored to. That anchor is resp.accepted_height
    and resp.confirm_depth, the maker's own signed values, so both sides'
    rows agree on the one public number a bystander would also compute
    from, rather than each side privately picking its own.
    """
    xlm_total = swap.xlm_for_lapse(req.lapse_total,
                                   order_row.price_stroops_per_lapse)
    try:
        schedule = swap.build_schedule(req.lapse_total, xlm_total,
                                       resp.increment_count)
    except ValueError as e:
        log.warning(
            "[swap] the maker's accepted step count for %s does not "
            "build a usable schedule: %s", req.request_id[:16], e)
        return False

    i_send = "xlm" if order_row.direction == "sell" else "lapse"
    i_open = swap.opening_mover(
        *trust_mod.mutual_scores(node, order_row.maker_lapse_addr))
    if i_open is None:
        i_open = True   # an even match still needs somebody to start

    now = time.time()
    Trade.create(
        session_id=req.session_id, order_id=req.order_id, role="taker",
        my_lapse_addr=node.addr, my_xlm_addr=req.taker_xlm_addr,
        peer_lapse_addr=order_row.maker_lapse_addr,
        peer_xlm_addr=order_row.maker_xlm_addr,
        i_send=i_send, lapse_total=req.lapse_total, xlm_total=xlm_total,
        increment_count=resp.increment_count, confirm_depth=resp.confirm_depth,
        accepted_height=resp.accepted_height,
        status=TRADE_ACTIVE, created_at=now, updated_at=now)

    timeout = step_timeout_seconds(confirm_depth)
    for n, (lapse_amount, xlm_amount) in enumerate(schedule, start=1):
        i_move_first = swap.i_move_first(n, i_open)
        Increment.create(
            id=f"{req.session_id}:{n}", session_id=req.session_id, n=n,
            lapse_amount=lapse_amount, xlm_amount=xlm_amount,
            i_move_first=i_move_first, created_at=now, deadline_at=now + timeout,
            deadline_height=(deadline_height(resp.accepted_height, resp.confirm_depth)
                             if n == 1 else 0))

    log.info("[swap] %s opened: %d steps against %s (taker side)",
             req.session_id, resp.increment_count, order_row.maker_lapse_addr[:24])
    return True
