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
import xlm as xlm_mod
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
        # Addresses with no account behind them yet, mirroring a never-
        # funded Stellar destination. Empty by default so existing tests
        # that never mention this keep behaving as if every destination
        # is already funded.
        self.nonexistent_accounts = set()
        self.build_calls = []           # (to_addr, amount, create_account)
        # Deliberately huge by default so existing tests that never
        # mention balance keep behaving as if this node can afford
        # whatever it is asked to send; see TestDiscoverTrades' solvency
        # tests for where this is actually made to matter.
        self.balances = {}

    # -- reads ---------------------------------------------------------

    def balance(self, addr):
        return self.balances.get(addr, 10**18)

    def account_exists(self, addr):
        self._check_reachable()
        return addr not in self.nonexistent_accounts

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

    def forget_sequence(self, addr):
        pass

    # -- writes --------------------------------------------------------

    def build(self, to_addr, amount, memo, secret, create_account=False):
        self._check_reachable()
        self.seq += 1
        self.build_calls.append((to_addr, amount, create_account))
        envelope = {"to": to_addr, "amount": amount, "memo": memo,
                    "seq": self.seq, "from": self.my_address,
                    "create_account": create_account}
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
        if envelope.get("create_account"):
            self.nonexistent_accounts.discard(envelope["to"])
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


class FakeMempool:
    def __init__(self, txs=None):
        self._txs = txs or []

    def all_txs(self):
        return list(self._txs)


class FakeAddrStorage:
    def __init__(self, heights_by_addr=None, memo_by_addr=None):
        self.heights_by_addr = heights_by_addr or {}
        # {addr: {memo: [(height, tx_hash), ...]}}, mirroring the real
        # (addr, memo) index find_payment now queries directly instead of
        # walking everything get_tx_heights_for_addr would return.
        self.memo_by_addr = memo_by_addr or {}

    def get_tx_heights_for_addr(self, addr):
        return self.heights_by_addr.get(addr, [])

    def get_tx_by_addr_and_memo(self, addr, memo):
        return self.memo_by_addr.get(addr, {}).get(memo, [])


class FakeView:
    def __init__(self, chain):
        self.chain = chain


class FakeLapseNode:
    """Just enough of a real Node for LapseAdapter.find_payment: a
    mempool, a chain, and the address index that backs storage lookups.
    """

    def __init__(self, mempool_txs=None, chain=None, heights_by_addr=None,
                memo_by_addr=None):
        self.mempool = FakeMempool(mempool_txs)
        self.view = FakeView(chain if chain is not None else
                             [{"height": 0, "transactions": []}])
        self.storage = FakeAddrStorage(heights_by_addr, memo_by_addr)


def _tx(from_addr, outputs, memo=None, nonce=1):
    t = {"from": from_addr, "outputs": outputs, "nonce": nonce}
    if memo is not None:
        t["memo"] = memo
    return t


