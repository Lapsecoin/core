"""The Trades page's worker-status view, and _start_trade.

A stalled trade must not look the same whether the wallet is locked,
Horizon is unreachable, or the counterparty genuinely vanished, so this
checks that market_routes._worker_view tells those apart correctly from
whatever swap_worker.status() reports.

_start_trade is the taker's half of the fill-request handshake: it has
to open a passphrase-sealed wallet, check this side can fund the fill,
and publish a signed fill request for the maker to answer, all without
a running Flask app around it. Nothing is opened as a trade until the
maker's own accept comes back (see swap_engine.check_fill_responses).
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


@pytest.fixture(autouse=True)
def plenty_of_xlm(monkeypatch):
    """Every test below is about LapseCoin-side logic, not Horizon, so the
    solvency check's XLM leg defaults to "funded" everywhere; the handful
    of tests that exist to exercise that check override this themselves."""
    monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 10**18)


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
        self.publish_order_calls = []
        self.publish_fill_request_calls = []

    def publish_claim(self, claim):
        self.publish_claim_calls.append(claim)

    def publish_order(self, order):
        self.publish_order_calls.append(order)

    def publish_fill_request(self, req):
        self.publish_fill_request_calls.append(req)


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

    def test_sends_a_fill_request_and_creates_no_trade_yet(self, tmp_path):
        """Nothing is created until the maker explicitly agrees (see
        swap_engine.check_fill_responses): _start_trade's whole job is
        sending a signed request, not opening a trade on its own say-so."""
        node = TakerNode(tmp_path)
        order = make_maker_order(direction="sell", price=1000)
        session_id = self._call(node, order, {"amount_lapse": "1"})

        assert Trade.select().count() == 0
        assert len(node.publish_fill_request_calls) == 1
        req = node.publish_fill_request_calls[0]
        assert req["session_id"] == session_id
        assert req["order_id"] == order.order_id
        assert req["taker_lapse_addr"] == node.addr
        assert req["taker_xlm_addr"] == node.xlm_addr
        assert req["lapse_total"] == 1 * LAPSE
        assert market_mod.verify_fill_request(req) is True
        assert market_mod.get_fill_request(req["request_id"]) is not None

    def test_fill_larger_than_the_order_is_refused(self, tmp_path):
        node = TakerNode(tmp_path)
        order = make_maker_order(lapse_total=1 * LAPSE)
        with pytest.raises(market_mod.OrderRejected, match="left"):
            self._call(node, order, {"amount_lapse": "5"})
        assert Trade.select().count() == 0
        assert node.publish_fill_request_calls == []

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
        assert node.publish_fill_request_calls == []

    def test_fill_request_cap_hit_raises_and_publishes_nothing(self, tmp_path):
        """The per-taker fill-request cap is the network's own admission
        control; hitting it must not leave a request half-sent."""
        node = TakerNode(tmp_path)
        for i in range(market_mod.MAX_FILL_REQUESTS_PER_TAKER):
            filler = market_mod.build_fill_request(
                order_id="other-order", session_id=f"filler{i}" * 4,
                taker_lapse_addr=node.addr, taker_xlm_addr=node.xlm_addr,
                lapse_total=1, pubkey_hex=node.pk_hex)
            filler["signature"] = "ab" * 10
            market_mod.store_fill_request(filler)
        order = make_maker_order()
        with pytest.raises(market_mod.FillRequestRejected, match="limit"):
            self._call(node, order, {"amount_lapse": "1"})
        assert Trade.select().count() == 0
        assert node.publish_fill_request_calls == []

    def test_a_buy_order_makes_the_taker_send_lapse(self, tmp_path):
        """i_send is derived and checked here even though the trade
        itself is not opened yet, so a fill nobody could pay for is
        refused before a request ever reaches the maker."""
        node = TakerNode(tmp_path)
        node.view.state.balances[node.addr] = 10**12
        order = make_maker_order(direction="buy", price=1000)
        self._call(node, order, {"amount_lapse": "1"})
        assert len(node.publish_fill_request_calls) == 1

    def test_an_expired_order_cannot_be_filled(self, tmp_path):
        """market_take fetches the order by id directly, bypassing the
        expiry filter open_orders() normally applies, so a stale link or
        a fill racing the order's own aging-out must be caught here too
        (plan item 4.4)."""
        node = TakerNode(tmp_path, height=1000)
        order = make_maker_order(expiry_block=999)   # already behind the tip
        with pytest.raises(market_mod.OrderRejected, match="expired"):
            self._call(node, order, {"amount_lapse": "1"})
        assert Trade.select().count() == 0
        assert node.publish_fill_request_calls == []

    def test_an_order_expiring_exactly_this_block_cannot_be_filled(self, tmp_path):
        node = TakerNode(tmp_path, height=1000)
        order = make_maker_order(expiry_block=1000)
        with pytest.raises(market_mod.OrderRejected, match="expired"):
            self._call(node, order, {"amount_lapse": "1"})

    def test_a_taker_without_enough_xlm_is_refused(self, tmp_path, monkeypatch):
        """direction='sell' means the taker pays XLM; a taker who cannot
        cover that leg must never even send a request (plan item 4.4)."""
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 0)
        node = TakerNode(tmp_path)
        order = make_maker_order(direction="sell", price=1000)
        with pytest.raises(ValueError, match="XLM"):
            self._call(node, order, {"amount_lapse": "1"})
        assert Trade.select().count() == 0
        assert node.publish_fill_request_calls == []

    def test_a_taker_without_enough_lapse_is_refused(self, tmp_path):
        """direction='buy' means the taker pays LAPSE."""
        node = TakerNode(tmp_path)   # no balance seeded: get_balance is 0
        order = make_maker_order(direction="buy", price=1000)
        with pytest.raises(ValueError, match="LAPSE"):
            self._call(node, order, {"amount_lapse": "1"})
        assert Trade.select().count() == 0
        assert node.publish_fill_request_calls == []

    def test_a_taker_with_exactly_enough_xlm_is_not_refused(self, tmp_path, monkeypatch):
        order = make_maker_order(direction="sell", price=1000)
        xlm_total = swap_mod.xlm_for_lapse(1 * LAPSE, 1000)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: xlm_total)
        node = TakerNode(tmp_path)
        session_id = self._call(node, order, {"amount_lapse": "1"})
        assert market_mod.get_fill_request(
            node.publish_fill_request_calls[0]["request_id"]) is not None


class TestPlaceOrder:
    def _call(self, node, form, height=1000):
        fake_request = FakeRequest({"passphrase": node.passphrase, **form})
        original = market_routes.request
        market_routes.request = fake_request
        try:
            return market_routes._place_order(node, node.xlm_addr, height)
        finally:
            market_routes.request = original

    def test_a_sell_order_needs_enough_lapse(self, tmp_path):
        node = TakerNode(tmp_path)   # balance defaults to 0
        with pytest.raises(ValueError, match="LAPSE"):
            self._call(node, {"direction": "sell", "amount_lapse": "1",
                              "price_xlm": "0.0001"})
        assert Order.select().count() == 0
        assert node.publish_order_calls == []

    def test_a_sell_order_with_enough_lapse_succeeds(self, tmp_path):
        node = TakerNode(tmp_path)
        node.view.state.balances[node.addr] = 10 * LAPSE
        self._call(node, {"direction": "sell", "amount_lapse": "1",
                          "price_xlm": "0.0001"})
        assert Order.select().count() == 1
        assert len(node.publish_order_calls) == 1

    def test_a_buy_order_needs_enough_xlm(self, tmp_path, monkeypatch):
        """Plan item 4.4: only the sell side used to check its own asset;
        a buy order committed the maker to paying out XLM it never had."""
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 0)
        node = TakerNode(tmp_path)
        with pytest.raises(ValueError, match="XLM"):
            self._call(node, {"direction": "buy", "amount_lapse": "1",
                              "price_xlm": "0.0001"})
        assert Order.select().count() == 0
        assert node.publish_order_calls == []

    def test_a_buy_order_with_enough_xlm_succeeds(self, tmp_path, monkeypatch):
        required = swap_mod.xlm_for_lapse(1 * LAPSE, xlm_mod.str_to_stroops("0.0001"))
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: required)
        node = TakerNode(tmp_path)
        self._call(node, {"direction": "buy", "amount_lapse": "1",
                          "price_xlm": "0.0001"})
        assert Order.select().count() == 1

    def test_a_buy_order_with_exactly_not_enough_xlm_is_refused(self, tmp_path, monkeypatch):
        required = swap_mod.xlm_for_lapse(1 * LAPSE, xlm_mod.str_to_stroops("0.0001"))
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: required - 1)
        node = TakerNode(tmp_path)
        with pytest.raises(ValueError, match="XLM"):
            self._call(node, {"direction": "buy", "amount_lapse": "1",
                              "price_xlm": "0.0001"})


class TestSuggestedPrice:
    """What prefills the order form's price field (plan item 5.2's
    ticker feeding into order placement, not just the Market page's
    display)."""

    def test_top_of_book_wins_when_present(self):
        best = {"best_sell": 1000, "best_buy": 900}
        assert market_routes._suggested_price(best, ticker=500) == \
            market_routes.fmt_xlm(1000)

    def test_falls_back_to_the_ticker_when_the_book_is_empty(self):
        best = {"best_sell": None, "best_buy": None}
        assert market_routes._suggested_price(best, ticker=777) == \
            market_routes.fmt_xlm(777)

    def test_blank_when_neither_exists(self):
        best = {"best_sell": None, "best_buy": None}
        assert market_routes._suggested_price(best, ticker=None) == ""

    def test_ticker_is_optional_for_backward_compatible_callers(self):
        best = {"best_sell": None, "best_buy": None}
        assert market_routes._suggested_price(best) == ""


class TestMakerXlmUnfunded:
    """The market_take warning: a taker paying XLM into a seller's
    never-funded account pays the network minimum, not just the agreed
    step, on the very first payment."""

    class _Order:
        def __init__(self, direction, maker_xlm_addr="GMAKER"):
            self.direction = direction
            self.maker_xlm_addr = maker_xlm_addr

    def test_sell_order_into_an_unfunded_maker_warns(self):
        order = self._Order("sell")
        assert market_routes._maker_xlm_unfunded(order, lambda addr: False) is True

    def test_sell_order_into_a_funded_maker_does_not_warn(self):
        order = self._Order("sell")
        assert market_routes._maker_xlm_unfunded(order, lambda addr: True) is False

    def test_unknown_horizon_state_never_warns(self):
        """A transient Horizon outage must not present a cost that might
        not even exist."""
        order = self._Order("sell")
        assert market_routes._maker_xlm_unfunded(order, lambda addr: None) is False

    def test_buy_order_never_warns_taker_pays_lapse_not_xlm(self):
        order = self._Order("buy")
        assert market_routes._maker_xlm_unfunded(order, lambda addr: False) is False


class TestMakerLapseOvercommitted:
    """The market_take warning for the fund-safety gap a single order's
    own post-time check cannot see: this maker's *other* open sell
    orders, combined with this one, asking for more LAPSE than the
    maker's chain balance actually holds."""

    class _Node:
        def __init__(self, balance):
            self.view = type("V", (), {"state": type(
                "S", (), {"get_balance": staticmethod(lambda addr: balance)})()})()

    def test_a_lone_affordable_sell_order_does_not_warn(self):
        order = make_maker_order(direction="sell", lapse_total=5 * LAPSE)
        assert market_routes._maker_lapse_overcommitted(
            order, self._Node(10 * LAPSE), height=100) is False

    def test_a_lone_order_bigger_than_the_balance_warns(self):
        order = make_maker_order(direction="sell", lapse_total=5 * LAPSE)
        assert market_routes._maker_lapse_overcommitted(
            order, self._Node(1 * LAPSE), height=100) is True

    def test_two_affordable_alone_but_not_together_warns(self):
        """Neither order alone exceeds the balance; together they do,
        which is exactly the gap swap_engine._pending_send_total closes
        at accept time and this surfaces before a request is even sent."""
        maker = "maker.addr"
        order_a = make_maker_order(order_id="a", direction="sell",
                                   lapse_total=6 * LAPSE, maker_lapse=maker)
        make_maker_order(order_id="b", direction="sell",
                         lapse_total=6 * LAPSE, maker_lapse=maker)
        assert market_routes._maker_lapse_overcommitted(
            order_a, self._Node(10 * LAPSE), height=100) is True

    def test_buy_order_never_warns_this_check_is_lapse_side_only(self):
        order = make_maker_order(direction="buy", lapse_total=5 * LAPSE)
        assert market_routes._maker_lapse_overcommitted(
            order, self._Node(0), height=100) is False


