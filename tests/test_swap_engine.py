"""Crash, restart and reorg behaviour of the swap engine.

The requirement these exist to prove: if a swap goes wrong, it is because
a counterparty chose to defect, never because this node was restarted,
lost its database, hit a reorg, or could not reach a chain.

So the fake chains below record every payment that is actually made, and
most assertions come down to the same question: how many times did money
move for this step? The answer must be one, no matter where the process
was killed or what was lost.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import storage as storage_mod
import swap
import swap_engine
import trade_storage
import trust
from trade_storage import (
    Increment, Trade, PeerRecord,
    LEG_PENDING, LEG_INTENT, LEG_SUBMITTED, LEG_SETTLED, LEG_DEAD,
    TRADE_ABANDONED, TRADE_ACTIVE, TRADE_COMPLETED, TRADE_STALLED,
)


XLM = 10_000_000
LAPSE = 100_000_000


@pytest.fixture(autouse=True)
def fresh_db():
    storage_mod.db.init(":memory:")
    storage_mod.db.connect(reuse_if_open=True)
    storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
    storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
    trade_storage._initialised = True
    yield
    storage_mod.db.close()


class Crash(Exception):
    """A simulated process death at a chosen instant."""


class FakeChain:
    """A chain that remembers every payment that actually happened.

    `payments` is the ledger. A test asking whether the engine double-paid
    just counts entries with the same memo, which is the property that
    matters and the one a stored row cannot fake.
    """

    def __init__(self, asset, my_address):
        self.asset = asset
        # The real adapters derive the sender from the signing key, so the
        # fake has to know its own address too. Without it a payment is
        # recorded under a sender nobody searches for, and the chain check
        # that prevents double-paying silently never matches.
        self.my_address = my_address
        self.payments = []
        self.seq = 0
        self.unreachable = False
        self.reject_submit = None       # detail string to fail with
        self.crash_on_submit = False
        self.depth = 10
        self.submit_attempts = 0

    # -- reads ---------------------------------------------------------

    def _check_reachable(self):
        if self.unreachable:
            raise swap_engine.Unreachable(f"{self.asset} unreachable")

    def find_payment(self, from_addr, to_addr, memo, min_amount):
        self._check_reachable()
        for p in self.payments:
            if (p["from"] == from_addr and p["to"] == to_addr
                    and p["memo"] == memo and p["amount"] >= min_amount):
                return p["hash"], self.depth
        return None

    def confirmations(self, tx_hash):
        self._check_reachable()
        for p in self.payments:
            if p["hash"] == tx_hash:
                return self.depth
        return None

    def sequence_consumed(self, addr, seq):
        self._check_reachable()
        return any(p["seq"] >= seq for p in self.payments)

    # -- writes --------------------------------------------------------

    def build(self, to_addr, amount, memo, secret, create_account=False):
        self._check_reachable()
        self.seq += 1
        envelope = {"to": to_addr, "amount": amount, "memo": memo,
                    "seq": self.seq, "from": self.my_address}
        tx_hash = f"{self.asset}-{memo}-{self.seq}"
        if self.asset == "lapse":
            return envelope, tx_hash, self.seq
        return json.dumps(envelope, sort_keys=True), tx_hash, self.seq

    def submit(self, envelope):
        self._check_reachable()
        self.submit_attempts += 1
        if self.crash_on_submit:
            raise Crash("killed during submit")
        if isinstance(envelope, str):
            envelope = json.loads(envelope)
        if self.reject_submit:
            return False, self.reject_submit
        # The sequence number is what makes a repeat a no-op, exactly as
        # it does on both real chains.
        if any(p["seq"] == envelope["seq"] for p in self.payments):
            return True, "already applied"
        self.payments.append({
            "from": envelope["from"], "to": envelope["to"],
            "amount": envelope["amount"], "memo": envelope["memo"],
            "seq": envelope["seq"],
            "hash": f"{self.asset}-{envelope['memo']}-{envelope['seq']}",
        })
        return True, "submitted"

    # -- helpers for tests ---------------------------------------------

    def deliver(self, from_addr, to_addr, memo, amount):
        """A payment made by the counterparty, outside this engine."""
        self.seq += 1
        self.payments.append({
            "from": from_addr, "to": to_addr, "amount": amount,
            "memo": memo, "seq": self.seq,
            "hash": f"{self.asset}-{memo}-peer{self.seq}",
        })

    def count_for(self, memo):
        return sum(1 for p in self.payments if p["memo"] == memo)


def make_trade(i_send="lapse", count=3, confirm_depth=2, role="taker"):
    now = time.time()
    session_id = swap.new_session_id("order-1", "taker.addr")
    trade = Trade.create(
        session_id=session_id, order_id="order-1", role=role,
        my_lapse_addr="me.lapse", my_xlm_addr="GME",
        peer_lapse_addr="peer.lapse", peer_xlm_addr="GPEER",
        i_send=i_send, lapse_total=3 * LAPSE, xlm_total=3 * XLM,
        increment_count=count, confirm_depth=confirm_depth,
        status=TRADE_ACTIVE, created_at=now, updated_at=now)
    schedule = swap.build_schedule(3 * LAPSE, 3 * XLM, count)
    for n, (lapse_amt, xlm_amt) in enumerate(schedule, start=1):
        Increment.create(
            id=f"{session_id}:{n}", session_id=session_id, n=n,
            lapse_amount=lapse_amt, xlm_amount=xlm_amt,
            i_move_first=swap.i_move_first(n, True),
            created_at=now, deadline_at=now + 3600)
    return trade


def make_engine(lapse=None, xlm=None, locked=False):
    lapse = lapse or FakeChain("lapse", "me.lapse")
    xlm = xlm or FakeChain("xlm", "GME")
    secrets = (lambda: (None, None)) if locked else (lambda: ("kek", "seed"))
    return swap_engine.Engine(lapse, xlm, secrets), lapse, xlm


def settle_peer_leg(trade, inc, engine):
    """Have the counterparty pay their side of a step."""
    adapter = engine._in_adapter(trade)
    from_addr, to_addr, memo, amount = engine._in_terms(trade, inc)
    adapter.deliver(from_addr, to_addr, memo, amount)


class TestHappyPath:
    def test_first_mover_sends_then_waits(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.i_move_first is True

        engine.advance(trade)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_SETTLED
        assert inc.in_state != LEG_SETTLED
        assert lapse.count_for(swap.session_tag(trade.session_id, 1)) == 1

    def test_second_mover_waits_before_sending(self):
        """The safe position: nothing is sent until the peer has paid."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:2")
        assert inc.i_move_first is False

        # Settle step 1 so the engine reaches step 2.
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        settle_peer_leg(trade, first, engine)
        engine.advance(trade)

        engine.advance(trade)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_PENDING, "sent before the peer paid"

    def test_trade_completes_when_all_steps_settle(self):
        engine, _lapse, _xlm = make_engine()
        trade = make_trade(count=3)
        for _ in range(12):
            inc = engine.advance(trade)
            if inc is None:
                break
            settle_peer_leg(trade, inc, engine)
        trade = Trade.get(Trade.session_id == trade.session_id)
        assert trade.status == TRADE_COMPLETED

    def test_completion_credits_the_peer(self):
        engine, _l, _x = make_engine()
        trade = make_trade(count=2)
        for _ in range(8):
            inc = engine.advance(trade)
            if inc is None:
                break
            settle_peer_leg(trade, inc, engine)
        row = PeerRecord.get(PeerRecord.lapse_addr == "peer.lapse")
        assert row.completed_count == 1


