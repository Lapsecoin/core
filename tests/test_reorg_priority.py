"""Transactions from blocks a node had and then lost in a reorg come back with
priority: they are put back whatever the pool looks like, they go into blocks
ahead of fee order, a new arrival cannot push them out, and a restart keeps
that."""

import time

import pytest

import block as block_mod
import mempool as mempool_mod
import mempool_store
import state as state_mod
import tx as tx_mod
from chainstate import ChainState
from mempool import Mempool
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_block, make_tx
from tests.test_node import node_env  # noqa: F401  (pytest fixture)


def funded(*senders):
    st = state_mod.State()
    for s in senders:
        st.credit(address(s), 100 * TICKS_PER_LAPSE)
        st.total_minted += 100 * TICKS_PER_LAPSE
    return st


def tx(sender, fee=0, state=None):
    return make_tx(sender, 9, TICKS_PER_LAPSE, state or funded(sender), fee=fee)


def small_pool(monkeypatch, holds):
    """A pool that holds `holds` of these transactions and no more."""
    size = tx_mod.tx_size(tx(0, fee=1000))
    monkeypatch.setattr(mempool_mod, "MEMPOOL_MAX_BYTES", size * holds + size // 2)
    return Mempool()


# The pool
def test_a_recovered_tx_is_marked_and_listed():
    mp = Mempool()
    a, b = tx(0), tx(1)
    mp.add(a, recovered=True)
    mp.add(b)
    assert mp.recovered_hashes() == {tx_mod.tx_hash(a)}


def test_a_full_pool_makes_room_for_a_recovered_tx_even_at_a_lower_fee(monkeypatch):
    mp = small_pool(monkeypatch, 2)
    mp.add(tx(0, fee=5000))
    mp.add(tx(1, fee=6000))
    low = tx(2, fee=1)
    assert mp.add(low)[0] is False                      # an ordinary arrival cannot
    assert mp.add(low, recovered=True)[0] is True       # one that came back can
    assert mp.get(tx_mod.tx_hash(low)) is not None and mp.size() == 2


def test_a_new_arrival_never_pushes_out_a_recovered_tx(monkeypatch):
    mp = small_pool(monkeypatch, 1)
    kept = tx(0, fee=1)
    mp.add(kept, recovered=True)
    assert mp.add(tx(1, fee=10**6))[0] is False
    assert mp.get(tx_mod.tx_hash(kept)) is not None


# Block building
def test_a_recovered_tx_goes_in_ahead_of_a_higher_fee(monkeypatch):
    rich, back = tx(0, fee=10**6), tx(1, fee=1)
    monkeypatch.setattr(block_mod, "BLOCK_SIZE_LIMIT",
                        block_mod.block_size(block_mod.create(1, "0" * 64, [], address(0), 1, 0))
                        + tx_mod.tx_size_in_block(rich, 0) + 5)
    g = ChainState.from_genesis().tip
    plain = block_mod.assemble(g, [rich, back], address(0), 1)["transactions"]
    first = block_mod.assemble(g, [rich, back], address(0), 1,
                               prefer={tx_mod.tx_hash(back)})["transactions"]
    assert plain == [rich] and first == [back]


def test_without_a_preference_nothing_changes():
    a, b = tx(0, fee=10), tx(1, fee=1000)
    g = ChainState.from_genesis().tip
    out = block_mod.assemble(g, [a, b], address(0), 1)["transactions"]
    assert out[0] == b


# Putting them back after a reorg
def _lost_branch(node, txs, stamp):
    g = node.cs.chain[0]
    old = make_block(1, g["hash"], txs)
    old["timestamp"] = stamp
    node.cs = ChainState.from_genesis()
    node.cs.chain.append(old)
    return [g, make_block(1, g["hash"], [], builder_index=1)]


def test_a_reorg_puts_them_back_with_priority_and_their_age(node_env):
    node, *_ = node_env
    st = funded(0)
    t = make_tx(0, 1, TICKS_PER_LAPSE, st)
    stamp = time.time() - 600
    new_chain = _lost_branch(node, [t], stamp)
    node._reorg_mempool(fork_point=1, old_chain=node.cs.chain, new_chain=new_chain, new_state=funded(0))
    h = tx_mod.tx_hash(t)
    assert h in node.mempool.recovered_hashes()
    assert node.mempool.entered_at(h) == pytest.approx(stamp, abs=1)


def test_a_very_old_block_does_not_bring_a_tx_back_due_to_expire(node_env):
    node, *_ = node_env
    t = make_tx(0, 1, TICKS_PER_LAPSE, funded(0))
    new_chain = _lost_branch(node, [t], time.time() - 10 * mempool_mod.MEMPOOL_TTL_SECONDS)
    node._reorg_mempool(fork_point=1, old_chain=node.cs.chain, new_chain=new_chain, new_state=funded(0))
    assert node.mempool.prune_stale(funded(0)) == []


def test_one_tx_that_cannot_come_back_does_not_stop_the_rest(node_env, monkeypatch):
    node, *_ = node_env
    st = funded(0, 1)
    a, b = make_tx(0, 2, TICKS_PER_LAPSE, st), make_tx(1, 2, TICKS_PER_LAPSE, st)
    new_chain = _lost_branch(node, [a, b], time.time())
    real = tx_mod.validate

    def flaky(t, s, *args, **kw):
        if t is a:
            raise RuntimeError("malformed")
        return real(t, s, *args, **kw)

    monkeypatch.setattr(tx_mod, "validate", flaky)
    node._reorg_mempool(fork_point=1, old_chain=node.cs.chain, new_chain=new_chain, new_state=funded(0, 1))
    assert node.mempool.get(tx_mod.tx_hash(b)) is not None


def test_a_fork_we_rejected_gets_no_priority(node_env):
    node, *_ = node_env
    t = make_tx(0, 1, TICKS_PER_LAPSE, funded(0))
    node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
    node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
    tail = [make_block(1, node.cs.chain[0]["hash"], [t], builder_index=1)]
    node._salvage_fork_txs(1, tail)
    assert node.mempool.get(tx_mod.tx_hash(t)) is not None
    assert node.mempool.recovered_hashes() == set()


# A restart keeps it
def test_the_priority_survives_a_restart(tmp_path):
    path = str(tmp_path / "pool.json")
    a, b = tx(0), tx(1)
    mempool_store.save(path, [(a, 1.0), (b, 2.0)], {tx_mod.tx_hash(a)})
    back = mempool_store.load(path, with_recovered=True)
    assert [(tx_mod.tx_hash(t), r) for t, _, r in back] == [(tx_mod.tx_hash(a), True), (tx_mod.tx_hash(b), False)]
    assert [e for _, e in mempool_store.load(path)] == [1.0, 2.0]
