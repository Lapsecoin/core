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
import threading
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
    Increment, Trade,
    LEG_PENDING, LEG_INTENT, LEG_SUBMITTED, LEG_SETTLED, LEG_DEAD,
    TRADE_ACTIVE, TRADE_COMPLETED, TRADE_STALLED,
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
        # Deliberately huge by default too, for the same reason: most
        # tests do not care about the chain-anchored deadline and should
        # read as "long past any of them" the way a big wall-clock age
        # already implied before deadline_height existed. A test that
        # cares sets this explicitly (see TestBlame).
        self.current_height = 10**9

    def height(self):
        return self.current_height

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


def make_trade(i_send="lapse", count=3, confirm_depth=2, role="taker",
              accepted_height=1000, my_lapse_addr="me.lapse",
              peer_lapse_addr="peer.lapse"):
    now = time.time()
    session_id = swap.new_session_id("order-1", "taker.addr")
    trade = Trade.create(
        session_id=session_id, order_id="order-1", role=role,
        my_lapse_addr=my_lapse_addr, my_xlm_addr="GME",
        peer_lapse_addr=peer_lapse_addr, peer_xlm_addr="GPEER",
        i_send=i_send, lapse_total=3 * LAPSE, xlm_total=3 * XLM,
        increment_count=count, confirm_depth=confirm_depth,
        accepted_height=accepted_height,
        status=TRADE_ACTIVE, created_at=now, updated_at=now)
    schedule = swap.build_schedule(3 * LAPSE, 3 * XLM, count)
    for n, (lapse_amt, xlm_amt) in enumerate(schedule, start=1):
        Increment.create(
            id=f"{session_id}:{n}", session_id=session_id, n=n,
            lapse_amount=lapse_amt, xlm_amount=xlm_amt,
            i_move_first=swap.i_move_first(n, True),
            created_at=now, deadline_at=now + 3600,
            deadline_height=(swap_engine.deadline_height(accepted_height, confirm_depth)
                             if n == 1 else 0))
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
        self.height = chain[-1]["height"] if chain else 0


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
        # Trust reads this straight off Trade.status now (trust.local_tally):
        # a completed trade's own row is the entire credit.
        assert Trade.get(Trade.session_id == trade.session_id).status == TRADE_COMPLETED
        assert trust.local_tally("peer.lapse")["completed_count"] == 1


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

    def test_a_reorg_on_a_completed_step_resets_the_next_steps_deadline(self):
        """A completed step's completed_height is what the NEXT step's
        deadline_height gets anchored to (see _propagate_deadline). If a
        reorg later takes that step's leg back, leaving the stale
        completed_height in place would understate the next step's real
        grace period once it re-settles: a step can only re-settle at a
        LATER height than before (time only runs one way), never an
        earlier one, so an unrefreshed deadline built from the old,
        too-early height would make the counterparty look overdue on
        the next step sooner than is actually fair."""
        engine, lapse, _xlm = make_engine()
        trade = make_trade()
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)                # we send step 1
        settle_peer_leg(trade, first, engine)
        engine.advance(trade)                # both legs settle; step 2's deadline is set

        first = Increment.get(Increment.id == first.id)
        second = Increment.get(Increment.id == f"{trade.session_id}:2")
        assert first.completed_height
        assert second.deadline_height == swap_engine.deadline_height(
            first.completed_height, trade.confirm_depth)

        lapse.payments.clear()               # the reorg takes step 1's leg back
        engine.check_outbound(trade, first)

        first = Increment.get(Increment.id == first.id)
        second = Increment.get(Increment.id == second.id)
        assert first.completed_height == 0
        assert second.deadline_height == 0


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
        engine, lapse, _x = make_engine()
        trade = make_trade(accepted_height=1000, confirm_depth=2)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        # Right at the deadline: the fake chain's own default height
        # (10**9) is used elsewhere to mean "so many blocks have passed
        # that any confirmation trivially clears", which would make this
        # trade look absurdly overdue rather than freshly stalled; pin
        # it to the real deadline so the test means what it says.
        lapse.current_height = inc.deadline_height
        Increment.update(deadline_at=time.time() - 1).execute()
        engine.advance(trade)
        trade = Trade.get(Trade.session_id == trade.session_id)
        assert trade.status == TRADE_STALLED
        # Freshly stalled, nowhere near ABANDON_AFTER_BLOCKS past its
        # deadline yet, so it must not already count against the peer.
        assert trust.local_tally("peer.lapse", lapse.current_height)["abandoned_count"] == 0

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

    def _stall(self, trade):
        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.save()
        return trade

    def test_blame_fires_on_first_step_defection_with_no_prior_reciprocation(self):
        """The gap this closes: a Trade row here can only exist through a
        verified, signed handshake (a FillRequest a real maker chose to
        answer, or a FillResponse from the order's actual maker), so the
        old worry, an attacker inventing a session out of thin air and
        blaming the victim for not answering it, is already structurally
        closed before this method is ever reached. Requiring the peer to
        have reciprocated at least once besides used to mean a peer who
        defects on the very first step they owe, the common case, could
        never be blamed at all. Now the chain-anchored deadline alone
        decides it."""
        engine, lapse, _x = make_engine()
        trade = make_trade()
        engine.advance(trade)            # we paid; they never reciprocated
        current_height = lapse.current_height + swap_engine.ABANDON_AFTER_BLOCKS + 1
        assert swap_engine.is_delinquent(self._stall(trade), current_height) is True
        assert trust.local_tally("peer.lapse", current_height)["abandoned_count"] == 1

    def test_no_blame_before_the_chain_height_margin_elapses(self):
        """The verdict is chain height against deadline_height +
        ABANDON_AFTER_BLOCKS. A trade whose chain has not moved far
        enough past its deadline must not be blamed yet."""
        engine, lapse, _x = make_engine()
        trade = make_trade(accepted_height=1000, confirm_depth=2)
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        lapse.current_height = inc.deadline_height  # right at the deadline, no margin yet
        engine.advance(trade)            # we paid; they never reciprocated
        assert swap_engine.is_delinquent(self._stall(trade), lapse.current_height) is False
        assert trust.local_tally("peer.lapse", lapse.current_height)["abandoned_count"] == 0

    def test_no_blame_when_this_node_owes_the_move(self):
        engine, lapse, _x = make_engine()
        trade = make_trade()
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)
        settle_peer_leg(trade, first, engine)
        engine.advance(trade)            # step 2 is ours and unsent
        far_height = lapse.current_height + 10**6
        assert swap_engine.is_delinquent(self._stall(trade), far_height) is False

    def test_blame_once_they_accepted_then_stopped(self):
        """Their own settled leg is the acceptance: a signed transaction
        carrying this session's tag, which only they could produce."""
        engine, lapse, _x = make_engine()
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

        # Steps 1 and 2 completing pushed step 3's deadline_height forward
        # from whatever height the chain was at when each did (see
        # _propagate_deadline); simulate the chain having actually moved
        # well past it since.
        lapse.current_height += 10**6
        assert swap_engine.is_delinquent(self._stall(trade), lapse.current_height) is True
        assert trust.local_tally("peer.lapse", lapse.current_height)["abandoned_count"] == 1

    def test_acceptance_cannot_be_forged_by_the_accuser(self):
        """Only an inbound leg counts. Our own payments, however many,
        never amount to the counterparty having agreed."""
        engine, _l, _x = make_engine()
        trade = make_trade()
        for _ in range(5):
            engine.advance(trade)
        assert engine._peer_ever_reciprocated(trade) is False


