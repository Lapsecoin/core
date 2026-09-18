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
import time

import market as market_mod
import swap
import trust as trust_mod
import xlm as xlm_mod
from trade_storage import (
    Increment, Trade,
    LEG_PENDING, LEG_INTENT, LEG_SUBMITTED, LEG_SETTLED, LEG_DEAD,
    TRADE_ABANDONED, TRADE_ACTIVE, TRADE_COMPLETED, TRADE_STALLED,
    ensure_tables,
)

log = logging.getLogger("ec.swap_engine")

# Depth at which a LapseCoin leg counts as settled. Two, not one: see the
# module docstring. A user may raise this and not lower it.
MIN_CONFIRM_DEPTH = 2
DEFAULT_CONFIRM_DEPTH = 2

# How long one step may take before the trade is marked stalled. Built
# from the chain's own pace rather than a fixed number of seconds, so it
# tracks whatever the network is actually doing.
#
# A step costs one confirmation window on the slow side plus a few seconds
# on the fast one, and the multiplier is deliberately generous: the cost
# of waiting too long is a slow trade, and the cost of not waiting long
# enough is blaming somebody whose node was restarting.
STEP_TIMEOUT_MULTIPLIER = 6

# Past this, with the peer observably reachable throughout, a stall
# becomes abandonment. Roughly an hour on a two-minute chain. Long on
# purpose: reloading a long chain at startup is slow, and that must never
# read as defection.
ABANDON_AFTER_SECONDS = 3600


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

    def height(self):
        return self.node.view.chain[-1]["height"]

    def balance(self, addr):
        return self.node.view.state.get_balance(addr)

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
        for block_height, tx_hash in self.node.storage.get_tx_heights_for_addr(from_addr):
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

    def recent_incoming(self, to_addr, limit=200):
        """Every payment landing on `to_addr`, most recent first, as
        (from_addr, memo, amount, tx_hash, confirmations).

        For discovery, not for checking one already-expected payment (see
        find_payment): scanning for any inbound payment whose memo might
        name one of this node's own orders (see
        swap_engine.discover_trades). The mempool is searched too, at
        zero confirmations, for the same reason find_payment does: a
        payment still settling must not be missed.

        A self-payment (this node paying its own address) is excluded:
        whatever it means, it is never a counterparty's step, and this is
        the one place that distinction has to be made explicitly, since
        nothing downstream of this list re-derives sender identity.
        """
        import tx as tx_mod

        rows = []
        seen_hashes = set()
        for candidate in self.node.mempool.all_txs():
            if candidate.get("from") == to_addr:
                continue
            paid = sum(o["amount"] for o in candidate.get("outputs", [])
                       if o.get("to") == to_addr)
            if paid <= 0:
                continue
            h = tx_mod.tx_hash(candidate)
            seen_hashes.add(h)
            rows.append((candidate.get("from"), candidate.get("memo"), paid, h, 0))

        chain = self.node.view.chain
        tip = chain[-1]["height"]
        for block_height, tx_hash in self.node.storage.get_tx_heights_for_addr(to_addr):
            if tx_hash in seen_hashes:
                continue
            if not 0 <= block_height < len(chain):
                continue
            for candidate in chain[block_height]["transactions"]:
                if tx_mod.tx_hash(candidate) != tx_hash:
                    continue
                if candidate.get("from") == to_addr:
                    break
                paid = sum(o["amount"] for o in candidate.get("outputs", [])
                           if o.get("to") == to_addr)
                if paid <= 0:
                    break
                rows.append((candidate.get("from"), candidate.get("memo"),
                            paid, tx_hash, tip - block_height + 1))
                break
        return rows[:limit]

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

    def recent_incoming(self, to_addr, limit=200):
        """Every payment landing on `to_addr`, most recent first, as
        (from_addr, memo, amount, tx_hash, confirmations). See
        LapseAdapter.recent_incoming; this is the XLM half discovery
        needs. Confirmations is always the same large constant find_payment
        already reports, since Stellar is final on inclusion.
        """
        try:
            rows = xlm_mod.recent_incoming_payments(to_addr, limit)
        except xlm_mod.XLMUnreachable as e:
            raise Unreachable(str(e)) from e
        return [(sender, memo, amount, tx_hash, 10**9)
                for sender, memo, amount, tx_hash in rows]

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
    """
    return confirm_depth * block_seconds * STEP_TIMEOUT_MULTIPLIER


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class Engine:
    """Drives active trades forward. One pass per call, never blocking.

    Every pass re-derives what it needs from the chains, so a pass is
    safe to abandon at any point and safe to repeat. There is no state
    carried between passes that a restart could lose.
    """

    def __init__(self, lapse, xlm, secrets_provider):
        self.lapse = lapse
        self.xlm = xlm
        # Returns (kek, xlm_seed) or (None, None) when the wallet is
        # locked. Held behind a call rather than stored so a locked node
        # simply cannot send, which is the correct behaviour rather than
        # an error case.
        self.secrets = secrets_provider

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
        return max(trade.confirm_depth, MIN_CONFIRM_DEPTH) \
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

        Sponsored creation (xlm.build_sponsored_create_account) cannot
        be used here even though it would spare the buyer this cost: it
        requires a signature from the new account's own key in the same
        transaction (CAP-33's end-sponsoring operation is sourced by the
        sponsored account), which means active, real-time cooperation
        from the seller. Nothing in this protocol gives a payer any way
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

    def _complete(self, trade):
        if trade.status == TRADE_COMPLETED:
            return
        trade.status = TRADE_COMPLETED
        trade.updated_at = time.time()
        trade.stalled_since = 0.0
        trade.save()
        trust_mod.record_completed(trade.peer_lapse_addr, trade.lapse_total)
        log.info("[swap] %s completed: %d steps delivered",
                 trade.session_id, trade.increment_count)

    # -- blame ---------------------------------------------------------

    def consider_abandonment(self, trade):
        """Decide whether a long stall is finally somebody's fault.

        Three conditions, and the middle one is what makes this safe to
        act on:

        The deadline is long past. Wide on purpose, because a restarting
        node and a defecting one look identical for a while.

        The peer has already reciprocated at least one step. Their own
        signed transaction on chain is the acceptance, and nothing else
        here counts as one. Without this an attacker sends an unsolicited
        payment tagged with a session the victim never agreed to, waits,
        and reports them as a defector; with it, a step nobody answered
        proves only that nobody agreed, which is not a wrong.

        This node does not itself owe the next move. A trade held up by
        our own unsent leg is our problem, not theirs.

        Deliberately no longer asks whether the peer looked reachable.
        That came from liveness notes which said a node was powered on,
        not that it had seen this trade, and publishing them tied a
        payable address to the network. Reciprocation is both stronger
        evidence and free.
        """
        if trade.status != TRADE_STALLED or not trade.stalled_since:
            return False
        if time.time() - trade.stalled_since < ABANDON_AFTER_SECONDS:
            return False
        if not self._peer_ever_reciprocated(trade):
            log.debug("[swap] %s stalled, but the peer never accepted it; "
                      "no blame", trade.session_id)
            return False

        pending = (Increment.select()
                   .where(Increment.session_id == trade.session_id)
                   .order_by(Increment.n))
        for inc in pending:
            if inc.out_state == LEG_SETTLED and inc.in_state == LEG_SETTLED:
                continue
            # This node still owes its own leg, so the delay is not the
            # peer's to answer for.
            if inc.out_state != LEG_SETTLED and not inc.i_move_first:
                return False
            if inc.out_state == LEG_SETTLED and inc.in_state != LEG_SETTLED:
                trade.status = TRADE_ABANDONED
                trade.updated_at = time.time()
                trade.note = (f"step {inc.n}: paid and not reciprocated "
                              f"within {ABANDON_AFTER_SECONDS}s")
                trade.save()
                trust_mod.record_abandonment(
                    trade.peer_lapse_addr, trade.session_id)
                log.warning("[swap] %s abandoned by %s at step %d",
                            trade.session_id, trade.peer_lapse_addr[:24], inc.n)
                return True
            return False
        return False

    @staticmethod
    def _peer_ever_reciprocated(trade):
        """Whether the counterparty has settled a leg of their own here.

        This is the acceptance. There is no handshake message and none is
        needed: a settled inbound leg is a transaction they signed,
        carrying this trade's session tag, which nobody else could have
        produced. It says they saw the trade, agreed to its terms, and
        acted on them.

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
# Maker-side discovery
# ---------------------------------------------------------------------------
#
# There is no handshake and no separate "I accept" message. A maker learns
# a trade exists the same way anything else here learns anything: by
# reading a public chain. The taker always sends step 1 (see
# market_routes._start_trade), because nothing else could ever tell the
# maker a session exists in the first place; discovering that payment and
# creating this side's mirror of the trade is what this does.
#
# A claim supplies the one thing the payment alone cannot: the taker's
# address on whichever chain the payment did *not* arrive on. Everything
# else is independently re-derived and re-checked against this node's own
# rules, exactly as if a human had typed it into the take-order form:
# the fill fits the order, the schedule it implies fits this node's own
# exposure cap for this counterparty, and only a genuinely settled (or at
# least broadcast) payment ever creates anything.

