"""The worker: who claims, who pays and when, and what keeps it from paying twice."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base_wallet
import crypto
import evm
import gas
import gas_io
import gas_worker
import gaslock
import relay
import settings as settings_mod
import tx as tx_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_block, make_tx

ESC = crypto.escrow_address()
NET = gas.NETWORKS["optimism"]
DEST = evm.address_from_secret((21).to_bytes(32, "big"))
TARGET = 5 * 10 ** 14                    # $1.25 at the fake price: under the ceiling
PRICE = 2500.0
RICH = 10 ** 18                          # a funded Base wallet: $2500 at the fake price
ASKER = 1
ME = 2


class Meta:
    def __init__(self):
        self.d = {}

    def get_meta(self, k, default=None):
        return self.d.get(k, default)

    def set_meta(self, k, v):
        self.d[k] = v


class FakeIO:
    def __init__(self):
        self.base = RICH
        self.base_of = {}                # base address -> wei, default RICH
        self.dest = 0
        self.paid = []
        self.claims = []
        self.fail_pay = None
        self.claim_ok = True
        self.gas_price_value = 10 ** 9
        self.listed = set()              # destination addresses on a sanctions list
        self.screening_down = False

    def price(self, net):
        return PRICE

    def gas_price(self, net):
        return self.gas_price_value

    def base_balance(self):
        return self.base

    def balance_of_base(self, addr):
        return self.base_of.get(addr, RICH)

    def dest_balance(self, net, addr):
        return self.dest

    def sanctioned(self, net, addr):
        if self.screening_down:
            raise evm.EVMUnreachable("oracle down")
        return addr in self.listed

    def claim(self, txid):
        self.claims.append(txid)
        return (self.claim_ok, "h" if self.claim_ok else "mempool full")

    def pay(self, net, dest, amount):
        if self.fail_pay:
            raise self.fail_pay
        self.paid.append((net.slug, dest, amount))
        return "0xpaid"


class World:
    """A chain of real signed transactions, one node under test, a fake outside."""

    def __init__(self, tmp_path):
        self.chain = [{"height": 0, "hash": "g" * 64, "transactions": []}]
        self.settings = settings_mod.Settings(Meta())
        keyfile = str(tmp_path / "node.key")
        sk, pk = crypto.generate_keypair()
        crypto.save_key(keyfile, sk, pk, "pw")
        self.kek = crypto.derive_kek(keyfile, "pw")
        self.wallet_path = str(tmp_path / "base_gas.key")
        base_wallet.create(self.wallet_path, self.kek)
        self.node = SimpleNamespace(
            addr=address(ME), settings=self.settings, storage=Meta(),
            base_wallet_path=self.wallet_path, _kek=self.kek,
            view=SimpleNamespace(chain=self.chain, height=0))
        self.io = FakeIO()
        self.worker = gas_worker.GasWorker(self.node, self.io)
        self.nonces = {}
        self.base_keys = {}              # lapse index -> Base secret

    # -- chain building -------------------------------------------------

    def mine(self, txs=()):
        h = len(self.chain)
        blk = make_block(h, self.chain[-1]["hash"], list(txs), builder_index=9)
        self.chain.append(blk)
        self.node.view.height = h
        return blk

    def mine_until(self, height):
        while len(self.chain) - 1 < height:
            self.mine()

    def _nonce(self, idx):
        self.nonces[idx] = self.nonces.get(idx, 0) + 1
        return self.nonces[idx]

    def request(self, lock=gas_worker.MIN_SERVED_LOCK, target=TARGET, dest=DEST, net=NET,
                sender=ASKER):
        memo = gas.build_request_memo(net.slug, target, dest)
        t = make_tx(sender, 0, 0, None, fee=1000, nonce_override=self._nonce(sender), memo=memo,
                    outputs_override=[{"to": ESC, "amount": lock}])
        self.mine([t])
        return tx_mod.tx_hash(t)

    def _claim_tx(self, txid, idx, bad_signature=False, base_secret=None):
        secret = base_secret or self.base_keys.setdefault(idx, os.urandom(32))
        msg = gas.claim_message(gas.request_ref(txid), address(idx))
        sig = evm.sign_message(b"nope" if bad_signature else msg, secret)
        memo = gas.build_claim_memo(txid, evm.address_from_secret(secret), sig)
        return make_tx(idx, 0, 1, None, fee=1000, nonce_override=self._nonce(idx),
                       outputs_override=[{"to": crypto.burn_address(), "amount": 1}], memo=memo)

    def claim(self, txid, idx, bad_signature=False, base_secret=None):
        self.mine([self._claim_tx(txid, idx, bad_signature, base_secret)])

    def claim_together(self, txid, idxs):
        """Several nodes' claims landing in the same block."""
        txs = []
        for idx in idxs:
            secret = base_wallet.decrypt_secret(self.wallet_path, self.kek) if idx == ME else None
            txs.append(self._claim_tx(txid, idx, base_secret=secret))
        self.mine(txs)

    def my_claim(self, txid):
        """A claim by the node under test, signed with its real gas wallet."""
        secret = base_wallet.decrypt_secret(self.wallet_path, self.kek)
        self.claim(txid, ME, base_secret=secret)

    def step(self):
        self.worker.step()

    def done(self):
        return json.loads(self.node.storage.get_meta(gas_worker.DONE_KEY) or "{}")


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestTracker:
    def test_a_signed_request_is_tracked(self, w):
        txid = w.request()
        w.step()
        r = w.worker.tracker.requests[txid]
        assert (r.height, r.lock, r.req["dest"], r.req["network"]) == (1, gas_worker.MIN_SERVED_LOCK, DEST, "optimism")

    def test_a_request_with_a_lock_but_a_bad_memo_is_not_tracked(self, w):
        t = make_tx(ASKER, 0, 0, None, fee=1000, nonce_override=1, memo="[gas] a b",
                    outputs_override=[{"to": ESC, "amount": gas_worker.MIN_SERVED_LOCK}])
        w.mine([t])
        w.step()
        assert w.worker.tracker.requests == {}

    def test_the_window_follows_the_chain_not_the_signatures(self, w):
        txid = w.request()
        w.claim(txid, 3, bad_signature=True)             # well formed, signature wrong
        w.step()
        r = w.worker.tracker.requests[txid]
        assert r.first_claim == 2 and r.close == 3       # the chain counts it
        assert r.claims == []                            # but it cannot be ranked

    def test_claims_outside_the_window_are_ignored(self, w):
        txid = w.request()
        w.mine_until(1 + gaslock.CLAIM_WINDOW_BLOCKS)
        w.claim(txid, 3)                                 # one block too late
        w.step()
        assert w.worker.tracker.requests[txid].first_claim is None

    def test_a_claim_naming_another_request_is_ignored(self, w):
        txid = w.request()
        w.claim("ee" * 32, 3)
        w.step()
        assert w.worker.tracker.requests[txid].claims == []

    def test_old_requests_are_forgotten(self, w):
        txid = w.request()
        w.step()
        w.mine_until(60)
        w.step()
        assert txid not in w.worker.tracker.requests


