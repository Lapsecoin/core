"""
Unit tests for api.py's pure helper functions (no Flask app, no HTTP).

Covers: fee_estimate (the send UI's fee-market summary).
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import mempool as mempool_mod
import peerpool as peerpool_mod
import settings as settings_mod
from chainstate import ChainState
from node import NodeView
import params
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_tx, seed_balance


class _MemoryMeta:
    """Storage stand-in for Settings: the only two methods it uses."""

    def __init__(self):
        self._meta = {}

    def get_meta(self, key, default=None):
        return self._meta.get(key, default)

    def set_meta(self, key, value):
        self._meta[key] = str(value)


class _FakeNode:
    """Just enough of Node's public surface for the routes under test:
    .mempool, .view (chain/tip), .addr, the liveness notes the peers and
    send pages read, and the settings the private settings page reads and
    writes."""

    def __init__(self, cs, addr=None, alive=()):
        self.mempool = mempool_mod.Mempool()
        self.view = NodeView(cs)
        self.addr = addr or address(0)
        self.settings = settings_mod.Settings(_MemoryMeta())
        self._alive = set(alive)

    def active_addresses(self, window_seconds):
        return set(self._alive)


def fresh():
    cs = ChainState.from_genesis()
    seed_balance(cs.state, 0, 1000.0)
    return _FakeNode(cs), cs


class TestFeeEstimate:
    def test_empty_mempool_still_suggests_the_relay_floor(self):
        node, _ = fresh()
        fees = api.fee_estimate(node)
        assert fees == {"pending": 0, "min": 0, "median": 0, "max": 0,
                        "next_block": params.MIN_RELAY_FEE_RATE}

    def test_reports_pending_count_and_rates(self):
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=10)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["pending"] == 1
        # min/median/max should all equal the single tx's own fee-per-byte
        assert fees["min"] == fees["max"] == fees["median"]
        assert fees["min"] > 0

    def test_next_block_is_just_the_relay_floor_when_mempool_below_capacity(self):
        """A mempool that easily fits in one block needs nothing beyond the
        relay-policy floor to clear the next block."""
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=0)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["next_block"] == params.MIN_RELAY_FEE_RATE

    def test_next_block_reflects_the_real_cutoff_when_block_is_full(self):
        """When the mempool overflows one block, next_block must match
        whatever block.assemble() itself would actually require, this
        reuses assemble() directly rather than reimplementing its packing
        logic, so the two can never drift apart."""
        import block as block_mod
        from unittest import mock

        node, cs = fresh()
        seed_balance(cs.state, 0, 100_000.0)
        txs = []
        s = cs.state
        for i in range(20):
            t = make_tx(0, 1, 1, s, fee=i)
            s.apply_tx(t)
            node.mempool.add(t)
            txs.append(t)

        # Force a tiny block size so the mempool clearly overflows one block.
        skeleton_size = block_mod.block_size(block_mod.create(
            height=1, previous_hash=cs.tip["hash"], transactions=[],
            builder=address(0), vdf_iterations=block_mod.VDF_ITERATIONS))
        one_tx_size = block_mod.tx_mod.tx_size_in_block(txs[0], position=0)
        tiny_limit = skeleton_size + one_tx_size * 3  # room for only a few

        with mock.patch("block.BLOCK_SIZE_LIMIT", tiny_limit):
            fees = api.fee_estimate(node)
            iterations = block_mod.get_vdf_iterations(node.view.chain)
            candidate = block_mod.assemble(node.view.tip, node.mempool.all_txs(),
                                            address(0), iterations)

        assert len(candidate["transactions"]) < len(txs)
        expected = min(t.get("fee", 0) / max(block_mod.tx_mod.tx_size(t), 1)
                        for t in candidate["transactions"])
        assert fees["next_block"] == expected


class TestFmtDuration:
    """Network age is shown as two units at most: a glanceable span, not
    seconds of precision on something measured in days."""

    def test_the_units_it_picks(self):
        assert api.fmt_duration(0) == "just now"
        assert api.fmt_duration(59) == "just now"
        assert api.fmt_duration(60) == "1m"
        assert api.fmt_duration(3599) == "59m"
        assert api.fmt_duration(3661) == "1h 1m"
        assert api.fmt_duration(90061) == "1d 1h"
        assert api.fmt_duration(86400 * 370) == "1y 5d"

    def test_it_never_shows_more_than_two(self):
        # 1y 35d 6h 5m would be four; the tail is noise at that scale.
        assert api.fmt_duration(86400 * 400 + 3600 * 6 + 305) == "1y 35d"

    def test_a_missing_or_negative_span_does_not_throw(self):
        # A clock that has gone backwards relative to genesis is not a
        # reason for the dashboard to 500.
        assert api.fmt_duration(None) == "just now"
        assert api.fmt_duration(-5) == "just now"


class TestDashboardTxPaging:
    """Recent transactions page like every other listing on the site.

    The walk is backwards from the tip and stops, so a later page costs
    the same as the first: reaching page 5 touches 5 pages' worth of
    transactions, never the whole chain.
    """

    class _DashNode:
        def __init__(self, tx_count):
            self.addr = address(0)
            # Two transactions per block, so paging has to cross block
            # boundaries rather than lining up with them.
            chain, n = [{"height": 0, "transactions": []}], 0
            while n < tx_count:
                txs = []
                for _ in range(min(2, tx_count - n)):
                    n += 1
                    txs.append({"from": address(1), "nonce": n, "fee": 0,
                                "outputs": [{"to": address(2),
                                             # whole LAPSE, so the rendered
                                             # amount reads back as the index
                                             "amount": n * TICKS_PER_LAPSE}]})
                chain.append({"height": len(chain), "transactions": txs})
            self.view = SimpleNamespace(chain=chain)

        def get_info(self):
            return {"height": len(self.view.chain) - 1, "tip_hash": "ab" * 32,
                    "mempool_size": 0, "address": self.addr, "peer_count": 0,
                    "total_minted": 0, "burned": 0, "circulating": 0,
                    "can_mint": 0, "block_reward": 0,
                    "block_time_ratio": None, "network_age_seconds": 90061,
                    "status": "ok"}

    def _client(self, tx_count):
        node = self._DashNode(tx_count)
        return api.create_private_app(node, peerpool_mod.PeerPool()).test_client()

    def _amounts(self, html):
        """The amount column, which is the tx's index, so a page's contents
        are identifiable without matching on hashes."""
        import re
        return [int(float(m.replace(",", "")))
                for m in re.findall(r'data-label="Amount">([\d,.]+) LAPSE', html)]

    def test_the_first_page_holds_the_newest(self):
        html = self._client(20).get("/").get_data(as_text=True)
        assert self._amounts(html) == [20, 19, 18, 17, 16, 15]

    def test_the_second_page_continues_where_it_left_off(self):
        html = self._client(20).get("/?tx_page=2").get_data(as_text=True)
        assert self._amounts(html) == [14, 13, 12, 11, 10, 9]

    def test_the_last_page_holds_the_remainder(self):
        html = self._client(20).get("/?tx_page=4").get_data(as_text=True)
        assert self._amounts(html) == [2, 1]

    def test_a_page_past_the_end_clamps_to_the_last(self):
        html = self._client(20).get("/?tx_page=99").get_data(as_text=True)
        assert self._amounts(html) == [2, 1]

    def test_a_page_before_the_start_clamps_to_the_first(self):
        for bad in ("0", "-3", "banana"):
            html = self._client(20).get(f"/?tx_page={bad}").get_data(as_text=True)
            assert self._amounts(html) == [20, 19, 18, 17, 16, 15], bad

    def test_no_pager_when_everything_fits_on_one_page(self):
        html = self._client(4).get("/").get_data(as_text=True)
        assert self._amounts(html) == [4, 3, 2, 1]
        assert "tx_page=" not in html

    def test_an_empty_chain_still_renders(self):
        html = self._client(0).get("/").get_data(as_text=True)
        assert "No transactions yet" in html

    def test_every_block_counts_toward_the_page_total(self):
        # Counted directly: a count that skips a block shortens the pager
        # and makes the oldest transactions unreachable, which no test
        # driving the route can see unless that block happens to hold one.
        chain = [{"transactions": ["a", "b"]}, {"transactions": []},
                 {}, {"transactions": ["c"]}]
        assert api._committed_tx_count(chain) == 3

    def test_the_page_tells_the_live_refresh_which_page_it_is_on(self):
        # The poll only ever carries the newest transactions, so the script
        # has to know not to write them over a reader sitting on page 3.
        html = self._client(20).get("/?tx_page=3").get_data(as_text=True)
        assert 'data-tx-page="3"' in html


class TestOddsPage:
    """The self figure on /odds has three sources and the page has to say
    which one it is showing.

    Once this node has built blocks in the window, its pace is the median
    interval of those, in the same unit as everything it is compared
    against. Before that there is only the VDF clock, which is a different
    quantity (see block.race_odds), either measured from completed builds
    or estimated from calibration."""

    class _OddsNode:
        """The surface /odds and /api/odds touch, and nothing else."""

        def __init__(self, is_estimate, own_blocks=True):
            self.addr = address(0)
            self.settings = settings_mod.Settings(_MemoryMeta())
            # Height 1 is ours at 120s, height 2 is somebody else's at
            # 200s, so there is a field to compare against and a win share
            # to show. own_blocks=False hands height 1 to a third party,
            # leaving this node with nothing of its own in the window.
            self.view = SimpleNamespace(chain=[
                {"height": 0, "timestamp": 1000, "vdf_iterations": 100},
                {"height": 1, "timestamp": 1120, "vdf_iterations": 100,
                 "builder": address(0) if own_blocks else address(2)},
                {"height": 2, "timestamp": 1320, "vdf_iterations": 100,
                 "builder": address(1)},
            ])
            self._is_estimate = is_estimate

        def own_vdf_median(self):
            return 90.0

        def own_vdf_is_estimate(self):
            return self._is_estimate

        def reorg_stats(self):
            return {"deepest": 0, "count": 0}

    def _client(self, is_estimate, own_blocks=True):
        node = self._OddsNode(is_estimate, own_blocks)
        return api.create_private_app(node, peerpool_mod.PeerPool()).test_client()

    def _sub(self, html):
        """The rendered sub-label, not a loose substring: the page also
        ships the refresh script, which carries every label as a literal."""
        marker = '<div class="stat-sub" id="own-median-sub">'
        return html.split(marker, 1)[1].split("<", 1)[0].strip()

    def test_pace_comes_from_our_own_blocks_when_we_have_them(self):
        html = self._client(False).get("/odds").get_data(as_text=True)
        assert self._sub(html) == "median interval of blocks we built"
        # 120s, our block's interval, not the 90s VDF clock.
        assert '<div class="stat-value" id="own-median-val">120.0s' in html

    def test_a_calibrated_figure_is_marked_as_an_estimate(self):
        # No block of ours in the window, so the VDF clock is all there is,
        # and here it has not even been measured yet.
        html = self._client(True, own_blocks=False).get("/odds").get_data(as_text=True)
        assert self._sub(html) == "estimated from calibration, no build finished yet"

    def test_a_measured_clock_without_our_blocks_says_it_is_a_clock(self):
        html = self._client(False, own_blocks=False).get("/odds").get_data(as_text=True)
        assert self._sub(html) == "VDF clock: no block of ours in this window"

    def test_the_page_says_who_is_in_the_draw(self):
        # The number is a share of a draw, so the page has to say how many
        # builders are in it and how wide the window that decided that is.
        # Our 120s against their 200s, so they are outside a 10s window.
        html = self._client(False).get("/odds").get_data(as_text=True)
        assert "only builder inside the 10s draw window" in html
        assert "1 of the last 2" in html   # blocks won, the measured fact

    def test_the_json_carries_the_draw_and_the_win_share(self):
        data = self._client(False).get("/api/odds").get_json()
        assert data["field_blocks"] == 1
        assert data["own_blocks"] == 1
        assert data["win_share_pct"] == 50.0
        assert data["own_pace"] == 120.0
        assert data["own_pace_measured"] is True
        # Their 200s is 80s off our 120s, well past the window.
        assert data["entrants"] == 1
        assert data["field_builders"] == 1
        assert data["draw_window"] == 10.0
        assert data["odds_pct"] == 100.0

    def test_the_configured_window_is_what_decides_the_draw(self):
        # Same chain, wider window: the rival that was too slow becomes a
        # tie and the odds halve, without any hardware changing. Read per
        # request, so a node whose operator edits the setting sees the page
        # that explains it change with it.
        node = self._OddsNode(False)
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        assert client.get("/api/odds").get_json()["odds_pct"] == 100.0

        node.settings.set(settings_mod.DRAW_WINDOW_SECONDS, 100)
        data = client.get("/api/odds").get_json()
        assert data["draw_window"] == 100.0
        assert data["entrants"] == 2
        assert data["odds_pct"] == 50.0

    def test_the_json_carries_the_same_distinction(self):
        assert self._client(True).get("/api/odds").get_json()["own_is_estimate"] is True
        assert self._client(False).get("/api/odds").get_json()["own_is_estimate"] is False


class TestPeersPage:
    """/network is a shell now, all peer data comes live from /api/peers
    and is rendered client-side (the graph), so the route tests split
    the same way: the page itself just needs to render and redirect
    correctly, and the JSON is where the real data-shape and privacy
    assertions belong. There is deliberately no wallet field anywhere: a
    payout address is not something a peer tells us, and not something
    this node publishes (see Node._handle_inbound_alive)."""

    def _client(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        pool.update_info("1.2.3.4:9000", height=5, version="0.2.0")
        pool.add("5.6.7.8:9000")  # no update_info, height/version unknown
        pool.add("9.9.9.9:9000")
        app = api.create_private_app(node, pool)
        return app.test_client()

    def test_peers_page_renders(self):
        resp = self._client().get("/network")
        assert resp.status_code == 200
        assert 'id="topology"' in resp.get_data(as_text=True)

    def test_old_peers_url_redirects(self):
        resp = self._client().get("/peers")
        assert resp.status_code == 301
        assert resp.headers["Location"].endswith("/network")

    def test_api_peers_reports_self_and_peer_data(self):
        data = self._client().get("/api/peers").get_json()
        by_addr = {p["address"]: p for p in data["graph_peers"]}
        assert set(by_addr) == {"1.2.3.4:9000", "5.6.7.8:9000", "9.9.9.9:9000"}
        assert by_addr["1.2.3.4:9000"]["height"] == 5
        assert by_addr["1.2.3.4:9000"]["version"] == "0.2.0"
        assert by_addr["5.6.7.8:9000"]["height"] is None
        assert by_addr["5.6.7.8:9000"]["version"] == ""
        assert "wallet" not in data["self"]
        for p in data["graph_peers"]:
            assert "wallet" not in p

    def test_peer_claiming_our_own_genesis_hash_is_not_a_fork(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        pool.update_info("1.2.3.4:9000", height=0, tip_hash=cs.chain[0]["hash"])
        app = api.create_private_app(node, pool)
        data = app.test_client().get("/api/peers").get_json()
        peer = next(p for p in data["graph_peers"] if p["address"] == "1.2.3.4:9000")
        assert peer["is_fork"] is False
        assert peer["fork_depth"] is None

    def test_peer_claiming_a_different_hash_at_a_height_we_hold_is_a_fork(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        pool.update_info("1.2.3.4:9000", height=0, tip_hash="not-our-genesis-hash")
        app = api.create_private_app(node, pool)
        data = app.test_client().get("/api/peers").get_json()
        peer = next(p for p in data["graph_peers"] if p["address"] == "1.2.3.4:9000")
        assert peer["is_fork"] is True
        assert peer["fork_depth"] == 0  # our own tip is also height 0 here

    def test_peer_claiming_a_height_past_our_own_tip_is_never_flagged(self):
        """We have nothing of our own to compare a claim past our tip
        against, so it must read as unknown, not as a fork."""
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        pool.update_info("1.2.3.4:9000", height=50, tip_hash="whatever")
        app = api.create_private_app(node, pool)
        data = app.test_client().get("/api/peers").get_json()
        peer = next(p for p in data["graph_peers"] if p["address"] == "1.2.3.4:9000")
        assert peer["is_fork"] is False
        assert peer["fork_depth"] is None

    def test_peer_with_no_tip_hash_yet_is_never_flagged(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")  # no update_info call at all
        app = api.create_private_app(node, pool)
        data = app.test_client().get("/api/peers").get_json()
        peer = next(p for p in data["graph_peers"] if p["address"] == "1.2.3.4:9000")
        assert peer["is_fork"] is False
        assert peer["fork_depth"] is None


class TestUpdateNav:
    """Smoke test the nav bar's update-available link for each severity,
    catches a template/Jinja mismatch in the severity->label/color lookup."""

    def _render_page(self, severity):
        from update_check import UpdateChecker

        node, _ = fresh()
        pool = peerpool_mod.PeerPool()
        checker = UpdateChecker(local_version="0.1.1")
        checker.severity = severity
        checker.latest_version = "9.9.9"
        app = api.create_app(node, pool, update_checker=checker)
        return app.test_client().get("/network").get_data(as_text=True)

    def test_no_link_when_no_update(self):
        # Checks for the update link's own rendered element, not a loose
        # "update" substring, the page also renders a randomly generated
        # wallet address (dot-joined words from a wordlist), which can
        # coincidentally contain "update" and has nothing to do with what
        # this test covers. The stylesheet always defines .version-alert
        # regardless of whether the link renders, so match the actual
        # element's opening tag, not just the class name appearing anywhere.
        html = self._render_page(None)
        assert 'class="version-alert"' not in html

    def test_minor_severity_label(self):
        html = self._render_page("minor")
        assert "New version available" in html

    def test_critical_severity_label(self):
        html = self._render_page("critical")
        assert "Critical update available" in html

    def test_protocol_severity_label(self):
        html = self._render_page("protocol")
        assert "Protocol update required" in html


class TestSettingsValidation:
    """A value that can't be parsed must be refused, not stored. Stored
    junk reads back as the default, so the page would say saved while the
    node quietly ran something else."""

    def _client(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool()
        app = api.create_private_app(node, pool)
        return app.test_client(), node

    def _token(self, client):
        html = client.get("/settings").get_data(as_text=True)
        import re
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def test_a_bad_number_is_reported_and_not_stored(self):
        client, node = self._client()
        before = node.settings.get(settings_mod.DRAW_WINDOW_SECONDS)
        resp = client.post("/settings", data={
            "csrf_token": self._token(client),
            "draw_window_seconds": "not a number",
        })
        assert b"not valid" in resp.data or b"could not convert" in resp.data
        assert node.settings.get(settings_mod.DRAW_WINDOW_SECONDS) == before

    def test_a_negative_window_is_refused(self):
        client, node = self._client()
        before = node.settings.get(settings_mod.DRAW_WINDOW_SECONDS)
        client.post("/settings", data={
            "csrf_token": self._token(client),
            "draw_window_seconds": "-5",
        })
        assert node.settings.get(settings_mod.DRAW_WINDOW_SECONDS) == before

    def test_a_valid_number_is_stored(self):
        client, node = self._client()
        client.post("/settings", data={
            "csrf_token": self._token(client),
            "draw_window_seconds": "3.5",
        })
        assert node.settings.get(settings_mod.DRAW_WINDOW_SECONDS) == 3.5

    def test_an_env_forced_setting_is_not_writable_from_the_page(self, monkeypatch):
        monkeypatch.setenv("LAPSECOIN_DRAW_WINDOW_SECONDS", "7")
        client, node = self._client()
        client.post("/settings", data={
            "csrf_token": self._token(client),
            "draw_window_seconds": "999",
        })
        assert node.settings.get(settings_mod.DRAW_WINDOW_SECONDS) == 7.0


class TestBoardPage:
    """Chat-style layout: oldest post at the top, newest (and anything
    still pending in the mempool) at the bottom right above the compose
    box, rather than the old top-down "newest first, compose above
    everything" arrangement. See api.py's _board_ctx/_board_pending."""

    def _client(self, pending_msgs=()):
        import tx as tx_mod
        TAG = tx_mod.BOARD_MEMO_TAG
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        confirmed = ["oldest confirmed post", "middle confirmed post",
                     "newest confirmed post"]
        for i, msg in enumerate(confirmed):
            t = {"from": address(i % 3), "nonce": i + 1, "fee": 100,
                 "outputs": [{"to": "1" * 40, "amount": 1}], "memo": TAG + msg}
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
        cs.state.total_board_posts = len(confirmed)
        node = _FakeNode(cs)
        for i, msg in enumerate(pending_msgs):
            t = make_tx(i % 3, (i + 1) % 3, 1, cs.state, fee=100, memo=TAG + msg)
            node.mempool.add(t)
        pool = peerpool_mod.PeerPool()
        app = api.create_private_app(node, pool)
        return app.test_client()

    def test_confirmed_posts_render_oldest_first(self):
        html = self._client().get("/board").get_data(as_text=True)
        assert (html.index("oldest confirmed post")
                < html.index("middle confirmed post")
                < html.index("newest confirmed post"))

    def test_pending_posts_render_after_confirmed_on_page_one(self):
        html = self._client(pending_msgs=["still pending"]).get("/board").get_data(as_text=True)
        assert html.index("newest confirmed post") < html.index("still pending")
        assert "pending" in html

    def test_pending_posts_do_not_appear_on_page_two(self):
        import tx as tx_mod
        TAG = tx_mod.BOARD_MEMO_TAG
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        # BOARD_PER_PAGE is 20: 21 confirmed posts makes page 2 a real,
        # distinct (older) page rather than one the router clamps back to
        # page 1 (which is what a "page 2" that doesn't actually exist
        # yet would do, and is not what this test means to check).
        for i in range(21):
            t = {"from": address(i % 3), "nonce": i + 1, "fee": 100,
                 "outputs": [{"to": "1" * 40, "amount": 1}], "memo": TAG + f"post {i}"}
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
        cs.state.total_board_posts = 21
        node = _FakeNode(cs)
        t = make_tx(0, 1, 1, cs.state, fee=100, memo=TAG + "still pending")
        node.mempool.add(t)
        pool = peerpool_mod.PeerPool()
        client = api.create_private_app(node, pool).test_client()

        resp = client.get("/board?page=2")
        html = resp.get_data(as_text=True)
        assert "still pending" not in html
        # Sanity: this really is a distinct older page, not a silent
        # clamp-back to page 1 (which would make the assertion above
        # meaningless).
        assert 'class="pager-current">2<' in html

    def test_no_leftover_waiting_to_be_mined_banner(self):
        """The old compose_ok text this replaced must not still be
        reachable through this page."""
        html = self._client(pending_msgs=["still pending"]).get("/board").get_data(as_text=True)
        assert "waiting to be mined" not in html

    def test_passphrase_field_is_hidden_not_a_visible_password_input(self):
        html = self._client().get("/board").get_data(as_text=True)
        assert 'id="board-passphrase"' in html
        assert 'type="hidden" name="passphrase"' in html
        assert 'type="password"' not in html

    def test_compose_form_is_the_last_thing_in_the_feed(self):
        html = self._client(pending_msgs=["still pending"]).get("/board").get_data(as_text=True)
        assert html.index("still pending") < html.index('class="board-compose"')