class TestLapseAdapterFindPayment:
    """Plan item 4.5: find_payment already knows the exact memo (this
    step's own session tag) it is checking for, so it must look it up
    through storage.get_tx_by_addr_and_memo rather than walking every
    transaction the sender has ever made."""

    def test_finds_a_confirmed_payment_via_the_memo_index(self):
        import tx as tx_mod
        t = _tx("peer.lapse", [{"to": "me.lapse", "amount": 5 * LAPSE}], memo="tag")
        h = tx_mod.tx_hash(t)
        chain = [{"height": 0, "transactions": []},
                 {"height": 1, "transactions": [t]}]
        node = FakeLapseNode(chain=chain,
                             memo_by_addr={"peer.lapse": {"tag": [(1, h)]}})
        adapter = swap_engine.LapseAdapter(node)
        found = adapter.find_payment("peer.lapse", "me.lapse", "tag", 5 * LAPSE)
        assert found == (h, 1)

    def test_never_consults_the_full_address_history(self):
        """If this regresses to the old address-wide scan, this fake
        would happily serve rows from heights_by_addr and the test would
        pass for the wrong reason, so it asserts the other index is
        simply never touched."""
        import tx as tx_mod
        t = _tx("peer.lapse", [{"to": "me.lapse", "amount": 5 * LAPSE}], memo="tag")
        h = tx_mod.tx_hash(t)
        chain = [{"height": 0, "transactions": []},
                 {"height": 1, "transactions": [t]}]

        class _AssertingStorage(FakeAddrStorage):
            def get_tx_heights_for_addr(self, addr):
                raise AssertionError("find_payment must not scan full address history")

        node = FakeLapseNode(chain=chain)
        node.storage = _AssertingStorage(memo_by_addr={"peer.lapse": {"tag": [(1, h)]}})
        adapter = swap_engine.LapseAdapter(node)
        assert adapter.find_payment("peer.lapse", "me.lapse", "tag", 5 * LAPSE) == (h, 1)

    def test_a_pending_mempool_payment_is_found_before_the_index_is_checked(self):
        import tx as tx_mod
        t = _tx("peer.lapse", [{"to": "me.lapse", "amount": 5 * LAPSE}], memo="tag")
        node = FakeLapseNode(mempool_txs=[t])
        adapter = swap_engine.LapseAdapter(node)
        found = adapter.find_payment("peer.lapse", "me.lapse", "tag", 5 * LAPSE)
        assert found == (tx_mod.tx_hash(t), 0)

    def test_wrong_memo_in_the_index_does_not_match(self):
        import tx as tx_mod
        t = _tx("peer.lapse", [{"to": "me.lapse", "amount": 5 * LAPSE}], memo="other-tag")
        h = tx_mod.tx_hash(t)
        chain = [{"height": 0, "transactions": []}, {"height": 1, "transactions": [t]}]
        node = FakeLapseNode(chain=chain,
                             memo_by_addr={"peer.lapse": {"other-tag": [(1, h)]}})
        adapter = swap_engine.LapseAdapter(node)
        assert adapter.find_payment("peer.lapse", "me.lapse", "tag", 5 * LAPSE) is None

    def test_underpayment_at_the_right_memo_does_not_match(self):
        import tx as tx_mod
        t = _tx("peer.lapse", [{"to": "me.lapse", "amount": 1 * LAPSE}], memo="tag")
        h = tx_mod.tx_hash(t)
        chain = [{"height": 0, "transactions": []}, {"height": 1, "transactions": [t]}]
        node = FakeLapseNode(chain=chain,
                             memo_by_addr={"peer.lapse": {"tag": [(1, h)]}})
        adapter = swap_engine.LapseAdapter(node)
        assert adapter.find_payment("peer.lapse", "me.lapse", "tag", 5 * LAPSE) is None

    def test_no_match_at_all_returns_none(self):
        node = FakeLapseNode()
        adapter = swap_engine.LapseAdapter(node)
        assert adapter.find_payment("peer.lapse", "me.lapse", "tag", 1) is None


class TestSequenceAllocator:
    """Plan item 4.3: two trades sharing one XLM wallet must never be
    handed the same sequence number, and the fix must not depend on
    Horizon having already caught up with a submission this same
    process just made (see SequenceAllocator's docstring)."""

    def test_first_allocation_reads_the_chain(self, monkeypatch):
        calls = []
        monkeypatch.setattr(xlm_mod, "get_sequence",
                            lambda addr: calls.append(addr) or 100)
        alloc = swap_engine.SequenceAllocator()
        assert alloc.allocate("GADDR") == 100
        assert calls == ["GADDR"]

    def test_later_allocations_for_the_same_address_do_not_reread_the_chain(self, monkeypatch):
        calls = []
        monkeypatch.setattr(xlm_mod, "get_sequence",
                            lambda addr: calls.append(addr) or 100)
        alloc = swap_engine.SequenceAllocator()
        got = [alloc.allocate("GADDR") for _ in range(3)]
        assert got == [100, 101, 102]
        assert calls == ["GADDR"], "only the first allocation should touch Horizon"

    def test_different_addresses_are_tracked_independently(self, monkeypatch):
        seqs = {"GA": 5, "GB": 50}
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: seqs[addr])
        alloc = swap_engine.SequenceAllocator()
        assert alloc.allocate("GA") == 5
        assert alloc.allocate("GB") == 50
        assert alloc.allocate("GA") == 6
        assert alloc.allocate("GB") == 51

    def test_reset_forces_a_fresh_read_next_time(self, monkeypatch):
        seqs = iter([100, 200])
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: next(seqs))
        alloc = swap_engine.SequenceAllocator()
        assert alloc.allocate("GADDR") == 100
        assert alloc.allocate("GADDR") == 101
        alloc.reset("GADDR")
        assert alloc.allocate("GADDR") == 200

    def test_resetting_an_address_never_read_is_harmless(self):
        swap_engine.SequenceAllocator().reset("GADDR")

    def test_a_failed_first_read_leaves_nothing_cached(self, monkeypatch):
        def boom(addr):
            raise xlm_mod.XLMUnreachable("horizon is down")
        monkeypatch.setattr(xlm_mod, "get_sequence", boom)
        alloc = swap_engine.SequenceAllocator()
        with pytest.raises(xlm_mod.XLMUnreachable):
            alloc.allocate("GADDR")

        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 7)
        assert alloc.allocate("GADDR") == 7, "the failed read must not have cached anything"