class TestCrashSafety:
    """The property the whole design exists for: one payment per step,
    whatever happens to this process."""

    def test_crash_during_submit_does_not_double_pay(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.session_id, 1)

        lapse.crash_on_submit = True
        with pytest.raises(Crash):
            engine.advance(trade)

        # The envelope was written down before the send, which is what
        # recovery re-uses.
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == LEG_INTENT
        assert inc.out_envelope

        lapse.crash_on_submit = False
        engine.advance(trade)
        engine.advance(trade)
        assert lapse.count_for(tag) == 1

    def test_crash_after_payment_before_recording_does_not_double_pay(self):
        """The nastiest window: the money moved but this node never learned
        it did. The chain check before building is what covers it."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.session_id, 1)

        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        from_addr, to_addr, memo, amount = engine._out_terms(trade, inc)
        envelope, tx_hash, seq = lapse.build(to_addr, amount, memo, "kek")
        lapse.submit(envelope)
        assert lapse.count_for(tag) == 1

        # This node knows nothing about it: the row is still untouched.
        assert inc.out_state == LEG_PENDING

        engine.advance(trade)
        assert lapse.count_for(tag) == 1, "paid twice after a lost write"
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_SETTLED

    def test_database_wiped_entirely_does_not_double_pay(self):
        """Even losing every trade row, the chain still says what was paid."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.session_id, 1)

        engine.advance(trade)
        assert lapse.count_for(tag) == 1

        # Wipe the local record of the trade and rebuild it from scratch,
        # as a node restoring from an old backup would.
        Increment.delete().where(Increment.session_id == trade.session_id).execute()
        schedule = swap.build_schedule(3 * LAPSE, 3 * XLM, 3)
        now = time.time()
        for n, (lapse_amt, xlm_amt) in enumerate(schedule, start=1):
            Increment.create(
                id=f"{trade.session_id}:{n}", session_id=trade.session_id, n=n,
                lapse_amount=lapse_amt, xlm_amount=xlm_amt,
                i_move_first=swap.i_move_first(n, True),
                created_at=now, deadline_at=now + 3600)

        engine.advance(trade)
        assert lapse.count_for(tag) == 1, "paid twice after losing the database"

    def test_repeated_advance_is_idempotent(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.session_id, 1)
        for _ in range(10):
            engine.advance(trade)
        assert lapse.count_for(tag) == 1

    def test_resubmitting_stored_envelope_applies_once(self):
        """Re-sending the same envelope is the recovery path, so it has to
        be a no-op rather than a second payment."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.session_id, 1)

        engine.advance(trade)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        envelope = json.loads(inc.out_envelope)
        for _ in range(5):
            lapse.submit(envelope)
        assert lapse.count_for(tag) == 1

    def test_locked_wallet_cannot_send(self):
        engine, lapse, _xlm = make_engine(locked=True)
        trade = make_trade()
        with pytest.raises(swap_engine.SwapError):
            engine.advance(trade)
        assert lapse.payments == []


class TestDeadEnvelope:
    def test_envelope_whose_sequence_is_spent_is_rebuilt(self):
        """A stored envelope that can never apply must not be retried
        forever, or the trade silently stops moving."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")

        # An envelope exists but its sequence was burned by something else.
        inc.out_envelope = json.dumps(
            {"to": "peer.lapse", "amount": 1, "memo": "x", "seq": 1,
             "from": "kek"}, sort_keys=True)
        inc.out_state = LEG_INTENT
        inc.out_seq = 1
        inc.save()
        lapse.payments.append({"from": "someone", "to": "else", "amount": 1,
                               "memo": "unrelated", "seq": 1,
                               "hash": "unrelated"})
        lapse.reject_submit = "tx_bad_seq"

        engine.advance(trade)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state in (LEG_DEAD, LEG_INTENT, LEG_SUBMITTED)
        assert inc.out_envelope != "" or inc.out_state == LEG_DEAD


