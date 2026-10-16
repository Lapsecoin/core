"""A pending transaction the chain can no longer pay for does not stay in the
pool. Block building assumes everything in it is payable and does not check
funds again, so one that is not makes every block built from the pool invalid.
Two ordinary things cause it: a reorg that removes the money a pending
transaction was spending, and a confirmed transaction that conflicts with the
sender's earlier one (a double-spend attempt, or two wallets racing)."""

import block as block_mod
import state as state_mod
import tx as tx_mod
from chainstate import ChainState
from mempool import Mempool
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_block, make_tx
from tests.test_node import node_env  # noqa: F401  (pytest fixture)

L = TICKS_PER_LAPSE


def funded(*senders):
    st = state_mod.State()
    for s in senders:
        st.credit(address(s), 100 * L)
        st.total_minted += 100 * L
    return st


def candidate_is_valid(txs, state):
    tip = ChainState.from_genesis().tip
    blk = block_mod.assemble(tip, txs, address(1), 1)
    ok, why = block_mod._apply_transactions(blk, state.snapshot())
    return ok, why


def test_a_follow_up_to_a_conflicting_tx_is_dropped():
    st = funded(0)
    tx1 = make_tx(0, 5, 10 * L, st)
    st.apply_tx(tx1)
    tx2 = make_tx(0, 6, 80 * L, st)               # payable only if tx1 was the small one
    mp = Mempool()
    mp.add(tx1)
    mp.add(tx2)
    confirmed = funded(0)
    big = make_tx(0, 7, 95 * L, confirmed)        # a different nonce-1 tx is what got confirmed
    confirmed.apply_tx(big)
    mp.prune_stale(confirmed)
    assert mp.size() == 0
    assert candidate_is_valid(mp.all_txs(), confirmed)[0]


def test_the_block_that_would_have_been_built_is_the_proof_it_mattered():
    st = funded(0)
    tx1 = make_tx(0, 5, 10 * L, st)
    st.apply_tx(tx1)
    tx2 = make_tx(0, 6, 80 * L, st)
    confirmed = funded(0)
    confirmed.apply_tx(make_tx(0, 7, 95 * L, confirmed))
    assert candidate_is_valid([tx2], confirmed)[0] is False


def test_everything_after_an_unpayable_tx_from_that_sender_goes_with_it():
    st = funded(0)
    txs = []
    for amount in (10, 80, 1, 1):
        t = make_tx(0, 5, amount * L, st)
        st.apply_tx(t)
        txs.append(t)
    mp = Mempool()
    for t in txs:
        mp.add(t)
    confirmed = funded(0)
    confirmed.apply_tx(make_tx(0, 7, 95 * L, confirmed))
    mp.prune_stale(confirmed)
    assert mp.size() == 0


def test_payable_txs_and_other_senders_are_left_alone():
    st = funded(0, 1)
    a = make_tx(0, 5, 10 * L, st)
    b = make_tx(1, 5, 10 * L, st)
    mp = Mempool()
    mp.add(a)
    mp.add(b)
    assert mp.prune_stale(funded(0, 1)) == []
    assert mp.size() == 2


def test_a_run_of_payable_txs_from_one_sender_all_stay():
    st = funded(0)
    mp = Mempool()
    for _ in range(4):
        t = make_tx(0, 5, 10 * L, st)
        st.apply_tx(t)
        mp.add(t)
    assert mp.prune_stale(funded(0)) == [] and mp.size() == 4


def test_a_reorg_that_removes_the_money_a_pending_tx_was_spending_drops_it(node_env):
    node, *_ = node_env
    g = node.cs.chain[0]
    old_state = funded(0)
    pay = make_tx(0, 5, 10 * L, old_state)         # in the block that is lost
    old_state.apply_tx(pay)
    spend = make_tx(5, 6, 5 * L, old_state)        # pending: spends what pay gave address 5
    node.mempool.add(spend)
    node.cs = ChainState.from_genesis()
    node.cs.chain.append(make_block(1, g["hash"], [pay]))
    new_state = funded(0)
    new_state.set_nonce(address(0), 1)             # the winning chain used nonce 1 for something else
    new_chain = [g, make_block(1, g["hash"], [], builder_index=1)]
    node._reorg_mempool(fork_point=1, old_chain=node.cs.chain, new_chain=new_chain, new_state=new_state)
    assert node.mempool.get(tx_mod.tx_hash(spend)) is None
    assert candidate_is_valid(node.mempool.all_txs(), new_state)[0]


def test_a_new_submission_is_not_broken_by_an_unpayable_tx_already_in_the_pool():
    st = funded(0)
    t = make_tx(0, 5, 50 * L, st)
    mp = Mempool()
    mp.add(t)
    poor = state_mod.State()
    poor.credit(address(0), 1 * L)
    mp.probe_state_for(address(0), poor)           # used to raise


def test_rechecking_skips_the_signature_and_only_that():
    st = funded(0)
    t = make_tx(0, 5, 10 * L, st)
    forged = dict(t, signature="00" * 8)
    assert tx_mod.validate(forged, funded(0))[0] is False
    assert tx_mod.validate(forged, funded(0), check_signature=False)[0] is True
    assert tx_mod.validate(t, state_mod.State(), check_signature=False)[0] is False   # still needs the money