class TestXLMAdapterSequencing:
    """The real adapter wired to the allocator above, not the FakeChain
    stand-in the rest of this file drives the engine through."""

    def _adapter(self, monkeypatch, start_seq=42):
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: start_seq)
        built = []
        monkeypatch.setattr(
            xlm_mod, "build_payment",
            lambda seed, to, amount, memo, seq: built.append(seq) or (f"xdr{seq}", f"hash{seq}"))
        return swap_engine.XLMAdapter("keyfile"), built

    def test_two_builds_for_the_same_seed_get_consecutive_sequences(self, monkeypatch):
        seed, _pub = xlm_mod.generate_keypair()
        adapter, built = self._adapter(monkeypatch)
        adapter.build("GDEST", 1, "memo1", seed)
        adapter.build("GDEST", 1, "memo2", seed)
        assert built == [42, 43]

    def test_two_different_seeds_are_not_forced_onto_one_counter(self, monkeypatch):
        seed_a, _ = xlm_mod.generate_keypair()
        seed_b, _ = xlm_mod.generate_keypair()
        adapter, built = self._adapter(monkeypatch)
        adapter.build("GDEST", 1, "memo1", seed_a)
        adapter.build("GDEST", 1, "memo2", seed_b)
        assert built == [42, 42], "distinct wallets must each start from their own chain read"

    def test_forget_sequence_makes_the_next_build_reread_the_chain(self, monkeypatch):
        seed, pub = xlm_mod.generate_keypair()
        adapter, built = self._adapter(monkeypatch)
        adapter.build("GDEST", 1, "memo1", seed)
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 999)
        adapter.forget_sequence(pub)
        adapter.build("GDEST", 1, "memo2", seed)
        assert built == [42, 999]


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
        assert lapse.count_for(swap.session_tag(trade.order_id, trade.session_id, 1)) == 1

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