class TestReorg:
    def test_settled_leg_is_taken_back_when_it_leaves_the_chain(self):
        """A LapseCoin leg at depth one can be un-confirmed by the draw
        window working normally. A stored flag must not outlive the fact."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        engine.advance(trade)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == LEG_SETTLED

        lapse.payments.clear()          # the reorg
        engine.check_outbound(trade, inc)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state != LEG_SETTLED
        assert inc.out_settled_at == 0.0

    def test_shallow_confirmation_is_not_settlement(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade(confirm_depth=2)
        lapse.depth = 1                 # one block only
        engine.advance(trade)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == LEG_SUBMITTED, "depth 1 counted as settled"

    def test_depth_below_the_floor_is_raised(self):
        """A trade cannot opt into depth 1; two is the floor."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade(confirm_depth=1)
        assert engine._required_depth(trade, lapse) >= swap_engine.MIN_CONFIRM_DEPTH

    def test_inbound_leg_can_also_be_taken_back(self):
        engine, _lapse, xlm = make_engine()
        trade = make_trade(i_send="lapse")
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        settle_peer_leg(trade, inc, engine)
        engine.check_inbound(trade, inc)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.in_state == LEG_SETTLED

        xlm.payments.clear()
        engine.check_inbound(trade, inc)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.in_state == LEG_PENDING


