"""The minimum fee a node takes a transaction into its pool for. Local relay
policy, not a rule of the chain: a block that carries a cheaper transaction
is still a valid block."""

import time

import mempool_store
import state as state_mod
import tx as tx_mod
import wallet_ui
from chainstate import ChainState
from params import MIN_RELAY_FEE_RATE, TICKS_PER_LAPSE
from tests.fixtures import address, make_block, make_tx
from tests.test_node import node_env  # noqa: F401  (pytest fixture)

L = TICKS_PER_LAPSE


def fund(node, *senders):
    for s in senders:
        node.cs.state.credit(address(s), 100 * L)
        node.cs.state.total_minted += 100 * L


def pool_state():
    st = state_mod.State()
    st.credit(address(0), 100 * L)
    st.total_minted += 100 * L
    return st


def test_a_free_tx_is_refused(node_env):
    node, *_ = node_env
    fund(node, 0)
    ok, why = node.submit_tx(make_tx(0, 1, L, node.cs.state, fee=0))
    assert ok is False and "relay minimum" in why and node.mempool.size() == 0


def test_a_tx_one_tick_under_the_floor_is_refused_and_the_floor_itself_is_taken(node_env):
    node, *_ = node_env
    fund(node, 0)
    floor = tx_mod.relay_fee_floor(make_tx(0, 1, L, node.cs.state))
    assert node.submit_tx(make_tx(0, 1, L, node.cs.state, fee=floor - 1))[0] is False
    assert node.submit_tx(make_tx(0, 1, L, node.cs.state, fee=floor))[0] is True


def test_what_the_wallet_pays_at_the_floor_rate_is_accepted(node_env):
    """The wallet sizes a draft with fee 0 and then signs with the real fee,
    which is a few digits longer. Measured on the final size, a tx paying
    exactly the floor would fall short by the length of its own fee."""
    acct = {"nonce": 1, "fees": {"next_block": MIN_RELAY_FEE_RATE}}
    outputs = [{"to": address(1), "amount": L}]
    fee = wallet_ui.auto_fee(acct, address(0), make_tx(0, 1, L, pool_state()).get("pubkey"), outputs)
    t = make_tx(0, 1, L, pool_state(), fee=fee)
    assert tx_mod.fee_rate(t) < MIN_RELAY_FEE_RATE          # short on the final size...
    assert tx_mod.check_relay_fee(t) == (True, None)        # ...and still fine, as intended


def test_a_peers_free_tx_is_refused_too(node_env):
    node, *_ = node_env
    fund(node, 0)
    ok, why = node._validate_for_mempool(make_tx(0, 1, L, node.cs.state, fee=0))
    assert ok is False and "relay minimum" in why


def test_a_block_with_a_cheaper_tx_is_still_valid():
    import block as block_mod
    t = make_tx(0, 1, L, pool_state(), fee=0)
    blk = make_block(1, ChainState.from_genesis().tip["hash"], [t])
    assert block_mod._apply_transactions(blk, pool_state())[0] is True


def test_a_free_tx_from_a_lost_block_is_not_put_back(node_env):
    node, *_ = node_env
    g = node.cs.chain[0]
    free = make_tx(0, 1, L, pool_state(), fee=0)
    paid = make_tx(2, 1, L, pool_state(), fee="floor")
    node.cs = ChainState.from_genesis()
    node.cs.chain.append(make_block(1, g["hash"], [free, paid]))
    st = state_mod.State()
    for s in (0, 2):
        st.credit(address(s), 100 * L)
        st.total_minted += 100 * L
    node._reorg_mempool(fork_point=1, old_chain=node.cs.chain,
                        new_chain=[g, make_block(1, g["hash"], [], builder_index=1)], new_state=st)
    assert node.mempool.get(tx_mod.tx_hash(free)) is None
    assert node.mempool.get(tx_mod.tx_hash(paid)) is not None


def test_a_free_tx_in_an_old_snapshot_is_not_restored(node_env, tmp_path):
    node, *_ = node_env
    fund(node, 0, 2)
    free = make_tx(0, 1, L, node.cs.state, fee=0)
    paid = make_tx(2, 1, L, node.cs.state, fee="floor")
    mempool_store.save(node._mempool_file, [(free, time.time()), (paid, time.time())])
    node._load_mempool()
    assert node.mempool.get(tx_mod.tx_hash(free)) is None
    assert node.mempool.get(tx_mod.tx_hash(paid)) is not None