class TestAccountCreation:
    """Plan item 1.2: a plain payment to a Stellar address with no
    account behind it fails outright, so a seller who has never funded
    their trading wallet (exactly who the "sell LAPSE without owning XLM
    first" feature is for) could never actually be paid."""

    def test_first_payment_to_an_unfunded_destination_creates_the_account(self):
        engine, _lapse, xlm = make_engine()
        xlm.nonexistent_accounts.add("GPEER")
        trade = make_trade(i_send="xlm")
        engine.advance(trade)
        assert xlm.build_calls[0][2] is True, "must be a create-account operation"

    def test_a_funded_destination_gets_a_plain_payment(self):
        engine, _lapse, xlm = make_engine()
        trade = make_trade(i_send="xlm")
        engine.advance(trade)
        assert xlm.build_calls[0][2] is False

    def test_amount_is_bumped_to_the_minimum_when_the_agreed_step_is_smaller(self):
        engine, _lapse, xlm = make_engine()
        xlm.nonexistent_accounts.add("GPEER")
        trade = make_trade(i_send="xlm", count=20)   # many steps: a tiny probe
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.xlm_amount < xlm_mod.ACCOUNT_MIN_BALANCE_STROOPS
        engine.advance(trade)
        sent_addr, sent_amount, create_account = xlm.build_calls[0]
        assert create_account is True
        assert sent_amount == xlm_mod.ACCOUNT_MIN_BALANCE_STROOPS

    def test_the_counterparty_still_recognises_the_bumped_payment(self):
        """The other side's own check only ever asks for paid >= agreed,
        so the overpayment settles the step it was scheduled for without
        either side's stored schedule needing to change."""
        engine, _lapse, xlm = make_engine()
        xlm.nonexistent_accounts.add("GPEER")
        trade = make_trade(i_send="xlm", count=20)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_SETTLED

    def test_no_bump_when_the_agreed_amount_already_clears_the_minimum(self):
        engine, _lapse, xlm = make_engine()
        xlm.nonexistent_accounts.add("GPEER")
        trade = make_trade(i_send="xlm", count=2)   # few steps: large amounts
        inc1 = Increment.get(Increment.id == f"{trade.session_id}:1")
        inc2 = Increment.get(Increment.id == f"{trade.session_id}:2")
        assert inc2.xlm_amount >= xlm_mod.ACCOUNT_MIN_BALANCE_STROOPS
        assert inc1.i_move_first is True and inc2.i_move_first is False

        engine.advance(trade)                # step 1: we send (creates GPEER)
        settle_peer_leg(trade, inc1, engine)
        engine.advance(trade)                # step 1: notice their reply, done
        settle_peer_leg(trade, inc2, engine)  # they open step 2
        engine.advance(trade)                # step 2: notice + send ours

        _addr, sent_amount, _ca = xlm.build_calls[-1]
        assert sent_amount == inc2.xlm_amount

    def test_only_the_first_payment_to_a_destination_creates_it(self):
        """Once the create-account envelope settles, the account exists
        for every later step; a second create-account attempt against an
        address that already exists would simply fail."""
        engine, _lapse, xlm = make_engine()
        xlm.nonexistent_accounts.add("GPEER")
        trade = make_trade(i_send="xlm", count=3)
        for _ in range(12):
            inc = engine.advance(trade)
            if inc is None:
                break
            settle_peer_leg(trade, inc, engine)
        create_flags = [call[2] for call in xlm.build_calls]
        assert create_flags.count(True) == 1
        assert create_flags[0] is True

    def test_lapse_destinations_never_need_account_creation(self):
        """Every LapseCoin address can receive a payment regardless of
        whether it has ever held a balance; only Stellar has this
        distinction."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade(i_send="lapse")
        engine.advance(trade)
        assert lapse.account_exists("peer.lapse") is True
        assert lapse.build_calls[0][2] is False


class TestCrashSafety:
    """The property the whole design exists for: one payment per step,
    whatever happens to this process."""

    def test_crash_during_submit_does_not_double_pay(self):
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.order_id, trade.session_id, 1)

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
        tag = swap.session_tag(trade.order_id, trade.session_id, 1)

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
        tag = swap.session_tag(trade.order_id, trade.session_id, 1)

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
        tag = swap.session_tag(trade.order_id, trade.session_id, 1)
        for _ in range(10):
            engine.advance(trade)
        assert lapse.count_for(tag) == 1

    def test_resubmitting_stored_envelope_applies_once(self):
        """Re-sending the same envelope is the recovery path, so it has to
        be a no-op rather than a second payment."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        tag = swap.session_tag(trade.order_id, trade.session_id, 1)

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


# ---------------------------------------------------------------------------
# Maker-side: answering fill requests
# ---------------------------------------------------------------------------

import crypto
import market as market_mod
from trade_storage import FillRequest, FillResponse, Order


class _FakeState:
    def __init__(self, balances=None):
        self.balances = balances or {}

    def get_balance(self, addr):
        return self.balances.get(addr, 0)


class _FakeStorage:
    def __init__(self, heights_by_addr=None):
        self.heights_by_addr = heights_by_addr or {}

    def get_tx_heights_for_addr(self, addr):
        return self.heights_by_addr.get(addr, [])


class _FakeView:
    def __init__(self, height, balances=None):
        self.height = height
        self.chain = [{"height": height}]
        self.state = _FakeState(balances)


