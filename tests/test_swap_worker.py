"""The worker that drives trades.

The property that matters most here is an ordering one: a node coming
back up must reconcile against both chains before it sends anything.
Sending first and reconciling after is exactly how a restart pays twice,
and no amount of care inside the engine helps if the worker calls it in
the wrong order.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import settings as settings_mod
import storage as storage_mod
import swap
import swap_engine
import swap_worker
import trade_storage
import xlm as xlm_mod
from test_swap_engine import FakeChain, make_trade, settle_peer_leg
from trade_storage import Increment, Trade, TRADE_ACTIVE, TRADE_STALLED


@pytest.fixture(autouse=True)
def fresh_db():
    storage_mod.db.init(":memory:")
    storage_mod.db.connect(reuse_if_open=True)
    storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
    storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
    trade_storage._initialised = True
    yield
    storage_mod.db.close()


class FakeSettings:
    def __init__(self, enabled=True):
        self.values = {settings_mod.SWAP_ENABLED.key: enabled}

    def get(self, setting):
        return self.values.get(setting.key, setting.default)

    def set(self, setting, value):
        self.values[setting.key] = value


class FakeNode:
    def __init__(self, enabled=True, unlocked=True):
        self.settings = FakeSettings(enabled)
        self._kek = b"k" * 32 if unlocked else None
        self.keyfile = "/nonexistent/node.key"


class Worker(swap_worker.SwapWorker):
    """A worker wired to fake chains instead of real ones."""

    def __init__(self, node, lapse=None, xlm=None, seed="seed", **kw):
        super().__init__(node, "/nonexistent/xlm.key", **kw)
        self.lapse = lapse or FakeChain("lapse", "me.lapse")
        self.xlm = xlm or FakeChain("xlm", "GME")
        self._seed = seed
        self.calls = []

    def _secrets(self):
        if self.node._kek is None or self._seed is None:
            return None, None
        return self.node._kek, self._seed

    def _engine(self):
        engine = swap_engine.Engine(self.lapse, self.xlm, self._secrets)
        self.calls.append("engine")
        return engine


class TestGating:
    def test_does_nothing_while_swaps_are_off(self):
        w = Worker(FakeNode(enabled=False))
        make_trade()
        assert w.run_once() == 0
        assert w.lapse.payments == []

    def test_does_nothing_while_locked(self):
        """A node that cannot sign must not send, and that is correct
        behaviour rather than an error worth reporting every pass."""
        w = Worker(FakeNode(unlocked=False))
        make_trade()
        assert w.run_once() == 0
        assert w.lapse.payments == []

    def test_missing_trading_wallet_is_not_an_error(self):
        w = Worker(FakeNode(), seed=None)
        make_trade()
        assert w.run_once() == 0

    def test_recover_is_a_no_op_while_swaps_are_off(self):
        w = Worker(FakeNode(enabled=False))
        assert w.recover() == 0


class TestOrdering:
    def test_reconciles_before_sending_on_startup(self):
        """The ordering that makes a restart safe.

        A payment already on chain that this node has no record of must be
        found by reconciliation, not re-sent by the first pass.
        """
        w = Worker(FakeNode())
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        engine = w._engine()
        terms = engine._out_terms(trade, inc)
        w.lapse.deliver(*terms)          # paid, but this node never recorded it
        assert inc.out_state == trade_storage.LEG_PENDING

        w.recover()
        inc = Increment.get(Increment.id == inc.id)
        assert inc.out_state == trade_storage.LEG_SETTLED
        assert w.lapse.count_for(swap.session_tag(trade.order_id, trade.session_id, 1)) == 1

    def test_pass_after_recovery_does_not_resend(self):
        w = Worker(FakeNode())
        trade = make_trade()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        terms = w._engine()._out_terms(trade, inc)
        w.lapse.deliver(*terms)
        w.recover()
        w.run_once()
        assert w.lapse.count_for(swap.session_tag(trade.order_id, trade.session_id, 1)) == 1


class TestDriving:
    def test_pass_advances_an_active_trade(self):
        w = Worker(FakeNode())
        trade = make_trade()
        assert w.run_once() == 1
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == trade_storage.LEG_SETTLED

    def test_repeated_passes_never_double_pay(self):
        w = Worker(FakeNode())
        trade = make_trade()
        for _ in range(10):
            w.run_once()
        assert w.lapse.count_for(swap.session_tag(trade.order_id, trade.session_id, 1)) == 1

    def test_finished_trades_are_left_alone(self):
        w = Worker(FakeNode())
        trade = make_trade(count=2)

        # Drive to completion, paying the counterparty's side each pass.
        for _ in range(12):
            w.run_once()
            fresh = Trade.get(Trade.session_id == trade.session_id)
            if fresh.status == trade_storage.TRADE_COMPLETED:
                break
            engine = w._engine()
            for step in (Increment.select()
                         .where(Increment.session_id == trade.session_id)
                         .order_by(Increment.n)):
                if step.in_state != trade_storage.LEG_SETTLED:
                    settle_peer_leg(fresh, step, engine)
                    break

        assert Trade.get(Trade.session_id == trade.session_id).status == \
            trade_storage.TRADE_COMPLETED

        before = len(w.lapse.payments) + len(w.xlm.payments)
        w.run_once()
        assert len(w.lapse.payments) + len(w.xlm.payments) == before

    def test_one_bad_trade_does_not_stop_the_others(self):
        w = Worker(FakeNode())
        good = make_trade()
        broken = make_trade()
        # Give the second trade a step schedule that cannot be read.
        Increment.delete().where(Increment.session_id == broken.session_id).execute()
        Increment.create(id=f"{broken.session_id}:1", session_id=broken.session_id,
                         n=1, lapse_amount=0, xlm_amount=0, i_move_first=True,
                         created_at=time.time(), deadline_at=0)
        w.run_once()
        inc = Increment.get(Increment.id == f"{good.session_id}:1")
        assert inc.out_state == trade_storage.LEG_SETTLED


class TestUnreachable:
    def test_outage_pauses_rather_than_hammering(self):
        w = Worker(FakeNode())
        make_trade()
        w.lapse.unreachable = True
        w.run_once()
        assert w._unreachable_until > time.time()

    def test_paused_worker_skips_its_pass(self):
        w = Worker(FakeNode())
        make_trade()
        w._unreachable_until = time.time() + 60
        assert w.run_once() == 0
        assert w.lapse.payments == []

    def test_outage_blames_nobody(self):
        w = Worker(FakeNode())
        trade = make_trade()
        w.lapse.unreachable = True
        w.run_once()
        assert Trade.get(Trade.session_id == trade.session_id).status != \
            trade_storage.TRADE_ABANDONED

    def test_recovers_after_the_backoff(self):
        w = Worker(FakeNode())
        trade = make_trade()
        w.lapse.unreachable = True
        w.run_once()
        w.lapse.unreachable = False
        w._unreachable_until = 0
        w.run_once()
        inc = Increment.get(Increment.id == f"{trade.session_id}:1")
        assert inc.out_state == trade_storage.LEG_SETTLED


class TestBlame:
    def test_stall_alone_does_not_blame(self):
        w = Worker(FakeNode())
        trade = make_trade()
        Increment.update(deadline_at=time.time() - 1).execute()
        w.run_once()
        assert Trade.get(Trade.session_id == trade.session_id).status == \
            TRADE_STALLED

    def test_peer_who_never_accepted_is_never_blamed(self):
        """An unsolicited payment nobody answered is not a defection, and
        this is the path a reputation attack would come down."""
        w = Worker(FakeNode())
        trade = make_trade()
        w.run_once()
        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.stalled_since = time.time() - swap_engine.ABANDON_AFTER_SECONDS - 10
        trade.save()
        w.run_once()
        assert Trade.get(Trade.session_id == trade.session_id).status != \
            trade_storage.TRADE_ABANDONED

    def test_peer_who_accepted_then_stopped_is_blamed(self):
        w = Worker(FakeNode())
        trade = make_trade()
        w.run_once()
        first = Increment.get(Increment.id == f"{trade.session_id}:1")
        settle_peer_leg(Trade.get(Trade.session_id == trade.session_id),
                        first, w._engine())
        w.run_once()
        second = Increment.get(Increment.id == f"{trade.session_id}:2")
        settle_peer_leg(Trade.get(Trade.session_id == trade.session_id),
                        second, w._engine())
        w.run_once()
        w.run_once()

        trade = Trade.get(Trade.session_id == trade.session_id)
        trade.status = TRADE_STALLED
        trade.stalled_since = time.time() - swap_engine.ABANDON_AFTER_SECONDS - 10
        trade.save()
        w.run_once()
        assert Trade.get(Trade.session_id == trade.session_id).status == \
            trade_storage.TRADE_ABANDONED


class TestStatus:
    def test_status_reports_the_gates(self):
        w = Worker(FakeNode())
        status = w.status()
        assert status["enabled"] is True
        assert status["unlocked"] is True

    def test_status_shows_locked(self):
        w = Worker(FakeNode(unlocked=False))
        assert w.status()["unlocked"] is False


class TestKekSealedWallet:
    """The trading wallet must open with the node's own key, so an
    unattended node can finish a trade it has already paid into."""

    def test_roundtrip_under_a_kek(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        path = str(tmp_path / "xlm.key")
        kek = b"k" * 32
        xlm_mod.save_key(path, seed, public, kek=kek)
        assert xlm_mod.decrypt_seed(path, kek=kek) == seed

    def test_wrong_kek_refused(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm_mod.save_key(path, seed, public, kek=b"k" * 32)
        with pytest.raises(ValueError):
            xlm_mod.decrypt_seed(path, kek=b"x" * 32)

    def test_passphrase_on_a_kek_wallet_says_so(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm_mod.save_key(path, seed, public, kek=b"k" * 32)
        with pytest.raises(ValueError, match="unlock the node"):
            xlm_mod.decrypt_seed(path, passphrase="pw")

    def test_passphrase_wallets_still_work(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm_mod.save_key(path, seed, public, passphrase="pw")
        assert xlm_mod.decrypt_seed(path, passphrase="pw") == seed

    def test_cannot_supply_both(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        with pytest.raises(ValueError):
            xlm_mod.save_key(str(tmp_path / "k"), seed, public,
                             passphrase="pw", kek=b"k" * 32)

    def test_cannot_supply_neither(self, tmp_path):
        seed, public = xlm_mod.generate_keypair()
        with pytest.raises(ValueError):
            xlm_mod.save_key(str(tmp_path / "k"), seed, public)
