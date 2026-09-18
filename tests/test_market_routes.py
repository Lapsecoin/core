"""The Trades page's worker-status view, and _start_trade.

A stalled trade must not look the same whether the wallet is locked,
Horizon is unreachable, or the counterparty genuinely vanished, so this
checks that market_routes._worker_view tells those apart correctly from
whatever swap_worker.status() reports.

_start_trade is the taker's half of maker-side discovery (plan item
1.1): it has to open a passphrase-sealed wallet, plan a schedule, force
step 1 onto this side regardless of trust, and publish a claim the
maker can later use, all without a running Flask app around it.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import crypto
import market as market_mod
import market_routes
import storage as storage_mod
import swap as swap_mod
import trade_storage
import trust as trust_mod
import xlm as xlm_mod
from trade_storage import Increment, Order, Trade


class FakeWorker:
    def __init__(self, **overrides):
        self._status = {
            "running": True, "enabled": True, "unlocked": True,
            "passes": 3, "paused_until": 0.0, "last_error": "",
        }
        self._status.update(overrides)

    def status(self):
        return self._status


class FakeNode:
    def __init__(self, worker=None):
        if worker is not None:
            self.swap_worker = worker


class TestWorkerView:
    def test_no_worker_attribute_reads_as_off(self):
        assert market_routes._worker_view(FakeNode())["state"] == "off"

    def test_disabled_reads_as_off(self):
        node = FakeNode(FakeWorker(enabled=False))
        assert market_routes._worker_view(node)["state"] == "off"

    def test_not_running_is_reported(self):
        node = FakeNode(FakeWorker(running=False))
        view = market_routes._worker_view(node)
        assert view["state"] == "stopped"
        assert "not running" in view["message"]

    def test_locked_wallet_is_reported(self):
        node = FakeNode(FakeWorker(unlocked=False))
        view = market_routes._worker_view(node)
        assert view["state"] == "locked"
        assert "locked" in view["message"]

    def test_paused_after_outage_is_reported(self):
        node = FakeNode(FakeWorker(paused_until=time.time() + 60,
                                   last_error="horizon unreachable: timeout"))
        view = market_routes._worker_view(node)
        assert view["state"] == "paused"
        assert "horizon unreachable" in view["message"]

    def test_expired_pause_reads_as_ok(self):
        node = FakeNode(FakeWorker(paused_until=time.time() - 60))
        assert market_routes._worker_view(node)["state"] == "ok"

    def test_running_normally(self):
        node = FakeNode(FakeWorker())
        view = market_routes._worker_view(node)
        assert view["state"] == "ok"
        assert view["passes"] == 3

    def test_locked_takes_priority_over_a_stale_pause(self):
        """Unlocking is the fix a user can act on; showing 'paused' instead
        would send them to wait on Horizon rather than unlock the node."""
        node = FakeNode(FakeWorker(unlocked=False,
                                   paused_until=time.time() + 60))
        assert market_routes._worker_view(node)["state"] == "locked"


LAPSE = 100_000_000
XLM = 10_000_000


@pytest.fixture(autouse=True)
def fresh_db():
    storage_mod.db.init(":memory:")
    storage_mod.db.connect(reuse_if_open=True)
    storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
    storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
    trade_storage._initialised = True
    yield
    storage_mod.db.close()


class _View:
    def __init__(self, height, balances=None):
        self.height = height
        self.chain = [{"height": height}]
        self.state = _State(balances)


class _State:
    def __init__(self, balances=None):
        self.balances = balances or {}

    def get_balance(self, addr):
        return self.balances.get(addr, 0)


class _Storage:
    def __init__(self, heights_by_addr=None):
        self.heights_by_addr = heights_by_addr or {}

    def get_tx_heights_for_addr(self, addr):
        return self.heights_by_addr.get(addr, [])


class TakerNode:
    """A real FALCON identity and a real (kek-sealed) XLM wallet, as a
    node taking an order actually has, plus just enough of Node's public
    surface for _start_trade and the trust lookups inside it."""

    def __init__(self, tmp_path, height=1000, balances=None,
                heights_by_addr=None, name="taker"):
        sk, pk = crypto.generate_keypair()
        self.keyfile = str(tmp_path / f"{name}.key")
        self.passphrase = "correct horse"
        crypto.save_key(self.keyfile, sk, pk, self.passphrase)
        self.addr = crypto.public_key_to_address(pk)
        self.pk_hex = pk.hex()

        kek = crypto.derive_kek(self.keyfile, self.passphrase)
        seed, xlm_pub = xlm_mod.generate_keypair()
        self.xlm_keyfile = str(tmp_path / f"{name}_xlm.key")
        xlm_mod.save_key(self.xlm_keyfile, seed, xlm_pub, kek=kek)
        self.xlm_addr = xlm_pub

        self.view = _View(height, balances)
        self.storage = _Storage(heights_by_addr)
        self.publish_claim_calls = []

    def publish_claim(self, claim):
        self.publish_claim_calls.append(claim)


class FakeForm(dict):
    def get(self, key, default=None):
        return super().get(key, default if default is not None else "")


class FakeRequest:
    def __init__(self, form):
        self.form = FakeForm(form)


def make_maker_order(order_id="order-1", direction="sell", lapse_total=10 * LAPSE,
                     price=1000, maker_lapse="maker.lapse", maker_xlm="GMAKER",
                     min_fill=0, max_fill=0, expiry_block=10**9):
    return Order.create(
        order_id=order_id, maker_lapse_addr=maker_lapse, maker_xlm_addr=maker_xlm,
        direction=direction, lapse_total=lapse_total,
        price_stroops_per_lapse=price, min_fill=min_fill,
        max_fill=max_fill or lapse_total, expiry_block=expiry_block,
        pubkey="ab" * 10, signature="cd" * 10,
        created_at=time.time(), received_at=time.time(), verified=True)


class TestStartTrade:
    def _call(self, node, order_row, form, depth=2, cap=5 * XLM):
        fake_request = FakeRequest({"passphrase": node.passphrase,
                                    "amount_lapse": "1", **form})
        original = market_routes.request
        market_routes.request = fake_request
        try:
            return market_routes._start_trade(
                node, order_row, node.view.height, node.xlm_keyfile, depth, cap)
        finally:
            market_routes.request = original

    def test_creates_a_trade_and_matching_schedule(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order(direction="sell", price=1000)
        session_id = self._call(node, order, {"amount_lapse": "1"})

        trade = Trade.get(Trade.session_id == session_id)
        assert trade.role == "taker"
        assert trade.peer_lapse_addr == "maker.lapse"
        assert trade.peer_xlm_addr == "GMAKER"
        assert trade.i_send == "xlm"   # maker sells LAPSE, taker pays XLM
        assert trade.lapse_total == 1 * LAPSE
        steps = list(Increment.select()
                    .where(Increment.session_id == session_id)
                    .order_by(Increment.n))
        assert len(steps) == trade.increment_count
        assert sum(s.lapse_amount for s in steps) == 1 * LAPSE

    def test_step_one_is_always_this_sides_move(self, tmp_path):
        """Forced regardless of trust, since nothing else could ever tell
        the maker this session exists (see swap_engine.discover_trades'
        module docstring)."""
        node = TakerNode(tmp_path, heights_by_addr={
            "maker.lapse": [(0, "h")]}, balances={"maker.lapse": 10**12})
        # Make the maker look extremely well-established relative to this
        # taker, which under a pure trust rule would have the maker open.
        order = make_maker_order()
        session_id = self._call(node, order, {})
        first = Increment.get(Increment.id == f"{session_id}:1")
        assert first.i_move_first is True

    def test_publishes_a_claim_matching_the_trade(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order(price=1000)
        session_id = self._call(node, order, {"amount_lapse": "2"})

        assert len(node.publish_claim_calls) == 1
        claim = node.publish_claim_calls[0]
        assert claim["session_id"] == session_id
        assert claim["order_id"] == order.order_id
        assert claim["taker_lapse_addr"] == node.addr
        assert claim["taker_xlm_addr"] == node.xlm_addr
        assert claim["lapse_total"] == 2 * LAPSE
        assert market_mod.verify_claim(claim) is True
        assert market_mod.get_claim(session_id) is not None

    def test_claims_schedule_matches_the_trades_own(self, tmp_path):
        """The maker rebuilds the exact same schedule from the claim (see
        swap_engine.discover_trades); the claim's increment_count has to
        be the one actually used, not a stale or re-derived guess."""
        node = TakerNode(tmp_path)
        order = make_maker_order(price=1000)
        session_id = self._call(node, order, {"amount_lapse": "5"})
        trade = Trade.get(Trade.session_id == session_id)
        claim = node.publish_claim_calls[0]
        assert claim["increment_count"] == trade.increment_count

    def test_fill_larger_than_the_order_is_refused(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order(lapse_total=1 * LAPSE)
        with pytest.raises(market_mod.OrderRejected, match="left"):
            self._call(node, order, {"amount_lapse": "5"})
        assert Trade.select().count() == 0

    def test_wrong_passphrase_is_refused_before_anything_is_created(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order()
        fake_request = FakeRequest({"passphrase": "not it", "amount_lapse": "1"})
        original = market_routes.request
        market_routes.request = fake_request
        try:
            with pytest.raises(ValueError, match="passphrase"):
                market_routes._start_trade(
                    node, order, node.view.height, node.xlm_keyfile, 2, 5 * XLM)
        finally:
            market_routes.request = original
        assert Trade.select().count() == 0
        assert node.publish_claim_calls == []

    def test_claim_cap_hit_creates_no_orphaned_trade(self, tmp_path):
        """The claim is built, signed and stored before the trade row: if
        storing it fails (the per-taker cap), nothing must be created,
        or this node would carry a trade the maker can never discover
        for want of the address only a claim supplies."""
        node = TakerNode(tmp_path)
        for i in range(market_mod.MAX_CLAIMS_PER_TAKER):
            filler = market_mod.build_claim(
                order_id="other-order", session_id=f"filler{i}" * 4,
                taker_lapse_addr=node.addr, taker_xlm_addr=node.xlm_addr,
                lapse_total=1, increment_count=2, pubkey_hex=node.pk_hex)
            filler["signature"] = "ab" * 10
            market_mod.store_claim(filler)
        order = make_maker_order()
        with pytest.raises(market_mod.ClaimRejected, match="limit"):
            self._call(node, order, {"amount_lapse": "1"})
        assert Trade.select().count() == 0
        assert Increment.select().count() == 0
        assert node.publish_claim_calls == []

    def test_a_buy_order_makes_the_taker_send_lapse(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order(direction="buy", price=1000)
        session_id = self._call(node, order, {"amount_lapse": "1"})
        trade = Trade.get(Trade.session_id == session_id)
        assert trade.i_send == "lapse"

    def test_opening_mover_actually_varies_step_two_with_trust(self, tmp_path):
        """Not the literal old behaviour (i_open hardcoded True for every
        step): with real shared history between this taker and this
        maker, whichever side is the *more* established one must not
        open step 2, and swapping which side that is must flip it.

        A tie needs completed trades to even have a nonzero score (see
        trust.score's Sybil defence: no history means no standing
        whatever the balance), so both scenarios below seed identical
        history and differ only in whose balance is larger.
        """
        def step_two_mover(taker_balance, maker_balance):
            trade_storage.PeerRecord.delete().execute()
            trust_mod.record_completed("maker.lapse", 50 * LAPSE)
            node = TakerNode(
                tmp_path, name=f"taker-{taker_balance}-{maker_balance}",
                heights_by_addr={"maker.lapse": [(0, "h")]})
            node.view = _View(1_000_000, balances={
                node.addr: taker_balance, "maker.lapse": maker_balance})
            node.storage = _Storage({"maker.lapse": [(0, "h")],
                                     node.addr: [(0, "h")]})
            order = make_maker_order(order_id=f"order-{taker_balance}-{maker_balance}")
            session_id = self._call(node, order, {"amount_lapse": "1"})
            trade = Trade.get(Trade.session_id == session_id)
            assert trade.increment_count >= 2
            return Increment.get(Increment.id == f"{session_id}:2").i_move_first

        richer_taker = step_two_mover(taker_balance=10**12, maker_balance=1)
        richer_maker = step_two_mover(taker_balance=1, maker_balance=10**12)
        assert richer_taker != richer_maker, \
            "step 2's mover must depend on which side trust favours"