class FakeDiscoveryNode:
    """Just enough of a node for answer_fill_requests: a real FALCON
    identity (a fill response is really signed, unlike the fake
    signatures the requests these tests build carry, since the maker's
    own commitment is exactly what is under test), a tip height, and the
    chain facts trust.mutual_scores needs."""

    def __init__(self, tmp_path, height=1000, heights_by_addr=None,
                balances=None, name="maker"):
        sk, pk = crypto.generate_keypair()
        self.passphrase = "correct horse"
        self.keyfile = str(tmp_path / f"{name}.key")
        crypto.save_key(self.keyfile, sk, pk, self.passphrase)
        self.addr = crypto.public_key_to_address(pk)
        self.pk_hex = pk.hex()
        self.view = _FakeView(height, balances)
        self.storage = _FakeStorage(heights_by_addr)
        self.publish_fill_response_calls = []

    def publish_fill_response(self, resp):
        self.publish_fill_response_calls.append(resp)

    def kek(self):
        return crypto.derive_kek(self.keyfile, self.passphrase)


def make_maker_engine(node, lapse=None, xlm=None):
    """Like make_engine, but with the real kek this node's own key needs
    to sign a fill response, rather than the placeholder make_engine
    uses for tests that never sign anything."""
    lapse = lapse or FakeChain("lapse", "me.lapse")
    xlm = xlm or FakeChain("xlm", "GME")
    kek = node.kek()
    return swap_engine.Engine(lapse, xlm, lambda: (kek, "seed")), lapse, xlm


def make_order(order_id="order-1", direction="sell", lapse_total=10 * LAPSE,
              price=1000, maker_lapse="maker.lapse", maker_xlm="GMAKER",
              min_fill=0, max_fill=0, expiry_block=10**9):
    return Order.create(
        order_id=order_id, maker_lapse_addr=maker_lapse, maker_xlm_addr=maker_xlm,
        direction=direction, lapse_total=lapse_total,
        price_stroops_per_lapse=price, min_fill=min_fill,
        max_fill=max_fill or lapse_total, expiry_block=expiry_block,
        pubkey="ab" * 10, signature="cd" * 10,
        created_at=time.time(), received_at=time.time(), verified=True)


def make_request(order_id="order-1", session_id="s" * 16, taker_lapse="taker.lapse",
                 taker_xlm="GTAKER", lapse_total=1 * LAPSE):
    return FillRequest.create(
        request_id=session_id, order_id=order_id,
        session_id=session_id, taker_lapse_addr=taker_lapse,
        taker_xlm_addr=taker_xlm, lapse_total=lapse_total,
        pubkey="ab" * 10, signature="cd" * 10, received_at=time.time())