class TestPeersForDownload:
    """_peers_for_download: the list /api/peers/download hands out.

    Self is included deliberately, see api_peers_download's own comment:
    whoever downloads this file wants a bootstrap seed for a new node,
    and this node is a perfectly good candidate the moment its own
    address is known.
    """

    def test_self_appended_when_known_and_not_already_present(self):
        result = api._peers_for_download(["1.2.3.4:8333"], "5.6.7.8:8333")
        assert result == ["1.2.3.4:8333", "5.6.7.8:8333"]

    def test_self_not_duplicated_if_already_a_known_peer(self):
        """Two nodes that already peered with each other could otherwise
        end up with a duplicate entry for the same address."""
        result = api._peers_for_download(["5.6.7.8:8333"], "5.6.7.8:8333")
        assert result == ["5.6.7.8:8333"]

    def test_self_omitted_entirely_when_not_yet_known(self):
        """our_external_addr is None until the first PONG confirms it;
        nothing should be appended rather than adding a garbage entry."""
        result = api._peers_for_download(["1.2.3.4:8333"], None)
        assert result == ["1.2.3.4:8333"]

    def test_self_omitted_when_empty_string(self):
        result = api._peers_for_download(["1.2.3.4:8333"], "")
        assert result == ["1.2.3.4:8333"]

    def test_does_not_mutate_the_input_list(self):
        known = ["1.2.3.4:8333"]
        api._peers_for_download(known, "5.6.7.8:8333")
        assert known == ["1.2.3.4:8333"]