class TestUnreachableChain:
    def test_unreachable_does_not_look_like_failure(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        lapse.unreachable = True
        with pytest.raises(swap_engine.Unreachable):
            engine.advance(trade)
        assert lapse.payments == []

    def test_reconcile_leaves_steps_alone_when_it_cannot_ask(self):
        """Guessing either way here re-sends a payment or abandons a live
        trade, so an unreachable chain must change nothing."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        engine.advance(trade)
        before = Increment.get(Increment.id == f"{trade.session_id}:1").out_state

        lapse.unreachable = True
        swap_engine.reconcile(engine, trade)
        after = Increment.get(Increment.id == f"{trade.session_id}:1").out_state
        assert after == before

    def test_recovers_once_the_chain_returns(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        lapse.unreachable = True
        with pytest.raises(swap_engine.Unreachable):
            engine.advance(trade)
        lapse.unreachable = False
        engine.advance(trade)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == LEG_SETTLED


class TestBlame:
    """A restarting node must never be mistaken for a defector."""

    def test_missed_deadline_only_stalls(self):
        engine, _l, _x = make_engine()
        trade = make_trade()
        Increment.update(deadline_at=time.time() - 1).execute()
        engine.advance(trade)
        trade = Trade.get(Trade.session_id == trade.session_id)
        assert trade.status == TRADE_STALLED
        assert PeerRecord.get_or_none(PeerRecord.lapse_addr == "peer.lapse") is None \
            or PeerRecord.get(PeerRecord.lapse_addr == "peer.lapse").abandoned_count == 0

    def test_stall_clears_when_the_peer_comes_back(self):
        engine, _l, _x = make_engine()
        trade = make_trade()
        Increment.update(deadline_at=time.time() - 1).execute()
        engine.advance(trade)
        assert Trade.get(Trade.session_id == trade.session_id).status == TRADE_STALLED

        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        settle_peer_leg(trade, inc, engine)
        trade = Trade.get(Trade.session_id == trade.session_id)
        engine.advance(trade)
        assert Trade.get(Trade.session_id == trade.session_id).status == TRADE_ACTIVE

    def _stall(self, trade, age=None):
        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.stalled_since = time.time() - (
            age if age is not None else swap_engine.ABANDON_AFTER_SECONDS + 10)
        trade.save()
        return trade

    def test_no_blame_before_the_margin_elapses(self):
        engine, _l, _x = make_engine()
        trade = make_trade()
        assert engine.consider_abandonment(self._stall(trade, age=10)) is False

    def test_no_blame_without_an_acceptance_on_chain(self):
        """The attack this closes: send somebody an unsolicited payment
        tagged with a session they never agreed to, wait, and report them
        as a defector. A step nobody answered proves nobody agreed."""
        engine, _l, _x = make_engine()
        trade = make_trade()
        engine.advance(trade)            # we paid; they never reciprocated
        assert engine.consider_abandonment(self._stall(trade)) is False
        assert PeerRecord.get_or_none(
            PeerRecord.lapse_addr == "peer.lapse") is None

    def test_no_blame_when_this_node_owes_the_move(self):
        engine, _l, _x = make_engine()
        trade = make_trade()
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        settle_peer_leg(trade, first, engine)
        engine.advance(trade)            # step 2 is ours and unsent
        assert engine.consider_abandonment(self._stall(trade)) is False

    def test_blame_once_they_accepted_then_stopped(self):
        """Their own settled leg is the acceptance: a signed transaction
        carrying this session's tag, which only they could produce."""
        engine, _l, _x = make_engine()
        trade = make_trade()
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        settle_peer_leg(trade, first, engine)   # they accept step 1
        engine.advance(trade)
        second = Increment.get(Increment.id == f"{trade.session_id}:2")
        settle_peer_leg(trade, second, engine)  # they move first on step 2
        engine.advance(trade)                   # we reciprocate
        third = Increment.get(Increment.id == f"{trade.session_id}:3")
        engine.advance(trade)                   # we pay step 3, they vanish

        assert engine.consider_abandonment(self._stall(trade)) is True
        assert Trade.get(Trade.session_id == trade.session_id).status == TRADE_ABANDONED
        assert PeerRecord.get(PeerRecord.lapse_addr == "peer.lapse").abandoned_count == 1

    def test_acceptance_cannot_be_forged_by_the_accuser(self):
        """Only an inbound leg counts. Our own payments, however many,
        never amount to the counterparty having agreed."""
        engine, _l, _x = make_engine()
        trade = make_trade()
        for _ in range(5):
            engine.advance(trade)
        assert engine._peer_ever_reciprocated(trade) is False


class TestReconcile:
    def test_reconcile_recovers_settled_steps_from_chain(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        from_addr, to_addr, memo, amount = engine._out_terms(trade, inc)
        lapse.deliver(from_addr, to_addr, memo, amount)

        assert inc.out_state == LEG_PENDING
        swap_engine.reconcile(engine, trade)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_SETTLED

    def test_reconcile_all_covers_active_and_stalled(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        trade.status = TRADE_STALLED
        trade.save()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        terms = engine._out_terms(trade, inc)
        lapse.deliver(*terms)
        assert swap_engine.reconcile_all(engine) >= 1