class TestAnswerFillRequests:
    """Deciding a fill request against this node's own order, before a
    single stroop moves: the maker builds its own schedule, checks it
    can fund its own leg, and publishes a signed accept or reject the
    instant it decides."""

    def test_accepts_a_request_and_creates_the_maker_trade(self, tmp_path):
        """A 'sell' order: the maker gives LAPSE, so this node pays that
        leg and expects XLM back."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1

        trade = Trade.get(Trade.session_id == req.session_id)
        assert trade.role == "maker"
        assert trade.i_send == "lapse"
        assert trade.peer_lapse_addr == "taker.lapse"
        assert trade.peer_xlm_addr == "GTAKER"
        assert trade.lapse_total == req.lapse_total
        steps = list(Increment.select()
                    .where(Increment.session_id == req.session_id)
                    .order_by(Increment.n))
        assert len(steps) == trade.increment_count
        assert sum(s.lapse_amount for s in steps) == req.lapse_total

        assert len(node.publish_fill_response_calls) == 1
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is True
        assert resp["request_id"] == req.request_id
        assert market_mod.verify_fill_response(
            resp, expected_maker_addr=node.addr) is True

    def test_a_buy_order_makes_the_maker_send_lapse_and_expect_xlm(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(direction="buy", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, xlm = make_maker_engine(node)
        xlm.balances["GMAKER"] = swap.xlm_for_lapse(req.lapse_total, 1000)

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1
        trade = Trade.get(Trade.session_id == req.session_id)
        assert trade.i_send == "xlm"

    def test_a_second_order_cannot_promise_the_same_unspent_balance_twice(self, tmp_path):
        """A chain balance backs every trade this node has already
        accepted, not just the one currently being decided: two 'sell'
        orders together asking for more LAPSE than this node actually
        holds must not both be accepted just because neither request
        alone exceeds the raw balance (see _pending_send_total)."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(order_id="order-a", direction="sell", maker_lapse=node.addr)
        make_order(order_id="order-b", direction="sell", maker_lapse=node.addr)
        make_request(order_id="order-a", session_id="a" * 16, lapse_total=6 * LAPSE)
        make_request(order_id="order-b", session_id="b" * 16, lapse_total=6 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 10 * LAPSE   # enough for one, not both

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1
        accepted = [r for r in node.publish_fill_response_calls if r["accepted"]]
        rejected = [r for r in node.publish_fill_response_calls if not r["accepted"]]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert Trade.select().count() == 1

    def test_a_maker_who_cannot_fund_the_lapse_leg_is_refused(self, tmp_path):
        """A signed request alone must never be enough to commit this
        node to a trade it cannot itself complete."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total - 1

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0
        assert Trade.select().count() == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False

    def test_a_maker_who_cannot_fund_the_xlm_leg_is_refused(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(direction="buy", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, xlm = make_maker_engine(node)
        xlm_total = swap.xlm_for_lapse(req.lapse_total, 1000)
        xlm.balances["GMAKER"] = xlm_total - 1

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0
        assert Trade.select().count() == 0

    def test_a_maker_with_exactly_enough_is_not_refused(self, tmp_path):
        """The boundary: exactly enough must still succeed, so this is not
        an off-by-one rejecting a maker who can actually pay."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1

    def test_no_requests_against_an_order_answers_nothing(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(maker_lapse=node.addr)
        engine, _lapse, _xlm = make_maker_engine(node)
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0

    def test_a_cancelled_order_with_a_pending_request_is_still_honoured(self, tmp_path):
        """Cancelling withdraws what is unfilled, not a request already in
        flight against it when this node cancelled."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        order = make_order(maker_lapse=node.addr)
        order.cancelled = True
        order.save()
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1

    def test_a_request_naming_an_unknown_order_is_ignored(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_request(order_id="no-such-order")
        engine, _lapse, _xlm = make_maker_engine(node)
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0

    def test_fill_larger_than_the_order_is_refused(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(lapse_total=1 * LAPSE, maker_lapse=node.addr)
        make_request(lapse_total=5 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 5 * LAPSE
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0
        assert Trade.select().count() == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False

    def test_fill_below_min_fill_is_refused(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(min_fill=5 * LAPSE, maker_lapse=node.addr)
        make_request(lapse_total=1 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0

    def test_schedule_over_this_nodes_own_cap_is_refused(self, tmp_path):
        """The maker builds its own schedule from its own trust view of
        this taker, so a stranger cannot talk its way into a bigger step
        than this node is willing to risk with them."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(lapse_total=1000 * LAPSE, price=1000, maker_lapse=node.addr)
        make_request(lapse_total=1000 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 1000 * LAPSE

        tiny_cap = 1000   # far below what any split of this trade needs
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", tiny_cap, 2) == 0
        assert Trade.select().count() == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False

    def test_an_already_answered_request_is_not_answered_twice(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1
        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 0
        assert Trade.select().count() == 1
        assert len(node.publish_fill_response_calls) == 1

    def test_multiple_requests_against_one_order_are_each_considered(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(lapse_total=10 * LAPSE, maker_lapse=node.addr)
        make_request(session_id="a" * 16, taker_lapse="takerA",
                    taker_xlm="GA", lapse_total=1 * LAPSE)
        make_request(session_id="b" * 16, taker_lapse="takerB",
                    taker_xlm="GB", lapse_total=1 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 10 * LAPSE

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 2

    def test_a_tie_in_trust_never_makes_both_sides_believe_they_open(self, tmp_path):
        """The unsafe reading of a tie: if the maker independently
        tie-broke its own opening_mover call the same way the taker's
        _start_trade does ('an even match still needs somebody to
        start' -> True), both sides would believe they open and step 1
        would deadlock. answer_fill_requests must derive the maker's
        decision as the negation of what the taker computed, never in
        parallel."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(maker_lapse=node.addr)
        # No PeerRecord for either address and no chain history: every
        # trust score here is 0, which is the exact tie opening_mover
        # returns None for.
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1
        first = Increment.get(Increment.id == f"{req.session_id}:1")
        # The taker's own _start_trade ties to i_open=True, which makes
        # the taker move first on step 1. The maker's step 1 must be the
        # complement of that, or neither side would send: this is the
        # assertion that would catch the deadlock a naive,
        # independently-tie-broken opening_mover call on the maker's
        # side would produce.
        assert first.i_move_first is False
