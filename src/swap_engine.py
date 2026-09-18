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


class XLMAdapter:
    """The Stellar side. Thin: xlm.py already does the work."""

    asset = "xlm"

    def __init__(self, keyfile_path):
        self.keyfile_path = keyfile_path

    def balance(self, addr):
        return xlm_mod.get_spendable_stroops(addr)

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
            sequence = xlm_mod.get_sequence(
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
            envelope, tx_hash, seq = adapter.build(to_addr, amount, memo, seed)
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