class TestPeerListPublishesNoAddresses:
    """The peers view must never tie a payable address to an IP.

    This used to be a weaker claim: a count of nodes that had announced
    themselves was shown, just not which ones. The announcements are gone
    now (they broadcast a payable address network-wide, which is exactly
    the link a trading identity cannot afford), so the invariant tightens
    to what it should always have been: no LapseCoin address appears in
    this view at all.
    """

    def _client(self):
        cs = ChainState.from_genesis()
        seed_balance(cs.state, 0, 1000.0)
        node = _FakeNode(cs)
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        return api.create_private_app(node, pool).test_client()

    def test_no_address_appears_in_the_peers_api(self):
        resp = self._client().get("/api/peers")
        body = resp.get_data(as_text=True)
        for i in range(1, 4):
            assert address(i) not in body

    def test_no_peer_row_carries_a_payout_address(self):
        data = self._client().get("/api/peers").get_json()
        for peer in data["graph_peers"]:
            assert "wallet" not in peer
        assert "wallet" not in data["self"]

    def test_no_announced_count_is_reported_any_more(self):
        data = self._client().get("/api/peers").get_json()
        assert "alive_count" not in data


# ---------------------------------------------------------------------------
# /send's XLM half (plan item 5.1): a plain payment or an account-merge
# through the same wallet market_routes trades against. No Flask app or
# HTTP involved; api._submit_xlm_and_alert is a pure function of (node,
# wallet path, form values, ctx dict) once Horizon itself is faked out.
# ---------------------------------------------------------------------------