class TestClaiming:
    def test_it_claims_a_request_it_will_serve(self, w):
        txid = w.request()
        w.step()
        assert w.io.claims == [txid]

    def test_it_claims_once(self, w):
        w.request()
        w.step()
        w.mine()
        w.step()
        assert len(w.io.claims) == 1

    def test_it_claims_a_request_that_arrived_before_it_started(self, tmp_path):
        w = World(tmp_path)
        txid = w.request()
        w.mine_until(3)
        w.step()                                         # first pass scans the backlog
        assert w.io.claims == [txid]

    def test_a_locked_node_does_nothing(self, w):
        w.node._kek = None
        w.request()
        w.step()
        assert w.io.claims == [] and w.io.paid == []

    def test_an_unfunded_wallet_is_the_off_switch_and_is_checked_once_per_block(self, w):
        w.io.base = 0
        calls = []
        real = w.io.base_balance
        w.io.base_balance = lambda: calls.append(1) or real()
        w.request(); w.request(sender=3)
        w.step(); w.step()
        assert w.io.claims == [] and len(calls) == 1
        w.mine(); w.step()
        assert len(calls) == 2

    def test_a_small_lock_is_not_worth_it(self, w):
        w.request(lock=gas_worker.MIN_SERVED_LOCK - 1)
        w.step()
        assert w.io.claims == []

    def test_it_will_not_help_with_its_own_request(self, w):
        w.request(sender=ME)
        w.step()
        assert w.io.claims == []

    def test_it_will_not_claim_without_the_funds(self, w):
        w.io.base = 10 ** 14                             # $0.25
        w.request()
        w.step()
        assert w.io.claims == []

    def test_it_will_not_claim_for_a_destination_that_is_fine(self, w):
        w.io.dest = TARGET
        w.request()
        w.step()
        assert w.io.claims == []

    def test_it_will_not_claim_a_network_it_does_not_know(self, w):
        w.request(net=SimpleNamespace(slug="dogechain"))
        w.step()
        assert w.worker.tracker.requests == {} and w.io.claims == []

    def test_a_failed_claim_is_tried_again_next_block(self, w):
        w.io.claim_ok = False
        w.request()
        w.step()
        w.io.claim_ok = True
        w.mine()
        w.step()
        assert len(w.io.claims) == 2

    def test_no_claim_once_the_window_is_closed(self, w):
        txid = w.request()
        w.claim(txid, 3)                                 # closes at block 3
        w.step()
        before = len(w.io.claims)
        w.mine()
        w.step()
        assert len(w.io.claims) == before


