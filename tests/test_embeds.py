"""The card a pasted link gets in Discord and similar apps: Open Graph tags
that say what was shared, built from the figures the page shows."""

import html
import re
from types import SimpleNamespace

import api
import embeds
import peerpool as peerpool_mod
import crypto
import tx as tx_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import make_tx
from tests.test_api import fresh


def _tags(page):
    """{property or name: content} for every meta tag in a page's head."""
    head = page.split("</head>", 1)[0]
    return {m.group(1): html.unescape(m.group(2))
            for m in re.finditer(r'<meta (?:property|name)="([^"]+)" content="([^"]*)"', head)}


def _node():
    """The thin test node, given the storage and info a few pages read."""
    node, cs = fresh()
    node.storage = SimpleNamespace(get_tx_height=lambda h: None,
                                   get_tx_heights_for_addr=lambda addr: [])
    node.get_info = lambda: {
        "height": len(node.view.chain) - 1, "tip_hash": "ab" * 32, "mempool_size": 3,
        "address": node.addr, "peer_count": 1, "total_minted": 5 * TICKS_PER_LAPSE,
        "burned": 0, "circulating": 5 * TICKS_PER_LAPSE, "can_mint": 0,
        "block_reward": 0, "block_time_ratio": None, "network_age_seconds": 90061,
        "status": "ok"}
    return node, cs


def _page(path, public=True, public_url=None):
    node, cs = _node()
    make = api.create_app if public else api.create_private_app
    app = make(node, peerpool_mod.PeerPool(), public_url=public_url)
    resp = app.test_client().get(path, base_url="http://node.test")
    return resp.get_data(as_text=True), node, cs


class TestWording:
    def test_a_post_cannot_smuggle_lines_or_length_into_a_card(self):
        card = embeds.board(1, [{"from": "a.b.c.d", "nick": "eve", "reply_ref": "",
                                 "text": "hi\nthere\r\n" + "x" * 500}])
        assert "\r" not in card["description"]
        assert card["description"].count("\n") == 1
        assert len(card["description"]) <= 260
        assert card["description"].endswith("…")

    def test_the_board_shows_the_newest_top_level_post_not_a_reply(self):
        rows = [{"from": "w.x.y.z", "nick": None, "reply_ref": "abc123", "text": "a reply"},
                {"from": "a.b.c.d.e", "nick": "ann", "reply_ref": "", "text": "the root"}]
        assert "ann: the root" in embeds.board(2, rows)["description"]

    def test_addresses_are_shortened_to_three_words(self):
        assert embeds.short_addr("one.two.three.four.five") == "one.two.three…"
        assert embeds.short_addr("one.two") == "one.two"

    def test_an_empty_benchmark_and_odds_still_make_a_card(self):
        assert embeds.benchmark([], 0, "1d")["title"] == "VDF benchmark"
        assert embeds.odds(None)["title"] == "Block race odds"
        assert embeds.mempool([])["description"] == "No pending transactions."

    def test_the_mempool_card_counts_and_totals_what_is_pending(self):
        rows = [{"tx": {"outputs": [{"amount": 3 * TICKS_PER_LAPSE}]}},
                {"tx": {"outputs": [{"amount": 2 * TICKS_PER_LAPSE}, {"amount": TICKS_PER_LAPSE}]}}]
        card = embeds.mempool(rows)
        assert "2 pending transactions" in card["description"]
        assert "6.00 LAPSE moving" in card["description"]

    def test_the_odds_card_says_so_when_mining_is_off(self):
        race = {"odds_pct": 0.0, "field_builders": 4, "median": 196.0}
        assert "mining is off" in embeds.odds(race, mining=False)["description"]
        assert "0.0%" in embeds.odds(race, mining=True)["description"]

    def test_a_board_post_card_shows_the_post_and_not_the_burn_transfer(self):
        t = {"from": "a.b.c.d", "fee": 5, "memo": "[board] [r:abc123]hello there",
             "outputs": [{"amount": 1, "to": "burn.burn.burn.burn"}]}
        card = embeds.transaction("f" * 64, t, "Block 7")
        assert card["title"] == "Board post"
        assert "a.b.c…: hello there" in card["description"]

    def test_a_tiny_amount_is_shown_in_ticks_not_as_zero(self):
        t = {"from": "a.b.c.d", "fee": 5, "memo": "", "outputs": [{"amount": 202, "to": "x.y.z"}]}
        assert "202 ticks from" in embeds.transaction("f" * 64, t, "Block 7")["description"]

    def test_the_blocks_card_lists_the_newest_first(self):
        chain = [{"height": h, "transactions": [], "timestamp": 1000 + h} for h in range(5)]
        lines = embeds.blocks(chain, now=2000)["description"].split("\n")
        assert [line.split(" ")[0] for line in lines] == ["#4", "#3", "#2"]

    def test_benchmark_names_the_fastest_machine(self):
        card = embeds.benchmark([{"name": "Test CPU", "ips": 1234.4}], 12, "20m")
        assert "Test CPU at 1,234 iterations per second" in card["description"]


class TestPages:
    def test_every_page_carries_a_card_with_the_icon(self):
        for path in ("/", "/blocks", "/mempool", "/network", "/whitepaper", "/fees", "/balance"):
            meta = _tags(_page(path)[0])
            assert meta["og:title"] and meta["og:description"], path
            assert meta["og:image"] == "http://node.test/lapsecoin-512x512.png", path
            assert meta["twitter:card"] == "summary"
            assert meta["og:url"] == "http://node.test" + path, path

    def test_the_card_uses_the_advertised_https_address_when_there_is_one(self):
        page, _, _ = _page("/blocks", public_url="https://node.example")
        meta = _tags(page)
        assert meta["og:image"] == "https://node.example/lapsecoin-512x512.png"
        assert meta["og:url"] == "https://node.example/blocks"

    def test_the_dashboard_says_where_the_chain_is(self):
        meta = _tags(_page("/")[0])
        assert meta["og:title"].startswith("LapseCoin at block ")
        assert "circulating" in meta["og:description"]

    def test_a_block_card_names_the_block(self):
        meta = _tags(_page("/block/0")[0])
        assert meta["og:title"] == "Block 0"
        assert "Genesis block" in meta["og:description"]

    def test_a_transaction_card_shows_amount_and_place(self):
        node, cs = _node()
        t = make_tx(0, 1, 5 * TICKS_PER_LAPSE, cs.state, fee=10)
        node.mempool.add(t)
        app = api.create_app(node, peerpool_mod.PeerPool())
        page = app.test_client().get("/tx/" + tx_mod.tx_hash(t)).get_data(as_text=True)
        meta = _tags(page)
        assert meta["og:title"].startswith("Transaction ")
        assert "5.00" in meta["og:description"] and "Mempool (unconfirmed)" in meta["og:description"]

    def test_a_balance_lookup_card_names_the_address(self):
        node, cs = _node()
        addr = crypto.burn_address()
        app = api.create_app(node, peerpool_mod.PeerPool())
        meta = _tags(app.test_client().get("/balance?addr=" + addr).get_data(as_text=True))
        assert meta["og:title"] == embeds.short_addr(addr)
        assert "LAPSE" in meta["og:description"]

    def test_a_hostile_nickname_or_post_is_escaped_in_the_tag(self):
        card = embeds.board(1, [{"from": "a.b.c", "nick": None, "reply_ref": "",
                                 "text": '"><script>alert(1)</script>'}])
        rendered = api.make_flask_app(__name__).jinja_env.from_string(
            '<meta content="{{ c.description }}">').render(c=card)
        assert "<script>" not in rendered and "&lt;script&gt;" in rendered