class TestRedemption:
    """A counterparty who was genuinely offline, not dishonest, must be
    able to still complete the trade late and have that be the whole
    fix: is_delinquent is computed fresh from Trade/Increment rows every
    time, so nothing was ever written down that needs to be written
    back once the missing leg settles."""

    def _make_delinquent(self, engine, trade):
        engine.advance(trade)          # we pay; they never reciprocate
        engine.lapse.current_height += 10**6
        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.save()
        assert swap_engine.is_delinquent(trade, engine.lapse.current_height) is True
        return trade

    def test_a_late_settlement_stops_counting_immediately(self):
        engine, lapse, _x = make_engine()
        trade = make_trade()
        trade = self._make_delinquent(engine, trade)
        assert trust.local_tally("peer.lapse", lapse.current_height)["abandoned_count"] == 1

        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        settle_peer_leg(trade, first, engine)   # the missing leg finally lands
        engine.advance(trade)                   # the ordinary loop notices it

        trade = Trade.get(Trade.session_id == trade.session_id)
        assert trade.status == TRADE_ACTIVE
        assert swap_engine.is_delinquent(trade, lapse.current_height) is False
        assert trust.local_tally("peer.lapse", lapse.current_height)["abandoned_count"] == 0

    def test_still_missing_leg_keeps_counting(self):
        engine, lapse, _x = make_engine()
        trade = make_trade()
        trade = self._make_delinquent(engine, trade)
        engine.advance(trade)   # nothing new to find
        trade = Trade.get(Trade.session_id == trade.session_id)
        assert swap_engine.is_delinquent(trade, lapse.current_height) is True

    def test_completed_trade_is_never_delinquent(self):
        engine, lapse, _x = make_engine()
        trade = make_trade()
        for _ in range(20):
            inc = engine.advance(trade)
            if inc is None:
                break
            settle_peer_leg(trade, inc, engine)
        trade = Trade.get(Trade.session_id == trade.session_id)
        assert trade.status == TRADE_COMPLETED
        assert swap_engine.is_delinquent(trade, lapse.current_height + 10**6) is False

    def test_redemption_lets_the_trade_finish_on_its_own(self):
        """Nothing but the ordinary advance loop is needed to drive the
        rest, including steps that never got a chance to run while the
        trade looked delinquent."""
        engine, lapse, _x = make_engine()
        trade = make_trade(count=2)
        trade = self._make_delinquent(engine, trade)
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        settle_peer_leg(trade, first, engine)

        trade = Trade.get(Trade.session_id == trade.session_id)
        for _ in range(6):
            inc = engine.advance(trade)
            if inc is None:
                break
            settle_peer_leg(trade, inc, engine)
        assert Trade.get(Trade.session_id == trade.session_id).status == TRADE_COMPLETED


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
        self.publish_fill_request_calls = []
        self.publish_receipt_calls = []

    def publish_fill_response(self, resp):
        self.publish_fill_response_calls.append(resp)

    def publish_fill_request(self, req):
        self.publish_fill_request_calls.append(req)

    def kek(self):
        return crypto.derive_kek(self.keyfile, self.passphrase)

    def publish_receipt(self, receipt):
        self.publish_receipt_calls.append(receipt)


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
              min_fill=0, max_fill=0, expiry_block=10**9, margin=0):
    return Order.create(
        order_id=order_id, maker_lapse_addr=maker_lapse, maker_xlm_addr=maker_xlm,
        direction=direction, lapse_total=lapse_total,
        price_stroops_per_lapse=price, min_fill=min_fill,
        max_fill=max_fill or lapse_total, expiry_block=expiry_block,
        auto_match_margin_stroops=margin,
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
        tie-broke its own opening_mover call differently from how the
        taker's own _open_taker_trade would, both sides would believe
        they open (or neither would) and step 1 would deadlock.
        answer_fill_requests must derive the maker's decision as the
        exact negation of what the taker independently computes, never
        in parallel.

        No trade history for either address and no chain history: every
        trust score here is 0, which is the exact tie
        swap.opening_mover's score comparison passes through to its
        alphabetical fallback (see that function's own docstring for
        why a tie is no longer broken by a fixed 'taker always opens'
        role - that was the exploitable lever a bait-order attacker
        used, see market.py's notes). The alphabetical answer itself is
        not what this test cares about (it depends on two addresses,
        one of them a freshly generated real FALCON one, so asserting a
        specific direction would be flaky); what has to hold regardless
        is that both sides land on complementary answers.
        """
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(engine, node, "GMAKER", 5 * XLM, 2) == 1
        first = Increment.get(Increment.id == f"{req.session_id}:1")

        # What the taker's own node independently derives for itself,
        # computed the exact same way _open_taker_trade does.
        taker_i_open = swap.opening_mover(0.0, 0.0, req.taker_lapse_addr, node.addr)
        assert first.i_move_first is not taker_i_open


class TestAnswerFillRequestsIsSerializedAcrossThreads:
    """_answer_one runs from two different threads that share nothing but
    this process's own database: the swap worker's own periodic pass
    (swap_worker.run_once) and a person clicking Accept on the Market
    page, which Werkzeug hands a brand new thread per request (see
    api._close_db_after_request's docstring). Both read this node's own
    balance and _pending_send_total, then commit a Trade, without a lock
    that read-then-commit is not atomic: two accepts racing each other
    against two different orders of the same maker can each see the
    exposure as it stood before the other committed, both pass the
    funding check, and jointly promise more LAPSE than this node holds.
    _ACCEPT_LOCK exists to make that impossible; this proves it.

    Needs a real, file-backed database rather than the usual ":memory:"
    fixture: peewee opens a separate connection per thread
    (thread_safe=True), and two connections to ":memory:" are two
    unrelated, empty databases. A file is what every real node actually
    uses, so this is also the more faithful setup for a threading test.
    """

    def test_two_orders_racing_the_same_balance_never_both_commit(
            self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "race.db")
        storage_mod.db.init(db_path)
        storage_mod.db.connect(reuse_if_open=True)
        storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
        trade_storage._initialised = True
        try:
            node = FakeDiscoveryNode(tmp_path, balances={})
            order_a = make_order(order_id="order-a", direction="sell",
                                 lapse_total=10 * LAPSE, maker_lapse=node.addr)
            order_b = make_order(order_id="order-b", direction="sell",
                                 lapse_total=10 * LAPSE, maker_lapse=node.addr)
            req_a = make_request(order_id="order-a", session_id="a" * 16,
                                 taker_lapse="takerA", taker_xlm="GA",
                                 lapse_total=5 * LAPSE)
            req_b = make_request(order_id="order-b", session_id="b" * 16,
                                 taker_lapse="takerB", taker_xlm="GB",
                                 lapse_total=5 * LAPSE)
            engine, _lapse, _xlm = make_maker_engine(node)
            # Enough for exactly one of the two 5-LAPSE fills, not both:
            # the scenario a missing lock would let through.
            engine.lapse.balances[node.addr] = 6 * LAPSE

            # Widen the funding check into a window an unlocked version
            # would race inside, and record when each thread was in it.
            real_pending_send_total = swap_engine._pending_send_total
            timeline = []
            timeline_lock = threading.Lock()

            def instrumented(asset):
                start = time.monotonic()
                result = real_pending_send_total(asset)
                time.sleep(0.1)
                with timeline_lock:
                    timeline.append(
                        (threading.current_thread().name, start, time.monotonic()))
                return result

            monkeypatch.setattr(swap_engine, "_pending_send_total", instrumented)

            barrier = threading.Barrier(2)
            results = {}

            def run(name, order_row, req):
                barrier.wait(timeout=5)
                results[name] = swap_engine._answer_one(
                    engine, node, "GMAKER", 5 * XLM, 2, order_row, req)

            t1 = threading.Thread(target=run, args=("worker", order_a, req_a),
                                  name="worker")
            t2 = threading.Thread(target=run, args=("flask", order_b, req_b),
                                  name="flask")
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert len(timeline) == 2, "both threads must have reached the funding check"
            (_n1, s1, e1), (_n2, s2, e2) = sorted(timeline, key=lambda row: row[1])
            assert e1 <= s2, (
                "the two accept decisions overlapped in the funding-check "
                "window; _ACCEPT_LOCK did not serialize them")

            # With the balance and requests as sized above, exactly one of
            # the two must have been accepted and the other refused for
            # insufficient funds; not both, and not neither.
            assert sorted(results.values()) == [False, True]
            trades = list(Trade.select())
            assert len(trades) == 1
            committed = sum(t.lapse_total for t in trades)
            assert committed <= engine.lapse.balances[node.addr]
        finally:
            storage_mod.db.close()
            storage_mod.db.init(":memory:")
            storage_mod.db.connect(reuse_if_open=True)
            storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
            storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
            trade_storage._initialised = True


class TestAutoAcceptTrustFloor:
    """settings.SWAP_AUTO_ACCEPT_MIN_TRUST: a per-request trust gate on
    top of the (always-enforced) exposure cap. A request that clears the
    cap but not this floor is left pending, not declined, for
    decide_fill_request to answer by hand."""

    def test_a_stranger_below_the_floor_is_left_pending(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        # No trade history and no chain history for the taker, so its
        # trust score is 0; any positive floor excludes it.
        assert swap_engine.answer_fill_requests(
            engine, node, "GMAKER", 5 * XLM, 2, min_trust=0.1) == 0
        assert Trade.select().count() == 0
        assert node.publish_fill_response_calls == []

    def test_zero_floor_keeps_accepting_everyone(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(
            engine, node, "GMAKER", 5 * XLM, 2, min_trust=0.0) == 1
        assert Trade.select().count() == 1

    def test_a_request_left_pending_by_the_floor_can_still_be_accepted_by_hand(
            self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(
            engine, node, "GMAKER", 5 * XLM, 2, min_trust=0.1) == 0

        # decide_fill_request never takes min_trust: a person clicking
        # Accept has already made the call the floor exists to require.
        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, req.request_id, accept=True) is True
        assert Trade.select().count() == 1

    def test_the_exposure_cap_still_declines_outright_regardless_of_the_floor(
            self, tmp_path):
        """A request that cannot fit even manually is not something a
        person can rescue by clicking Accept, so it is still auto-
        declined rather than left pending."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(lapse_total=1000 * LAPSE, price=1000, maker_lapse=node.addr)
        req = make_request(lapse_total=1000 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 1000 * LAPSE

        tiny_cap = 1000
        assert swap_engine.answer_fill_requests(
            engine, node, "GMAKER", tiny_cap, 2, min_trust=0.1) == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False


class TestManualFillDecisions:
    """min_trust=inf: answer_fill_requests leaves every live request
    pending instead of deciding it (no counterparty ever clears an
    infinite floor), and decide_fill_request is the one-at-a-time
    counterpart a person clicks from the Market page."""

    def test_an_infinite_floor_answers_nothing(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.answer_fill_requests(
            engine, node, "GMAKER", 5 * XLM, 2, min_trust=float("inf")) == 0
        assert Trade.select().count() == 0
        assert node.publish_fill_response_calls == []

    def test_decide_fill_request_accepts_exactly_like_auto_would(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, req.request_id, accept=True) is True
        trade = Trade.get(Trade.session_id == req.session_id)
        assert trade.role == "maker"
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is True

    def test_decide_fill_request_still_enforces_the_exposure_cap(self, tmp_path):
        """A manual accept is not a weaker check than an automatic one:
        the same trust/cap decision runs either way."""
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(lapse_total=1000 * LAPSE, price=1000, maker_lapse=node.addr)
        req = make_request(lapse_total=1000 * LAPSE)
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = 1000 * LAPSE

        tiny_cap = 1000
        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", tiny_cap, 2, req.request_id, accept=True) is False
        assert Trade.select().count() == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False

    def test_decide_fill_request_can_decline_without_touching_trust(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        # Deliberately no balance given: a decline must never need to
        # fund anything, unlike an accept.

        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, req.request_id, accept=False) is True
        assert Trade.select().count() == 0
        resp = node.publish_fill_response_calls[0]
        assert resp["accepted"] is False
        assert resp["reason"] == "declined by the maker"

    def test_an_already_answered_request_cannot_be_decided_again(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        make_order(direction="sell", maker_lapse=node.addr)
        req = make_request()
        engine, _lapse, _xlm = make_maker_engine(node)
        engine.lapse.balances[node.addr] = req.lapse_total

        swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, req.request_id, accept=True)
        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, req.request_id, accept=False) is False
        assert Trade.select().count() == 1

    def test_an_unknown_request_id_is_reported_as_not_handled(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        engine, _lapse, _xlm = make_maker_engine(node)
        assert swap_engine.decide_fill_request(
            engine, node, "GMAKER", 5 * XLM, 2, "no-such-request", accept=False) is False


class TestAutoMatchOrders:
    """Proactively taking a compatible order already resting in the
    book, on this node's own initiative, for one of this node's own
    live orders: no person on either side has to notice the other and
    click Buy or Sell for a pair of orders that already agree on price
    to actually trade."""

    def test_a_crossing_buy_order_is_auto_matched(self, tmp_path):
        """This node's own sell at 1000 and somebody else's resting buy
        at 1050 already agree (the buyer offered more than the seller
        asked), so this node sends a fill request against it on its
        own, without a human or an incoming request from the other
        side."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=1050,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 1
        assert len(node.publish_fill_request_calls) == 1
        req = node.publish_fill_request_calls[0]
        assert req["order_id"] == "theirs"
        assert req["taker_lapse_addr"] == node.addr
        assert market_mod.get_fill_request(req["request_id"]) is not None

    def test_a_non_crossing_order_is_left_alone(self, tmp_path):
        """A resting buy at 950 has not offered enough to meet a sell
        asking 1000; nobody here is bridging that gap, so nothing is
        sent."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=950,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0
        assert node.publish_fill_request_calls == []

    def test_a_buy_order_matching_a_cheaper_sell_pays_lapse_or_xlm_correctly(
            self, tmp_path):
        """This node's own buy at 1050 crosses somebody else's resting
        sell at 1000 (this node offered more than they asked); this
        node pays LAPSE to fulfil their sell exactly as a manual taker
        would (order_row.direction == 'sell' -> the maker gives LAPSE
        -> here 'the maker' of the resting order is the other side, so
        this node, the taker, pays XLM back for it)."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="buy", price=1050,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="sell", price=1000,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 1
        req = node.publish_fill_request_calls[0]
        assert req["order_id"] == "theirs"

    def test_an_already_requested_counter_order_is_not_asked_again(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=1050,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 1
        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0
        assert len(node.publish_fill_request_calls) == 1

    def test_insufficient_funding_skips_the_match(self, tmp_path):
        """This node cannot actually fund the LAPSE leg a matching buy
        order would need, so nothing is sent even though the prices
        agree; the same funding check a manual taker's request would
        be held to."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=1050,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, lapse, _xlm = make_maker_engine(node)
        lapse.balances[node.addr] = 0

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0
        assert node.publish_fill_request_calls == []

    def test_a_locked_wallet_sends_nothing(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=1050,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine = swap_engine.Engine(FakeChain("lapse", node.addr),
                                    FakeChain("xlm", "GMINE"),
                                    lambda: (None, None))

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0
        assert node.publish_fill_request_calls == []

    def test_no_own_orders_is_a_no_op(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        engine, _lapse, _xlm = make_maker_engine(node)
        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0

    def test_a_near_miss_within_the_margin_is_matched(self, tmp_path):
        """A resting buy at 950 does not cross a sell asking 1000
        outright, but this node's own private margin of 50 says it will
        still go looking for exactly this: privately good enough, even
        though nothing publicly posted says so."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE", margin=50)
        make_order(order_id="theirs", direction="buy", price=950,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 1
        req = node.publish_fill_request_calls[0]
        assert req["order_id"] == "theirs"

    def test_a_near_miss_outside_the_margin_is_left_alone(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE", margin=20)
        make_order(order_id="theirs", direction="buy", price=950,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        assert swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM) == 0
        assert node.publish_fill_request_calls == []

    def test_a_sent_request_never_reveals_the_margin(self, tmp_path):
        """The whole point: whatever this node privately would have
        accepted, the request it actually sends is indistinguishable
        from a request sent with no margin set at all."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  maker_lapse=node.addr, maker_xlm="GMINE", margin=50)
        make_order(order_id="theirs", direction="buy", price=950,
                  maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        swap_engine.auto_match_orders(engine, node, "GMINE", 5 * XLM)
        req = node.publish_fill_request_calls[0]
        assert set(req) - {"signature"} == set(market_mod.FILL_REQUEST_SIGNED_FIELDS)

    def test_the_exposure_cap_still_bounds_the_sent_amount(self, tmp_path):
        """A stranger counterparty's exposure cap is small; the amount
        this node asks for must fit inside it rather than the whole of
        either order."""
        node = FakeDiscoveryNode(tmp_path)
        make_order(order_id="mine", direction="sell", price=1000,
                  lapse_total=1000 * LAPSE, maker_lapse=node.addr, maker_xlm="GMINE")
        make_order(order_id="theirs", direction="buy", price=1050,
                  lapse_total=1000 * LAPSE, maker_lapse="other.maker", maker_xlm="GOTHER")
        engine, _lapse, _xlm = make_maker_engine(node)

        tiny_cap = 1000
        assert swap_engine.auto_match_orders(engine, node, "GMINE", tiny_cap) == 1
        req = node.publish_fill_request_calls[0]
        assert req["lapse_total"] < 1000 * LAPSE


# ---------------------------------------------------------------------------
# Step receipts: emission and chain verification
# ---------------------------------------------------------------------------

import market as market_mod2  # noqa: E402  (mirrors the market_mod import above)


class TestEmitReceipts:
    def test_settled_step_emits_a_receipt_for_each_leg(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        lapse = FakeChain("lapse", node.addr)
        xlm = FakeChain("xlm", "GME")
        trade = make_trade(i_send="lapse", count=2, my_lapse_addr=node.addr)
        kek = node.kek()
        engine = swap_engine.Engine(lapse, xlm, lambda: (kek, "seed"))

        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine.advance(trade)             # we send our lapse leg
        xlm.deliver(*engine._in_terms(trade, inc))   # they send theirs
        engine.advance(trade)             # notice it settled

        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == LEG_SETTLED and inc.in_state == LEG_SETTLED

        emitted = swap_engine.emit_receipts_for_trade(engine, node, kek, trade)
        assert emitted == 2
        receipts = market_mod2.receipts_for_addr(node.addr)
        assert {r.asset for r in receipts} == {"lapse", "xlm"}
        assert all(r.outcome == "settled" for r in receipts)
        assert all(r.tx_hash for r in receipts)
        assert len(node.publish_receipt_calls) == 2

        # Idempotent: nothing new the second time.
        again = swap_engine.emit_receipts_for_trade(engine, node, kek, trade)
        assert again == 0
        assert len(market_mod2.receipts_for_addr(node.addr)) == 2

    def test_abandoned_step_emits_one_missed_receipt(self, tmp_path):
        node = FakeDiscoveryNode(tmp_path, balances={})
        lapse = FakeChain("lapse", node.addr)
        xlm = FakeChain("xlm", "GME")
        trade = make_trade(i_send="lapse", count=3, my_lapse_addr=node.addr)
        kek = node.kek()
        engine = swap_engine.Engine(lapse, xlm, lambda: (kek, "seed"))

        engine.advance(trade)   # we pay step 1; they never reciprocate
        lapse.current_height += 10**6
        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.save()
        assert swap_engine.is_delinquent(trade, lapse.current_height) is True

        emitted = swap_engine.emit_receipts_for_trade(engine, node, kek, trade)
        assert emitted == 1
        receipts = [r for r in market_mod2.receipts_for_addr(node.addr)
                   if r.outcome == "missed"]
        assert len(receipts) == 1
        assert receipts[0].tx_hash == ""


class TestVerifyReceiptAgainstChain:
    def _engine(self):
        lapse = FakeChain("lapse", "a.lapse")
        xlm = FakeChain("xlm", "GA")
        return swap_engine.Engine(lapse, xlm, lambda: (None, None)), lapse, xlm

    def test_true_settled_claim_verifies(self):
        engine, lapse, _xlm = self._engine()
        lapse.deliver("a.lapse", "b.lapse", "memo1", 500)
        tx_hash, _depth = lapse.find_payment("a.lapse", "b.lapse", "memo1", 500)
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="settled", tx_hash=tx_hash)
        assert swap_engine.verify_receipt_against_chain(engine, receipt) is True

    def test_false_settled_claim_with_no_matching_payment(self):
        engine, _lapse, _xlm = self._engine()
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="settled", tx_hash="nope")
        assert swap_engine.verify_receipt_against_chain(engine, receipt) is False

    def test_true_missed_claim_with_no_payment_and_deadline_passed(self):
        engine, lapse, _xlm = self._engine()
        lapse.current_height = 2000
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="missed", tx_hash="",
            deadline_height=1000)
        assert swap_engine.verify_receipt_against_chain(engine, receipt) is True

    def test_false_missed_claim_when_the_payment_actually_exists(self):
        engine, lapse, _xlm = self._engine()
        lapse.deliver("a.lapse", "b.lapse", "memo1", 500)
        lapse.current_height = 2000
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="missed", tx_hash="",
            deadline_height=1000)
        assert swap_engine.verify_receipt_against_chain(engine, receipt) is False

    def test_not_yet_due_raises_rather_than_returning_false(self):
        """Not enough height has passed on THIS node's own clock yet.
        This is a different claim from 'the payment was found' (a real,
        permanent False) and must not be reported the same way, or a
        genuinely true claim checked too early (a syncing node, or one
        that simply checks sooner than the reporter's own margin
        implies) would be buried forever - see
        trust._verify_addr_receipts, which never rechecks a False."""
        engine, lapse, _xlm = self._engine()
        lapse.current_height = 1010   # short of deadline_height + ABANDON_AFTER_BLOCKS
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="missed", tx_hash="",
            deadline_height=1000)
        with pytest.raises(swap_engine.NotYetDue):
            swap_engine.verify_receipt_against_chain(engine, receipt)

    def test_unreachable_propagates_rather_than_guessing(self):
        engine, lapse, _xlm = self._engine()
        lapse.unreachable = True
        receipt = _FakeReceiptRow(
            asset="lapse", from_addr="a.lapse", to_addr="b.lapse",
            amount=500, memo="memo1", outcome="settled", tx_hash="tx1")
        with pytest.raises(swap_engine.Unreachable):
            swap_engine.verify_receipt_against_chain(engine, receipt)


class _FakeReceiptRow:
    """Just the attributes verify_receipt_against_chain reads off a
    StepReceipt row, without needing the database round trip."""

    def __init__(self, asset, from_addr, to_addr, amount, memo, outcome,
                tx_hash, deadline_height=0):
        self.asset = asset
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.amount = amount
        self.memo = memo
        self.outcome = outcome
        self.tx_hash = tx_hash
        self.deadline_height = deadline_height