import crypto as crypto_mod
import xlm as xlm_mod


class _XlmSendNode:
    def __init__(self, tmp_path):
        sk, pk = crypto_mod.generate_keypair()
        self.keyfile = str(tmp_path / "node.key")
        self.passphrase = "correct horse battery staple"
        crypto_mod.save_key(self.keyfile, sk, pk, self.passphrase)
        self.addr = crypto_mod.public_key_to_address(pk)


def _make_wallet(tmp_path, node, name="xlm.key"):
    kek = crypto_mod.derive_kek(node.keyfile, node.passphrase)
    seed, pub = xlm_mod.generate_keypair()
    path = str(tmp_path / name)
    xlm_mod.save_key(path, seed, pub, kek=kek)
    return path, pub


class TestXlmView:
    def test_no_wallet_reads_as_blank(self, tmp_path):
        assert api._xlm_view(str(tmp_path / "none.key")) == ("", 0, 0)

    def test_reports_spendable_and_locked(self, tmp_path, monkeypatch):
        node = _XlmSendNode(tmp_path)
        path, pub = _make_wallet(tmp_path, node)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 100)
        monkeypatch.setattr(xlm_mod, "get_balance_stroops", lambda addr: 1100)
        addr, spendable, locked = api._xlm_view(path)
        assert addr == pub
        assert spendable == 100
        assert locked == 1000

    def test_unreachable_horizon_reads_as_zero_not_a_crash(self, tmp_path, monkeypatch):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        def boom(addr):
            raise xlm_mod.XLMUnreachable("down")
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", boom)
        _addr, spendable, locked = api._xlm_view(path)
        assert (spendable, locked) == (0, 0)