def discover_trades(engine, node, my_xlm_addr, stranger_cap, confirm_depth):
    """Look for fills against this node's own orders and create the
    maker side of any that are ready. Returns how many were created.

    One Horizon call and one LapseCoin history scan at most per pass,
    however many orders or claims are waiting: the incoming-payment lists
    are fetched once each and matched against every pending claim in
    memory, not re-fetched per claim, since Horizon is a rate-limited
    public endpoint that every other trade this node runs is also
    reading from in the same pass.
    """
    ensure_tables()
    # Not orders_by_maker: that hides a cancelled or expired order, and a
    # claim that arrived (and was already paid for) before this node
    # cancelled its own order still deserves completion (see
    # market.orders_by_maker_with_claims).
    my_orders = market_mod.orders_by_maker_with_claims(node.addr)
    if not my_orders:
        return 0

    lapse_incoming = None
    xlm_incoming = None
    created = 0
    for order_row in my_orders:
        claims = market_mod.claims_for_order(order_row.order_id)
        if not claims:
            continue

        # The maker's own direction says which asset the taker pays
        # first: a "sell" order means the maker gives LAPSE, so the
        # taker's step 1 is in XLM, and vice versa.
        if order_row.direction == "sell":
            if xlm_incoming is None:
                xlm_incoming = engine.xlm.recent_incoming(my_xlm_addr)
            incoming = xlm_incoming
        else:
            if lapse_incoming is None:
                lapse_incoming = engine.lapse.recent_incoming(node.addr)
            incoming = lapse_incoming

        for claim in claims:
            if Trade.get_or_none(Trade.session_id == claim.session_id) is not None:
                continue
            if _discover_one(engine, node, my_xlm_addr, stranger_cap,
                             confirm_depth, order_row, claim, incoming):
                created += 1
    if created:
        log.info("[swap] discovered %d new trade(s) as maker", created)
    return created


