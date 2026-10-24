"""The balance curve behind the lookup page's Balance and % of circulation views."""

import state as state_mod
import crypto
from local_reader import LocalReader
from tests.fixtures import address
from tests.wallet_app import _TxIndex

A, B = address(0), address(1)
UNIT = 10 ** 8


class _CS:
    pass


class _View:
    pass


def _node(blocks):
    """A node over a chain of (builder, transactions) pairs, ledger replayed
    the way chainstate does it. Genesis has no builder."""
    st, chain = state_mod.State(), []
    for h, (builder, txs) in enumerate([(None, [])] + blocks):
        for t in txs:
            st.debit(t["from"], sum(o["amount"] for o in t["outputs"]) + t["fee"])
            for o in t["outputs"]:
                st.credit(o["to"], o["amount"])
        if builder:
            fees = sum(t["fee"] for t in txs)
            if fees:
                st.credit(builder, fees)
            st.apply_reward_distribution([(builder, st.compute_block_reward())])
        st.height = h
        chain.append({"height": h, "hash": f"{h:064x}", "builder": builder, "transactions": txs})
    cs, view = _CS(), _View()
    cs.chain, cs.state = chain, st
    view.chain, view.state = chain, st

    class Node:
        pass
    node = Node()
    node.view, node.storage = view, _TxIndex(cs)
    return node


def test_unknown_address_has_no_curve():
    s = LocalReader(_node([(A, [])] * 3)).address_series(B)
    assert s["points"] == [] and s["moves"] == [] and s["balance"] == 0


def test_curve_ends_on_the_true_balance_and_steps_on_each_move():
    send = {"from": A, "nonce": 1, "fee": 100, "outputs": [{"to": B, "amount": 2 * UNIT}]}
    node = _node([(A, []), (A, []), (B, [send]), (A, [])])
    s = LocalReader(node).address_series(B)
    assert s["balance"] == node.view.state.get_balance(B)
    heights = [m[0] for m in s["moves"]]
    assert heights == sorted(heights) and 3 in heights
    assert s["moves"][-1][2] == s["balance"]
    assert s["points"][-1][0] == s["tip"] == 4
    assert all(p[1] <= p[2] for p in s["points"])  # never more than circulates


def test_a_first_receipt_is_a_step_up_from_zero():
    node = _node([(A, []), (A, []), (B, []), (A, [])])
    s = LocalReader(node).address_series(B)
    assert s["moves"][0][0] == 3
    assert s["points"][0][:2] == [2, 0]


def test_genesis_mints_nothing():
    from local_reader import _reward_series
    assert _reward_series.reward_at(0) == 0 and _reward_series.reward_at(1) > 0


def test_sending_to_yourself_only_costs_the_fee():
    t = {"from": A, "nonce": 1, "fee": 100, "outputs": [{"to": A, "amount": UNIT}]}
    node = _node([(A, []), (B, [t]), (B, [])])
    s = LocalReader(node).address_series(A)
    move = next(m for m in s["moves"] if m[0] == 2)
    assert move[1] == -100


def test_burn_address_is_not_circulating():
    burn = crypto.burn_address()
    t = {"from": A, "nonce": 1, "fee": 0, "outputs": [{"to": burn, "amount": UNIT}]}
    node = _node([(A, []), (A, [t])])
    s = LocalReader(node).address_series(A)
    tip = s["points"][-1]
    assert tip[2] == node.view.state.total_minted - UNIT


class TestPages:
    def _app(self):
        import api
        import peerpool as peerpool_mod
        from tests.test_embeds import _node as embeds_node
        node, cs = embeds_node()
        return node, cs, api.create_app(node, peerpool_mod.PeerPool())

    def test_lookup_offers_history_and_share_when_there_is_one_change(self):
        _node_, _cs, app = self._app()
        html = app.test_client().get("/balance?addr=" + A + "&view=share").get_data(as_text=True)
        assert 'data-tab="history"' in html and 'data-tab="share"' in html
        assert 'data-tab="balance"' not in html  # one change is only a flat line
        assert 'id="addr-series"' in html and 'data-view="share"' in html

    def test_history_is_the_default_view(self):
        _node_, _cs, app = self._app()
        for q in ("", "&view=nope"):
            html = app.test_client().get("/balance?addr=" + A + q).get_data(as_text=True)
            assert 'data-view="history"' in html

    def test_an_address_with_no_activity_has_no_curve_tabs(self):
        _node_, _cs, app = self._app()
        html = app.test_client().get("/balance?addr=" + crypto.burn_address() + "&view=balance").get_data(as_text=True)
        assert 'data-tab="balance"' not in html and 'aria-disabled="true"' in html
        assert 'data-view="history"' in html

    def test_series_api_rejects_a_bad_address(self):
        _node_, _cs, app = self._app()
        assert app.test_client().get("/api/address/not-an-address/series").status_code == 400

    def test_addresses_on_chain_pages_link_to_the_lookup(self):
        import tx as tx_mod
        from tests.fixtures import make_tx
        from params import TICKS_PER_LAPSE
        node, cs, app = self._app()
        t = make_tx(0, 1, 5 * TICKS_PER_LAPSE, cs.state, fee=10)
        node.mempool.add(t)
        page = app.test_client().get("/tx/" + tx_mod.tx_hash(t)).get_data(as_text=True)
        assert f'href="/balance?addr={t["from"]}"' in page
        assert f'href="/balance?addr={t["outputs"][0]["to"]}"' in page
        pool = app.test_client().get("/mempool").get_data(as_text=True)
        assert f'href="/balance?addr={t["from"]}"' in pool

    def test_a_flat_balance_request_falls_back_to_the_share_curve(self):
        _node_, _cs, app = self._app()
        html = app.test_client().get("/balance?addr=" + A + "&view=balance").get_data(as_text=True)
        assert 'data-view="share"' in html


def test_holder_stats_leave_out_the_burn_address():
    node = _node([(A, []), (A, []), (B, [])])
    burn = crypto.burn_address()
    node.view.state.credit(burn, 10_000 * UNIT)
    bal = node.view.state.all_balances()
    others = sorted(b for a, b in bal.items() if a != burn)
    stats = LocalReader(node).holder_stats()
    assert stats["holders"] == len(others)
    assert stats["mean"] == sum(others) // len(others)
    mid = len(others) // 2
    expect = others[mid] if len(others) % 2 else (others[mid - 1] + others[mid]) // 2
    assert stats["median"] == expect
    assert stats["mean"] < 10_000 * UNIT


def test_holder_stats_are_reused_until_the_tip_moves():
    node = _node([(A, []), (B, [])])
    reader = LocalReader(node)
    first = reader.holder_stats()
    node.view.state.credit(A, UNIT)
    assert reader.holder_stats() is first          # same tip, not recomputed
    node.view.chain.append({"height": 3, "hash": f"{3:064x}", "builder": None,
                            "transactions": []})
    assert reader.holder_stats() is not first


def test_series_carries_the_holder_stats():
    node = _node([(A, []), (B, [])])
    assert LocalReader(node).address_series(B)["holders"]["holders"] == 2