class TestSubmitXlmAndAlert:
    def _ctx(self):
        return {"alert_err": "", "alert_ok_tx": "", "alert_ok_verb": ""}

    def test_requires_a_passphrase(self, tmp_path):
        node = _XlmSendNode(tmp_path)
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, "", "GDEST", 100, "", ctx)
        assert "Passphrase" in ctx["alert_err"]

    def test_requires_a_wallet_to_exist(self, tmp_path):
        node = _XlmSendNode(tmp_path)
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, str(tmp_path / "missing.key"), "GDEST",
                                  100, node.passphrase, ctx)
        assert "trading address" in ctx["alert_err"]

    def test_rejects_an_invalid_destination(self, tmp_path):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, "not-an-address", 100,
                                  node.passphrase, ctx)
        assert "valid Stellar address" in ctx["alert_err"]

    def test_rejects_the_wrong_passphrase(self, tmp_path):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        _seed, dest = xlm_mod.generate_keypair()
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, dest, 100, "wrong", ctx)
        assert "passphrase" in ctx["alert_err"]

    def test_rejects_sending_to_its_own_address(self, tmp_path):
        node = _XlmSendNode(tmp_path)
        path, pub = _make_wallet(tmp_path, node)
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, pub, 100, node.passphrase, ctx)
        assert "own address" in ctx["alert_err"]

    def test_rejects_an_amount_above_spendable(self, tmp_path, monkeypatch):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        _seed, dest = xlm_mod.generate_keypair()
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 1)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 50)
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, dest, 100, node.passphrase, ctx)
        assert "spend" in ctx["alert_err"]

    def test_a_successful_payment_reports_the_tx_hash(self, tmp_path, monkeypatch):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        _seed, dest = xlm_mod.generate_keypair()
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 1)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 1000)
        monkeypatch.setattr(xlm_mod, "build_payment",
                            lambda seed, to, amt, memo, seq: ("xdr", "hash123"))
        monkeypatch.setattr(xlm_mod, "submit_envelope",
                            lambda xdr: (True, "hash123", "submitted"))
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, dest, 100, node.passphrase, ctx)
        assert ctx["alert_ok_tx"] == "hash123"
        assert ctx["alert_err"] == ""

    def test_a_rejected_submission_reports_the_chains_own_detail(self, tmp_path, monkeypatch):
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        _seed, dest = xlm_mod.generate_keypair()
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 1)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops", lambda addr: 1000)
        monkeypatch.setattr(xlm_mod, "build_payment",
                            lambda seed, to, amt, memo, seq: ("xdr", "hash123"))
        monkeypatch.setattr(xlm_mod, "submit_envelope",
                            lambda xdr: (False, "hash123", "tx_bad_seq"))
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, dest, 100, node.passphrase, ctx)
        assert "tx_bad_seq" in ctx["alert_err"]
        assert ctx["alert_ok_tx"] == ""

    def test_merge_ignores_the_spendable_check_and_the_amount(self, tmp_path, monkeypatch):
        """The whole point of a merge is reclaiming the reserve a plain
        payment can never touch, so it must not be gated by the same
        spendable-only check a payment is."""
        node = _XlmSendNode(tmp_path)
        path, _pub = _make_wallet(tmp_path, node)
        _seed, dest = xlm_mod.generate_keypair()
        monkeypatch.setattr(xlm_mod, "get_sequence", lambda addr: 1)
        monkeypatch.setattr(xlm_mod, "get_spendable_stroops",
                            lambda addr: (_ for _ in ()).throw(AssertionError(
                                "merge must not consult spendable balance")))
        monkeypatch.setattr(xlm_mod, "build_account_merge",
                            lambda seed, to, seq: ("xdr", "mergehash"))
        monkeypatch.setattr(xlm_mod, "submit_envelope",
                            lambda xdr: (True, "mergehash", "submitted"))
        ctx = self._ctx()
        api._submit_xlm_and_alert(node, path, dest, 0, node.passphrase, ctx, merge=True)
        assert ctx["alert_ok_tx"] == "mergehash"
        assert "closed" in ctx["alert_ok_verb"].lower()