def _discover_one(engine, node, my_xlm_addr, stranger_cap, confirm_depth,
                  order_row, claim, incoming):
    """Try to turn one claim against one order into a maker-side trade.
    Returns True if it created one.

    Order matters, cheapest and least trusted first: the claim's own
    shape was already checked before it was ever stored (market.verify_claim),
    but whether it makes sense is not, so the fill is checked against the
    order before anything else, the schedule it implies is checked
    against this node's own exposure cap before that schedule is trusted
    for anything, and only then is the incoming-payment list consulted
    for evidence this was actually paid for.
    """
    try:
        market_mod.validate_fill(order_row, claim.lapse_total)
    except market_mod.OrderRejected as e:
        log.debug("[swap] claim %s does not fit order %s: %s",
                 claim.session_id[:16], order_row.order_id, e)
        return False

    xlm_total = swap.xlm_for_lapse(claim.lapse_total,
                                   order_row.price_stroops_per_lapse)
    try:
        schedule = swap.build_schedule(claim.lapse_total, xlm_total,
                                       claim.increment_count)
    except ValueError as e:
        log.debug("[swap] claim %s has an inconsistent schedule: %s",
                 claim.session_id[:16], e)
        return False

    # Never trust the taker's chosen step count for what it implies about
    # risk: it decides how large a single step is, and a maker's safety
    # depends on that being checked against its *own* trust view of this
    # taker, not accepted because a stranger asserted it was fine.
    my_trust_of_taker, taker_trust_of_me = trust_mod.mutual_scores(
        node, claim.taker_lapse_addr)
    my_cap = swap.exposure_cap_stroops(my_trust_of_taker, stranger_cap)
    worst_step = max(xlm for _lapse, xlm in schedule)
    if worst_step > my_cap:
        log.warning(
            "[swap] refusing claim %s from %s: its schedule exposes %d "
            "stroops, above this node's own %d-stroop cap for them",
            claim.session_id[:16], claim.taker_lapse_addr[:24],
            worst_step, my_cap)
        return False

    # Which asset the maker sends is the mirror of the order's own
    # direction, exactly as market_routes._start_trade derives the
    # taker's from the same field.
    maker_i_send = "lapse" if order_row.direction == "sell" else "xlm"
    step1_lapse, step1_xlm = schedule[0]
    if maker_i_send == "lapse":
        taker_pay_addr, my_receive_addr = claim.taker_xlm_addr, my_xlm_addr
        expected_amount = step1_xlm
    else:
        taker_pay_addr, my_receive_addr = claim.taker_lapse_addr, node.addr
        expected_amount = step1_lapse

    memo = swap.session_tag(order_row.order_id, claim.session_id, 1)
    if not any(sender == taker_pay_addr and tag == memo and amount >= expected_amount
              for sender, tag, amount, _tx_hash, _conf in incoming):
        return False   # not paid (yet); tried again next pass

    # The taker has already paid; nothing checked so far says this node
    # can pay its own side back. Without this a maker who accepted an
    # order it cannot fully cover commits to a trade here, unattended,
    # that only surfaces the shortfall as a stall partway through, and a
    # stall is what blame is measured from. Same principle as the
    # taker's own check in market_routes._start_trade, just run on the
    # side that never gets a form to reject it from.
    if maker_i_send == "lapse":
        have, need = engine.lapse.balance(node.addr), claim.lapse_total
    else:
        have, need = engine.xlm.balance(my_xlm_addr), xlm_total
    if have < need:
        log.warning(
            "[swap] refusing claim %s from %s: this node cannot fund its "
            "own %s leg (has %d, needs %d)",
            claim.session_id[:16], claim.taker_lapse_addr[:24],
            maker_i_send, have, need)
        return False

    # opening_mover is computed from the same mutual, unforgeable data
    # either side of a dyad can derive on its own (trust.mutual_scores),
    # so it lands on the same answer the taker already used without
    # anything having to say so. Step 1 is still forced to the taker
    # regardless, for the reason the module docstring above gives.
    taker_i_open = swap.opening_mover(taker_trust_of_me, my_trust_of_taker)
    if taker_i_open is None:
        taker_i_open = True
    maker_i_open = not taker_i_open

    now = time.time()
    Trade.create(
        session_id=claim.session_id, order_id=order_row.order_id, role="maker",
        my_lapse_addr=node.addr, my_xlm_addr=my_xlm_addr,
        peer_lapse_addr=claim.taker_lapse_addr, peer_xlm_addr=claim.taker_xlm_addr,
        i_send=maker_i_send, lapse_total=claim.lapse_total, xlm_total=xlm_total,
        increment_count=claim.increment_count, confirm_depth=confirm_depth,
        status=TRADE_ACTIVE, created_at=now, updated_at=now)

    timeout = step_timeout_seconds(confirm_depth)
    for n, (lapse_amount, xlm_amount) in enumerate(schedule, start=1):
        i_move_first = False if n == 1 else swap.i_move_first(n, maker_i_open)
        Increment.create(
            id=f"{claim.session_id}:{n}", session_id=claim.session_id, n=n,
            lapse_amount=lapse_amount, xlm_amount=xlm_amount,
            i_move_first=i_move_first, created_at=now, deadline_at=now + timeout)

    log.info("[swap] %s discovered: %d steps against %s (maker side)",
             claim.session_id, claim.increment_count, claim.taker_lapse_addr[:24])
    return True