class TestAutoFill:
    """The market-order half of the market (raised directly by the user:
    trading should not require reading the book and copying an
    order_id). _auto_fill sweeps the best-priced compatible orders and
    fills as much as is safe, without any order being chosen by hand."""

    def _call(self, node, form, height=1000, depth=2, stranger_cap=5 * XLM):
        fake_request = FakeRequest({"passphrase": node.passphrase, **form})
        original = market_routes.request
        market_routes.request = fake_request
        try:
            return market_routes._auto_fill(
                node, node.xlm_keyfile, height, depth, stranger_cap)
        finally:
            market_routes.request = original

    def test_fills_the_single_best_priced_order_first(self, tmp_path):
        make_maker_order(order_id="cheap", direction="sell", price=1000,
                         lapse_total=10 * LAPSE, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        make_maker_order(order_id="pricey", direction="sell", price=2000,
                         lapse_total=10 * LAPSE, maker_lapse="maker2.lapse",
                         maker_xlm="GMAKER2")
        node = TakerNode(tmp_path)
        result = self._call(node, {"direction": "buy", "amount_lapse": "2"})

        assert result["filled"] == 2 * LAPSE
        assert result["remaining"] == 0
        assert len(result["fills"]) == 1
        assert result["fills"][0]["lapse"] == 2 * LAPSE
        assert result["fills"][0]["price"] == 1000
        req = trade_storage.FillRequest.get(
            trade_storage.FillRequest.session_id == result["fills"][0]["session_id"])
        assert req.order_id == "cheap"

    def test_a_price_limit_excludes_worse_priced_orders(self, tmp_path):
        make_maker_order(order_id="cheap", direction="sell", price=1000,
                         lapse_total=10 * LAPSE, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        make_maker_order(order_id="pricey", direction="sell", price=2000,
                         lapse_total=10 * LAPSE, maker_lapse="maker2.lapse",
                         maker_xlm="GMAKER2")
        node = TakerNode(tmp_path)
        # 1500 stroops/tick: between the two orders' prices, so only the
        # cheaper one qualifies.
        result = self._call(node, {"direction": "buy", "amount_lapse": "15",
                                   "max_price_xlm": "0.00015"})

        assert result["filled"] == 10 * LAPSE   # all of the cheap order
        assert result["remaining"] == 5 * LAPSE
        assert len(result["fills"]) == 1
        assert trade_storage.FillRequest.select().count() == 1
        req = trade_storage.FillRequest.get(
            trade_storage.FillRequest.session_id == result["fills"][0]["session_id"])
        assert req.order_id == "cheap"

    def test_splits_across_orders_once_one_counterpartys_cap_is_reached(self, tmp_path):
        tiny_cap = 10_000
        max_safe_lapse = swap_mod.lapse_for_xlm(
            swap_mod.max_safe_trade_stroops(
                swap_mod.exposure_cap_stroops(0, tiny_cap)), 1000)
        make_maker_order(order_id="m1", direction="sell", price=1000,
                         lapse_total=max_safe_lapse * 3, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        make_maker_order(order_id="m2", direction="sell", price=1000,
                         lapse_total=max_safe_lapse * 3, maker_lapse="maker2.lapse",
                         maker_xlm="GMAKER2")
        node = TakerNode(tmp_path)
        requested_lapse = max_safe_lapse * 2
        result = self._call(
            node, {"direction": "buy",
                  "amount_lapse": str(requested_lapse / LAPSE)},
            stranger_cap=tiny_cap)

        assert result["filled"] == requested_lapse
        assert result["remaining"] == 0
        assert len(result["fills"]) == 2
        assert trade_storage.FillRequest.select().count() == 2
        makers = {f["maker"] for f in result["fills"]}
        assert makers == {"maker1.lapse", "maker2.lapse"}

    def test_nothing_on_the_book_reports_zero_filled_not_an_error(self, tmp_path):
        node = TakerNode(tmp_path)
        result = self._call(node, {"direction": "buy", "amount_lapse": "5"})
        assert result["filled"] == 0
        assert result["remaining"] == 5 * LAPSE
        assert result["fills"] == []
        assert Trade.select().count() == 0

    def test_wrong_passphrase_raises_once_rather_than_looping(self, tmp_path):
        make_maker_order(direction="sell", price=1000, lapse_total=10 * LAPSE)
        node = TakerNode(tmp_path)
        fake_request = FakeRequest({"passphrase": "wrong", "direction": "buy",
                                    "amount_lapse": "1"})
        original = market_routes.request
        market_routes.request = fake_request
        try:
            with pytest.raises(ValueError, match="passphrase"):
                market_routes._auto_fill(node, node.xlm_keyfile, 1000, 2, 5 * XLM)
        finally:
            market_routes.request = original
        assert Trade.select().count() == 0

    def test_a_slice_below_an_orders_own_minimum_fill_is_skipped(self, tmp_path):
        make_maker_order(direction="sell", price=1000, lapse_total=10 * LAPSE,
                         min_fill=5 * LAPSE)
        node = TakerNode(tmp_path)
        result = self._call(node, {"direction": "buy", "amount_lapse": "1"})

        assert result["filled"] == 0
        assert result["remaining"] == 1 * LAPSE
        assert result["fills"] == []
        assert result["skipped"]
        assert Trade.select().count() == 0

    def test_selling_matches_against_the_best_buy_order_first(self, tmp_path):
        """For a seller, the best price is the *highest* standing bid."""
        make_maker_order(order_id="low", direction="buy", price=1000,
                         lapse_total=10 * LAPSE, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        make_maker_order(order_id="high", direction="buy", price=2000,
                         lapse_total=10 * LAPSE, maker_lapse="maker2.lapse",
                         maker_xlm="GMAKER2")
        node = TakerNode(tmp_path)
        node.view.state.balances[node.addr] = 10**12   # enough LAPSE to sell
        result = self._call(node, {"direction": "sell", "amount_lapse": "2"})

        assert len(result["fills"]) == 1
        assert result["fills"][0]["maker"] == "maker2.lapse"

    def test_a_minimum_below_what_the_book_can_supply_trades_nothing_at_all(self, tmp_path):
        """The whole point of the minimum: a sweep that would only ever
        get you a token amount should refuse outright rather than open
        a trade you didn't actually want, since an opened trade cannot
        be cheaply undone (it is a claim already published, not a
        pending order this node can just forget)."""
        make_maker_order(order_id="only", direction="sell", price=1000,
                         lapse_total=2 * LAPSE, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        node = TakerNode(tmp_path)
        result = self._call(node, {"direction": "buy", "amount_lapse": "10",
                                   "min_total_lapse": "5"})

        assert result["min_not_met"] is True
        assert result["filled"] == 0
        assert result["fills"] == []
        assert Trade.select().count() == 0, \
            "nothing may be opened once the minimum cannot be met"

    def test_a_minimum_the_book_can_meet_still_trades_normally(self, tmp_path):
        make_maker_order(order_id="only", direction="sell", price=1000,
                         lapse_total=10 * LAPSE, maker_lapse="maker1.lapse",
                         maker_xlm="GMAKER1")
        node = TakerNode(tmp_path)
        result = self._call(node, {"direction": "buy", "amount_lapse": "3",
                                   "min_total_lapse": "2"})

        assert result["min_not_met"] is False
        assert result["filled"] == 3 * LAPSE
        assert trade_storage.FillRequest.select().count() == 1


class TestAutoFillMessage:
    def _fill(self, session_id, lapse, price):
        return {"session_id": session_id, "lapse": lapse, "price": price,
                "maker": "maker.lapse"}

    def test_full_fill_single_trade(self):
        msg = market_routes._auto_fill_message(
            {"filled": 2 * LAPSE, "remaining": 0,
             "fills": [self._fill("s1", 2 * LAPSE, 1000)], "skipped": []})
        assert "2.0000 LAPSE" in msg
        assert "1 trade" in msg
        assert "trades" not in msg

    def test_full_fill_pluralizes_multiple_trades(self):
        msg = market_routes._auto_fill_message(
            {"filled": 2 * LAPSE, "remaining": 0,
             "fills": [self._fill("s1", 1 * LAPSE, 1000),
                      self._fill("s2", 1 * LAPSE, 1000)], "skipped": []})
        assert "2 trades" in msg

    def test_different_prices_are_reported_as_separate_tiers(self):
        """The 'bought a at x, b at y' shape the report should have,
        rather than one blended average that hides what actually
        happened at each price."""
        msg = market_routes._auto_fill_message(
            {"filled": 5 * LAPSE, "remaining": 0,
             "fills": [self._fill("s1", 2 * LAPSE, 1000),
                      self._fill("s2", 3 * LAPSE, 2000)], "skipped": []})
        assert "2.0000 LAPSE at" in msg
        assert "3.0000 LAPSE at" in msg
        assert msg.index("2.0000") < msg.index("3.0000")

    def test_partial_fill_mentions_what_is_left(self):
        msg = market_routes._auto_fill_message(
            {"filled": 1 * LAPSE, "remaining": 1 * LAPSE,
             "fills": [self._fill("s1", 1 * LAPSE, 1000)], "skipped": []})
        assert "1.0000 LAPSE" in msg
        assert "could not be filled" in msg

    def test_zero_fill_suggests_a_standing_order(self):
        msg = market_routes._auto_fill_message(
            {"filled": 0, "remaining": 5 * LAPSE, "fills": [], "skipped": []})
        assert "Nothing could be filled" in msg
        assert "resting order" in msg

    def test_min_not_met_explains_why_nothing_traded(self):
        msg = market_routes._auto_fill_message({
            "min_not_met": True, "would_have_filled": 2 * LAPSE,
            "min_total": 5 * LAPSE, "filled": 0, "remaining": 10 * LAPSE,
            "fills": [], "skipped": [],
        })
        assert "2.0000 LAPSE" in msg
        assert "5.0000 LAPSE" in msg
        assert "nothing was traded" in msg


class TestMyOrders:
    """The maker's own progress view: how much of an order has actually
    settled versus merely been claimed and not yet paid for, since those
    are different things to tell someone managing their own order."""

    class _Node:
        def __init__(self, addr):
            self.addr = addr

    def test_untouched_order_shows_zero_progress(self):
        make_maker_order(order_id="o1", maker_lapse="me.lapse",
                         lapse_total=10 * LAPSE)
        rows = market_routes._my_orders(self._Node("me.lapse"), height=100)
        assert len(rows) == 1
        assert rows[0]["delivered"] == 0
        assert rows[0]["reserved"] == 0
        assert rows[0]["pct_delivered"] == 0
        assert rows[0]["remaining"] == 10 * LAPSE

    def test_an_accepted_fill_response_shows_as_reserved_not_delivered(self):
        make_maker_order(order_id="o1", maker_lapse="me.lapse",
                         lapse_total=10 * LAPSE)
        market_mod.FillResponse.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            lapse_total=4 * LAPSE, accepted=True, increment_count=3,
            reason="", maker_pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        rows = market_routes._my_orders(self._Node("me.lapse"), height=100)

        assert rows[0]["delivered"] == 0
        assert rows[0]["reserved"] == 4 * LAPSE
        assert rows[0]["remaining"] == 6 * LAPSE
        assert rows[0]["pct_delivered"] == 0

    def test_a_settled_step_shows_as_delivered_percentage(self):
        order = make_maker_order(order_id="o1", maker_lapse="me.lapse",
                                 lapse_total=10 * LAPSE)
        Trade.create(
            session_id="s" * 16, order_id="o1", role="maker",
            my_lapse_addr="me.lapse", my_xlm_addr="GME",
            peer_lapse_addr="taker.lapse", peer_xlm_addr="GTAKER",
            i_send="lapse", lapse_total=5 * LAPSE, xlm_total=5000 * XLM,
            increment_count=1, confirm_depth=1, status="active",
            created_at=time.time(), updated_at=time.time())
        Increment.create(
            id="s" * 16 + ":1", session_id="s" * 16, n=1,
            lapse_amount=5 * LAPSE, xlm_amount=5000 * XLM,
            i_move_first=True, out_state="settled", in_state="settled",
            created_at=time.time(), deadline_at=time.time() + 3600)
        rows = market_routes._my_orders(self._Node("me.lapse"), height=100)

        assert rows[0]["delivered"] == 5 * LAPSE
        assert rows[0]["pct_delivered"] == 50
        assert rows[0]["remaining"] == 5 * LAPSE


class TestPendingRequests:
    """The taker's own view of a fill request that has not yet become a
    trade one way or the other: pure observability, nothing to click,
    and gone from this list the moment it does become a trade."""

    class _Node:
        def __init__(self, addr):
            self.addr = addr

    def test_unanswered_request_shows_as_pending(self):
        make_maker_order(order_id="o1", maker_lapse="maker.lapse", direction="sell")
        market_mod.FillRequest.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=2 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        rows = market_routes._pending_requests(self._Node("me.lapse"))
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["maker_lapse_addr"] == "maker.lapse"
        assert rows[0]["lapse_total"] == 2 * LAPSE

    def test_accepted_response_shows_as_accepted(self):
        make_maker_order(order_id="o1", maker_lapse="maker.lapse")
        market_mod.FillRequest.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=2 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        market_mod.FillResponse.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            lapse_total=2 * LAPSE, accepted=True, increment_count=3,
            reason="", maker_pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        rows = market_routes._pending_requests(self._Node("me.lapse"))
        assert rows[0]["status"] == "accepted"

    def test_declined_response_shows_the_reason(self):
        make_maker_order(order_id="o1", maker_lapse="maker.lapse")
        market_mod.FillRequest.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=2 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        market_mod.FillResponse.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            lapse_total=2 * LAPSE, accepted=False, increment_count=None,
            reason="no room left", maker_pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        rows = market_routes._pending_requests(self._Node("me.lapse"))
        assert rows[0]["status"] == "declined"
        assert rows[0]["detail"] == "no room left"

    def test_a_request_that_already_became_a_trade_is_excluded(self):
        """Once check_fill_responses has opened the trade, this list is
        not the place for it any more; the active trades list is."""
        make_maker_order(order_id="o1", maker_lapse="maker.lapse")
        market_mod.FillRequest.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=2 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        Trade.create(
            session_id="s" * 16, order_id="o1", role="taker",
            my_lapse_addr="me.lapse", my_xlm_addr="GME",
            peer_lapse_addr="maker.lapse", peer_xlm_addr="GMAKER",
            i_send="xlm", lapse_total=2 * LAPSE, xlm_total=2000 * XLM,
            increment_count=3, confirm_depth=2, status="active",
            created_at=time.time(), updated_at=time.time())
        assert market_routes._pending_requests(self._Node("me.lapse")) == []

    def test_someone_elses_requests_are_not_shown(self):
        make_maker_order(order_id="o1", maker_lapse="maker.lapse")
        market_mod.FillRequest.create(
            request_id="r" * 16, order_id="o1", session_id="s" * 16,
            taker_lapse_addr="someone.else", taker_xlm_addr="GELSE",
            lapse_total=2 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        assert market_routes._pending_requests(self._Node("me.lapse")) == []

    def test_newest_request_first(self):
        make_maker_order(order_id="o1", maker_lapse="maker.lapse")
        market_mod.FillRequest.create(
            request_id="old" * 6, order_id="o1", session_id="old" * 6,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=1 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time() - 100)
        market_mod.FillRequest.create(
            request_id="new" * 6, order_id="o1", session_id="new" * 6,
            taker_lapse_addr="me.lapse", taker_xlm_addr="GME",
            lapse_total=1 * LAPSE, pubkey="ab" * 10, signature="cd" * 10,
            received_at=time.time())
        rows = market_routes._pending_requests(self._Node("me.lapse"))
        assert [r["order_id"] for r in rows] == ["o1", "o1"]
        assert rows[0]["sent_at"] > rows[1]["sent_at"]