def settled_world(w, claimers=(ME,)):
    """A request with a claim from each of `claimers` (node indexes), all in
    one block, so the window closes right after it."""
    txid = w.request()
    w.claim_together(txid, claimers)
    return txid


class TestPaying:
    def _closed(self, w, claimers):
        txid = settled_world(w, claimers)
        w.step()
        close = w.worker.tracker.requests[txid].close
        w.mine_until(close)
        w.step()
        return txid, w.worker.tracker.requests[txid]

    def test_the_only_claimer_pays_once_the_window_closes(self, w):
        txid, r = self._closed(w, [ME])
        assert w.io.paid == []                           # the order is fixed by block `close`
        w.mine()
        w.step()
        assert w.io.paid == [("optimism", DEST, TARGET)]
        assert w.done()[txid]["state"] == "paid"

    def test_it_does_not_pay_twice(self, w):
        self._closed(w, [ME])
        w.mine(); w.step()
        w.mine(); w.step()
        w.mine(); w.step()
        assert len(w.io.paid) == 1

    def test_it_does_not_pay_twice_across_a_restart(self, w):
        self._closed(w, [ME])
        w.mine(); w.step()
        fresh = gas_worker.GasWorker(w.node, w.io)        # same storage, new process
        fresh.step()
        w.mine()
        fresh.step()
        assert len(w.io.paid) == 1

    def test_a_node_that_did_not_claim_never_pays(self, w):
        self._closed(w, [3, 4])
        for _ in range(10):
            w.mine(); w.step()
        assert w.io.paid == []

    @pytest.mark.parametrize("rank", [0, 1, 2])
    def test_a_claimer_waits_for_its_own_turn(self, w, monkeypatch, rank):
        txid = settled_world(w, [ME, 3, 4])
        w.step()
        r = w.worker.tracker.requests[txid]
        others = [address(3), address(4)]
        order = others[:rank] + [address(ME)] + others[rank:]
        monkeypatch.setattr(gas, "order_claimers", lambda *a: order)
        turn = gas.turn_start(r.close, rank)
        w.mine_until(turn - 1)
        w.step()
        assert w.io.paid == []                            # not its slot yet
        w.mine()
        w.step()
        assert w.io.paid == [("optimism", DEST, TARGET)]  # the first block of its slot

    def test_the_real_order_gives_each_node_a_slot(self, w):
        txid = settled_world(w, [ME, 3, 4])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close)
        order = gas_worker.claimers_in_order(r, w.chain[r.close]["hash"], lambda b: True)
        assert sorted(order) == sorted([address(ME), address(3), address(4)])
        rank = order.index(address(ME))
        w.mine_until(gas.turn_start(r.close, rank) - 1)
        w.step()
        assert w.io.paid == []
        w.mine(); w.step()
        assert len(w.io.paid) == 1

    def test_a_later_claimer_stands_down_when_the_destination_is_already_funded(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.dest = TARGET                                # someone got there first
        w.step()
        assert w.io.paid == [] and w.done()[txid]["state"] == "funded"

    def test_it_pays_only_the_shortfall(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.dest = 3 * 10 ** 14
        w.step()
        assert w.io.paid == [("optimism", DEST, 2 * 10 ** 14)]

    def test_a_failed_payment_is_recorded_not_retried_blindly(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.fail_pay = relay.RelayError("amount too low")
        w.step()
        assert w.done()[txid]["state"] == "failed" and "too low" in w.done()[txid]["error"]
        w.io.fail_pay = None
        w.mine(); w.step()
        assert w.io.paid == []                           # the next ranked node covers it

    def test_it_will_not_pay_without_the_funds(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.base = 10 ** 14
        w.step()
        assert w.io.paid == [] and w.done()[txid]["state"] == "unaffordable"

    def test_a_claimer_without_funds_is_not_ranked(self, w):
        txid = settled_world(w, [3, ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        theirs = evm.address_from_secret(w.base_keys[3])
        order = gas_worker.claimers_in_order(
            r, "x" * 64, lambda b: b != theirs)
        assert order == [address(ME)]

    def test_one_base_wallet_takes_one_place_however_many_lapse_addresses_name_it(self, w):
        txid = w.request()
        shared = os.urandom(32)
        w.mine([w._claim_tx(txid, 3, base_secret=shared), w._claim_tx(txid, 4, base_secret=shared),
                w._claim_tx(txid, 5)])
        w.step()
        r = w.worker.tracker.requests[txid]
        assert len(r.claims) == 3
        order = gas_worker.claimers_in_order(r, "c" * 64, lambda b: True)
        assert len(order) == 2 and address(5) in order
        assert address(3) in order and address(4) not in order       # the earlier claim keeps it

    def test_each_node_gets_the_same_order(self, w):
        txid = settled_world(w, [ME, 3, 4, 5])
        w.step()
        r = w.worker.tracker.requests[txid]
        a = gas_worker.claimers_in_order(r, "c" * 64, lambda b: True)
        r.claims.reverse()
        b = gas_worker.claimers_in_order(r, "c" * 64, lambda b: True)
        assert a == b and len(a) == gas.MAX_RANKS

    def test_a_reorg_starts_the_reading_over(self, w):
        txid = w.request()
        w.step()
        assert txid in w.worker.tracker.requests
        # the chain the worker read from is replaced by another one
        w.chain[1] = make_block(1, w.chain[0]["hash"], [], builder_index=8)
        w.step()
        assert txid not in w.worker.tracker.requests


class TestSanctions:
    def test_a_listed_destination_is_not_claimed(self, w):
        w.io.listed = {DEST}
        w.request()
        w.step()
        assert w.io.claims == []

    def test_it_will_not_claim_when_it_cannot_screen(self, w):
        w.io.screening_down = True
        w.request()
        w.step()
        assert w.io.claims == []

    def test_a_destination_listed_after_the_claim_is_not_paid(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.listed = {DEST}
        w.step()
        assert w.io.paid == [] and w.done()[txid]["state"] == "refused"

    def test_an_oracle_outage_at_payment_time_retries_instead_of_giving_up(self, w):
        txid = settled_world(w, [ME])
        w.step()
        r = w.worker.tracker.requests[txid]
        w.mine_until(r.close + 1)
        w.io.screening_down = True
        w.step()
        assert w.io.paid == [] and txid not in w.done()
        w.io.screening_down = False
        w.mine(); w.step()
        assert len(w.io.paid) == 1


class TestLiveIO:
    def test_only_evm_destinations_are_screened(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gas_io.sanctions, "is_sanctioned", lambda a: calls.append(a) or True)
        io = gas_worker.LiveIO(SimpleNamespace(settings=None))
        assert io.sanctioned(gas.NETWORKS["optimism"], DEST) is True
        assert io.sanctioned(gas.NETWORKS["solana"], "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK") is False
        assert calls == [DEST]

    def test_dest_balance_reads_solana_with_its_own_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(evm, "rpc", lambda url, m, p=None: calls.append((url, m, p)) or {"value": 7})
        node = SimpleNamespace(settings=settings_mod.Settings(Meta()), base_wallet_path="x")
        io = gas_worker.LiveIO(node)
        assert io.dest_balance(gas.NETWORKS["solana"], "addr") == 7
        assert calls == [(gas.NETWORKS["solana"].rpc, "getBalance", ["addr"])]

    def test_base_reads_use_the_public_base_endpoint(self, monkeypatch):
        seen = []
        monkeypatch.setattr(evm, "get_balance_wei", lambda url, a: seen.append(url) or 5)
        gas_worker.LiveIO(SimpleNamespace()).dest_balance(gas.NETWORKS["base"], "0x" + "1" * 40)
        assert seen == [evm.DEFAULT_BASE_RPC]
