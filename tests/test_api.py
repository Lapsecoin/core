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
    .mempool, .view (chain/tip), .addr, and the settings the private
    settings page reads and writes."""

    def __init__(self, cs, addr=None):
        self.mempool = mempool_mod.Mempool()
        self.view = NodeView(cs)
        self.addr = addr or address(0)
        self.settings = settings_mod.Settings(_MemoryMeta())


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
            self.view = SimpleNamespace(chain=chain, state=SimpleNamespace(nicknames={}))

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
        assert self._amounts(html) == [20, 19, 18, 17, 16]

    def test_the_second_page_continues_where_it_left_off(self):
        html = self._client(20).get("/?tx_page=2").get_data(as_text=True)
        assert self._amounts(html) == [15, 14, 13, 12, 11]

    def test_the_last_page_holds_the_remainder(self):
        html = self._client(20).get("/?tx_page=4").get_data(as_text=True)
        assert self._amounts(html) == [5, 4, 3, 2, 1]

    def test_a_page_past_the_end_clamps_to_the_last(self):
        html = self._client(20).get("/?tx_page=99").get_data(as_text=True)
        assert self._amounts(html) == [5, 4, 3, 2, 1]

    def test_a_page_before_the_start_clamps_to_the_first(self):
        for bad in ("0", "-3", "banana"):
            html = self._client(20).get(f"/?tx_page={bad}").get_data(as_text=True)
            assert self._amounts(html) == [20, 19, 18, 17, 16], bad

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

        def __init__(self, is_estimate, own_blocks=True, own_median=90.0,
                    mining_enabled=True):
            self.addr = address(0)
            self.settings = settings_mod.Settings(_MemoryMeta())
            if not mining_enabled:
                self.settings.set(settings_mod.MINING_ENABLED, False)
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
            ], state=SimpleNamespace(nicknames={}))
            self._is_estimate = is_estimate
            self._own_median = own_median

        def own_vdf_median(self):
            return self._own_median

        def own_vdf_is_estimate(self):
            return self._is_estimate

        def reorg_stats(self):
            return {"deepest": 0, "count": 0}

    def _client(self, is_estimate, own_blocks=True, own_median=90.0,
               mining_enabled=True):
        node = self._OddsNode(is_estimate, own_blocks, own_median, mining_enabled)
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

    def test_banner_absent_when_mining_is_on(self):
        html = self._client(False).get("/odds").get_data(as_text=True)
        assert "Not building right now" not in html

    def test_mining_disabled_banner_shows(self):
        html = self._client(False, mining_enabled=False).get("/odds").get_data(as_text=True)
        data = self._client(False, mining_enabled=False).get("/api/odds").get_json()
        assert data["mining_enabled"] is False
        assert "mining is off" in html

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

    def test_hardware_cell_shown_by_default(self):
        html = self._client(False).get("/odds").get_data(as_text=True)
        assert '<div class="stat-label">Hardware</div>' in html

    def test_hardware_cell_hidden_when_setting_is_off(self):
        node = self._OddsNode(False)
        node.settings.set(settings_mod.SHOW_HARDWARE_DETAILS, False)
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/odds").get_data(as_text=True)
        assert '<div class="stat-label">Hardware</div>' not in html

    def test_hardware_cell_contents_are_real_values(self):
        html = self._client(False).get("/odds").get_data(as_text=True)
        # Whatever this machine's OS actually is, not a placeholder.
        import platform as _platform
        assert _platform.system() in html


class TestPeersPage:
    """/network is a shell now, all peer data comes live from /api/peers
    and is rendered client-side (the graph), so the route tests split
    the same way: the page itself just needs to render and redirect
    correctly, and the JSON is where the real data-shape and privacy
    assertions belong. There is deliberately no wallet field anywhere: a
    payout address is not something a peer tells us, and not something
    this node publishes."""

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

    def _full_form(self, client, **overrides):
        """A complete, otherwise-valid submission: every non-bool setting
        needs a value in the same POST or it fails to parse an empty
        string, same as any of these tests would if they posted just one
        field among several required ones."""
        form = {
            "csrf_token": self._token(client),
            "draw_window_seconds": "10.0",
        }
        form.update(overrides)
        return form

    def test_hardware_switch_checked_is_stored_true(self):
        client, node = self._client()
        client.post("/settings", data=self._full_form(
            client, show_hardware_details="on"))
        assert node.settings.get(settings_mod.SHOW_HARDWARE_DETAILS) is True

    def test_hardware_switch_omitted_is_stored_false(self):
        """An unchecked checkbox sends no field at all, browsers never
        submit one, this is the only signal "off" ever has."""
        client, node = self._client()
        node.settings.set(settings_mod.SHOW_HARDWARE_DETAILS, True)
        client.post("/settings", data=self._full_form(client))
        assert node.settings.get(settings_mod.SHOW_HARDWARE_DETAILS) is False

    def test_slider_metadata_reaches_the_page(self):
        """The numeric settings render a slider (a range input); the
        bool settings (show_hardware_details, mining_enabled,
        hide_address_publicly) render a switch each, not a
        slider."""
        client, _ = self._client()
        html = client.get("/settings").get_data(as_text=True)
        assert html.count('type="range"') == 2
        assert html.count('class="switch"') == 3


class TestAddressLookupBurnAlias:
    """Typing "burn" is a lot easier than the real twelve-word address;
    it isn't a secret (crypto.burn_address() is public and deterministic),
    so redirecting to it is just a convenience, not a trust decision."""

    def _client(self):
        node, _ = fresh()
        # address_lookup's full render path (history) reads node.storage;
        # _FakeNode doesn't carry one (nothing else in this file exercises
        # that path). An empty index is enough here: these tests care about
        # the redirect and the banner, not this address's transaction
        # history.
        node.storage = SimpleNamespace(get_tx_heights_for_addr=lambda addr: [])
        pool = peerpool_mod.PeerPool()
        return api.create_private_app(node, pool).test_client()

    def test_burn_redirects_to_the_real_address(self):
        import crypto as crypto_mod
        resp = self._client().get("/address?addr=burn", follow_redirects=False)
        assert resp.status_code == 302
        assert f"addr={crypto_mod.burn_address()}" in resp.headers["Location"]

    def test_case_insensitive_and_trims_whitespace(self):
        resp = self._client().get("/address?addr=%20BURN%20", follow_redirects=False)
        assert resp.status_code == 302

    def test_other_query_params_survive_the_redirect(self):
        resp = self._client().get("/address?addr=burn&page=2", follow_redirects=False)
        assert resp.status_code == 302
        assert "page=2" in resp.headers["Location"]

    def test_redirect_target_actually_shows_the_burn_banner(self):
        resp = self._client().get("/address?addr=burn", follow_redirects=True)
        assert resp.status_code == 200
        assert b"burn address" in resp.data

    def test_an_ordinary_address_is_not_treated_as_the_alias(self):
        resp = self._client().get("/address?addr=" + address(0), follow_redirects=False)
        assert resp.status_code == 200  # rendered directly, no redirect


class TestAddressLookupNicknameRedirect:
    """A board nickname typed into the address lookup redirects to
    whichever address actually owns it -- same pattern, same reasoning,
    as the "burn" alias above, just resolved via _nickname_owned_by
    instead of a fixed constant."""

    def _client_with_nickname(self, nick="Al", owner_index=0):
        import tx as tx_mod
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        t = {"from": address(owner_index), "nonce": 1, "fee": 1,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + api.build_board_body("hi", icon=0, nick=nick)}
        cs.chain.append({"height": len(cs.chain), "timestamp": 1000,
                         "transactions": [t], "hash": "h0"})
        cs.state.apply_tx(t)  # so state.nicknames actually reflects the claim
        # _FakeNode (via NodeView) takes a snapshot of cs.state at
        # construction time, so it has to be built *after* the claim
        # above is applied, not before -- constructing it first would
        # freeze a view of state.nicknames from before this test's own
        # nickname ever existed.
        node = _FakeNode(cs)
        node.storage = SimpleNamespace(get_tx_heights_for_addr=lambda addr: [])
        pool = peerpool_mod.PeerPool()
        return api.create_private_app(node, pool).test_client()

    def test_nickname_redirects_to_its_owner(self):
        client = self._client_with_nickname(nick="Al", owner_index=0)
        resp = client.get("/address?addr=Al", follow_redirects=False)
        assert resp.status_code == 302
        assert f"addr={address(0)}" in resp.headers["Location"]

    def test_lookup_is_case_insensitive(self):
        client = self._client_with_nickname(nick="Al", owner_index=0)
        resp = client.get("/address?addr=AL", follow_redirects=False)
        assert resp.status_code == 302
        assert f"addr={address(0)}" in resp.headers["Location"]

    def test_unclaimed_name_is_not_redirected(self):
        client = self._client_with_nickname(nick="Al", owner_index=0)
        resp = client.get("/address?addr=Nobody", follow_redirects=False)
        assert resp.status_code == 200
        assert "Invalid address format" in resp.get_data(as_text=True)


class TestBoardPage:
    """One continuous feed: compose box on top, then newest thread first
    (anything still pending in the mempool above the newest confirmed
    post). See board_view.board_ctx and board_view._board_events."""

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

    def test_confirmed_posts_render_newest_first(self):
        html = self._client().get("/board").get_data(as_text=True)
        assert (html.index("newest confirmed post")
                < html.index("middle confirmed post")
                < html.index("oldest confirmed post"))

    def test_pending_posts_render_before_confirmed(self):
        html = self._client(pending_msgs=["still pending"]).get("/board").get_data(as_text=True)
        assert html.index("still pending") < html.index("newest confirmed post")
        assert "pending" in html

    def test_long_board_loads_in_chunks_with_a_sentinel(self):
        import tx as tx_mod
        TAG = tx_mod.BOARD_MEMO_TAG
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        n = api.BOARD_THREADS_PER_CHUNK + 1
        for i in range(n):
            t = {"from": address(i % 3), "nonce": i + 1, "fee": 100,
                 "outputs": [{"to": "1" * 40, "amount": 1}], "memo": TAG + f"post {i}"}
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
        cs.state.total_board_posts = n
        client = api.create_private_app(_FakeNode(cs), peerpool_mod.PeerPool()).test_client()
        first = client.get("/api/board/fragment?page=1").get_data(as_text=True)
        assert 'id="board-more"' in first and "post 0" not in first
        both = client.get("/api/board/fragment?page=2").get_data(as_text=True)
        assert 'id="board-more"' not in both and "post 0" in both

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

    def test_compose_form_is_the_first_thing_in_the_feed(self):
        html = self._client(pending_msgs=["still pending"]).get("/board").get_data(as_text=True)
        assert html.index('class="board-compose"') < html.index("still pending")


class TestBoardMemoParsing:
    """parse_board_body / build_board_body: the profile-header and
    reply-header convention folded into a post's own memo (see api.py's
    module-level comment on why -- a post pays for these once, not as a
    transaction of their own)."""

    def test_round_trips_plain_text_with_no_header(self):
        body = api.build_board_body("hello board")
        assert body == "hello board"
        icon, nick, reply_ref, text = api.parse_board_body(body)
        assert (icon, nick, reply_ref, text) == (None, None, None, "hello board")

    def test_round_trips_profile_header_only(self):
        body = api.build_board_body("hi", icon=3, nick="Bob")
        icon, nick, reply_ref, text = api.parse_board_body(body)
        assert (icon, nick, reply_ref, text) == (3, "Bob", None, "hi")

    def test_round_trips_reply_header_only(self):
        body = api.build_board_body("+1", reply_ref="abc123")
        icon, nick, reply_ref, text = api.parse_board_body(body)
        assert (icon, nick, reply_ref, text) == (None, None, "abc123", "+1")

    def test_round_trips_both_headers_together(self):
        body = api.build_board_body("agreed", icon=1, nick="Al", reply_ref="deadbe")
        icon, nick, reply_ref, text = api.parse_board_body(body)
        assert (icon, nick, reply_ref, text) == (1, "Al", "deadbe", "agreed")

    def test_nickname_cannot_smuggle_a_closing_bracket(self):
        """A ']' in the nickname would otherwise let it terminate the
        profile header early, corrupting whatever the parser reads as
        free text next -- stripped rather than rejected (see
        build_board_body's own comment)."""
        body = api.build_board_body("hi", icon=0, nick="Bo]b")
        icon, nick, reply_ref, text = api.parse_board_body(body)
        assert nick == "Bob"
        assert text == "hi"

    def test_out_of_range_icon_index_parses_as_a_raw_int(self):
        # Hand-built rather than through build_board_body, which would
        # never emit an out-of-range index itself: this simulates an
        # older/newer client with a differently sized palette.
        # parse_board_body itself doesn't bounds-check (that's tx.py's
        # job now, a consensus-neutral parser) -- see _icon_emoji below
        # for where the fallback to the ghost placeholder actually lives.
        idx = len(api.ICON_PALETTE) + 5
        icon, nick, reply_ref, text = api.parse_board_body(f"[p:{idx}:X]hi")
        assert icon == idx
        assert text == "hi"

    def test_icon_emoji_falls_back_to_ghost_when_out_of_range(self):
        idx = len(api.ICON_PALETTE) + 5
        assert api._icon_emoji(idx) == api.ICON_GHOST
        assert api._icon_emoji(None) == api.ICON_GHOST
        assert api._icon_emoji(0) == api.ICON_PALETTE[0]


class TestBoardProfilesAndVotes:
    """_board_profiles_and_votes: the address->profile lookup and the
    per-target vote tally, including the "latest wins, not summed"
    semantics a vote needs (see the function's own docstring) and pending
    (mempool) votes counting immediately instead of only once mined."""

    TAG = None  # set in setup_method to avoid importing tx at collection time

    def setup_method(self):
        import tx as tx_mod
        self.tx_mod = tx_mod
        self.TAG = tx_mod.BOARD_MEMO_TAG

    def _chain_with(self, *memos_by_sender):
        """memos_by_sender: list of (sender_index, memo) pairs, oldest
        first, one block each."""
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        for i, (sender, memo) in enumerate(memos_by_sender):
            t = {"from": address(sender), "nonce": i + 1, "fee": 100,
                 "outputs": [{"to": "1" * 40, "amount": 1}], "memo": memo}
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
            # apply_tx (not just appending to the chain list) so
            # cs.state.nicknames -- now the real source
            # _board_profiles_and_votes reads -- comes out exactly like a
            # node's own would after accepting these same transactions.
            cs.state.apply_tx(t)
        return cs

    def test_latest_profile_per_address_wins(self):
        cs = self._chain_with(
            (0, self.TAG + api.build_board_body("first", icon=1, nick="Old")),
            (0, self.TAG + api.build_board_body("second", icon=5, nick="New")),
        )
        profiles, _votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert profiles[address(0)] == {"icon": 5, "nick": "New"}

    def test_different_addresses_keep_separate_profiles(self):
        cs = self._chain_with(
            (0, self.TAG + api.build_board_body("hi", icon=1, nick="A")),
            (1, self.TAG + api.build_board_body("hi", icon=2, nick="B")),
        )
        profiles, _votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert profiles[address(0)] == {"icon": 1, "nick": "A"}
        assert profiles[address(1)] == {"icon": 2, "nick": "B"}

    def test_vote_is_replaced_not_summed_when_same_address_votes_twice(self):
        cs = self._chain_with(
            (0, api.VOTE_UP_TAG + "abcdef"),
            (0, api.VOTE_DOWN_TAG + "abcdef"),
        )
        _profiles, votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert votes["abcdef"]["up"] == 0
        assert votes["abcdef"]["down"] == 1

    def test_votes_from_different_addresses_both_count(self):
        cs = self._chain_with(
            (0, api.VOTE_UP_TAG + "abcdef"),
            (1, api.VOTE_UP_TAG + "abcdef"),
        )
        _profiles, votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert votes["abcdef"]["up"] == 2

    def test_nickname_is_first_come_first_served(self):
        """A later address claiming an already-taken name (impersonation)
        gets its icon but not the name -- see _board_profiles_and_votes'
        own docstring on why first-claim-wins needs no consensus rule."""
        cs = self._chain_with(
            (0, self.TAG + api.build_board_body("hi", icon=1, nick="Al")),
            (1, self.TAG + api.build_board_body("hey", icon=2, nick="Al")),
        )
        profiles, _votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert profiles[address(0)] == {"icon": 1, "nick": "Al"}
        assert profiles[address(1)] == {"icon": 2, "nick": None}

    def test_nickname_ownership_is_case_insensitive(self):
        cs = self._chain_with(
            (0, self.TAG + api.build_board_body("hi", icon=1, nick="Al")),
            (1, self.TAG + api.build_board_body("hey", icon=2, nick="al")),
        )
        profiles, _votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert profiles[address(1)]["nick"] is None

    def test_nickname_owned_by_reports_the_first_claimant(self):
        cs = self._chain_with(
            (0, self.TAG + api.build_board_body("hi", icon=1, nick="Al")),
        )
        assert api._nickname_owned_by(cs.state, "AL") == address(0)
        assert api._nickname_owned_by(cs.state, "Bob") is None
        assert api._nickname_owned_by(cs.state, "") is None


    def test_vote_tag_is_not_a_board_post(self):
        """Confirms voting never shares the board's fee-floor staircase:
        is_board_post only matches BOARD_MEMO_TAG, a different tag family
        entirely (see tx.py/api.py's own comments on this)."""
        assert not self.tx_mod.is_board_post({"memo": api.VOTE_UP_TAG + "abcdef"})

    def test_pending_mempool_vote_counts_immediately(self):
        cs = self._chain_with()
        node = _FakeNode(cs)
        t = make_tx(0, 1, 1, cs.state, fee=100, memo=api.VOTE_UP_TAG + "abcdef")
        node.mempool.add(t)
        _profiles, votes, pending_refs = api._board_profiles_and_votes(cs.chain, cs.state.nicknames, node.mempool)
        assert votes["abcdef"]["up"] == 1
        assert "abcdef" in pending_refs

    def test_confirmed_vote_is_not_marked_pending(self):
        cs = self._chain_with((0, api.VOTE_UP_TAG + "abcdef"))
        _profiles, _votes, pending_refs = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        assert pending_refs == set()


class TestCurrentNicknamesByAddress:
    """The odds page's builder labels and the dashboard's own-address line
    both derive addr -> nick from _board_profiles_and_votes' own profiles
    dict, rather than a second scan -- this just checks that derivation."""

    def _chain_with(self, *memos_by_sender):
        import tx as tx_mod
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        for i, (sender, memo) in enumerate(memos_by_sender):
            t = {"from": address(sender), "nonce": i + 1, "fee": 100,
                 "outputs": [{"to": "1" * 40, "amount": 1}], "memo": memo}
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
            cs.state.apply_tx(t)
        return cs, tx_mod

    def _nicknames_by_address(self, cs):
        profiles, _votes, _pending = api._board_profiles_and_votes(cs.chain, cs.state.nicknames)
        return {addr: prof["nick"] for addr, prof in profiles.items()}

    def test_latest_nickname_per_address(self):
        # Second post overwrites the *display* nick, not the registry --
        # "Old" stays permanently owned by address(0), it just isn't
        # what's shown once a newer post says otherwise.
        cs, _tx_mod = self._chain_with(
            (0, api.BOARD_MEMO_TAG + api.build_board_body("a", icon=0, nick="Old")),
            (0, api.BOARD_MEMO_TAG + api.build_board_body("b", icon=1, nick="New")),
        )
        result = self._nicknames_by_address(cs)
        assert result[address(0)] == "New"

    def test_address_with_no_profile_post_is_absent(self):
        cs, _tx_mod = self._chain_with((0, api.BOARD_MEMO_TAG + "just text, no header"))
        result = self._nicknames_by_address(cs)
        assert address(0) not in result

    def test_a_nickname_claimed_by_someone_else_never_displays(self):
        cs, _tx_mod = self._chain_with(
            (0, api.BOARD_MEMO_TAG + api.build_board_body("a", icon=0, nick="Al")),
            (1, api.BOARD_MEMO_TAG + api.build_board_body("b", icon=1, nick="Al")),
        )
        result = self._nicknames_by_address(cs)
        assert result[address(0)] == "Al"
        assert result[address(1)] is None


class TestRaceChartNicknames:
    """_race_chart(race, nicknames_by_addr): the odds page's builder
    labels, wired to the same registry the board itself reads."""

    def _race(self, builders):
        """builders: list of address strings, oldest first, one 100s
        block-interval row each."""
        window = [(i + 1, 100.0, b) for i, b in enumerate(builders)]
        return {"window": window, "median": 100.0, "own_pace": 100.0}

    def test_points_carry_the_builders_nickname(self):
        race = self._race([address(0), address(1)])
        chart = api._race_chart(race, {address(0): "Al"})
        by_height = {p["height"]: p for p in chart["points"]}
        assert by_height[1]["nick"] == "Al"
        assert by_height[2]["nick"] is None

    def test_legend_carries_the_builders_nickname(self):
        race = self._race([address(0)] * 5)
        chart = api._race_chart(race, {address(0): "Al"})
        assert chart["legend"][0]["label"] == address(0)
        assert chart["legend"][0]["nick"] == "Al"

    def test_missing_nicknames_dict_defaults_to_no_nicknames_shown(self):
        race = self._race([address(0)])
        chart = api._race_chart(race)  # no nicknames_by_addr at all
        assert chart["points"][0]["nick"] is None


class TestRenderBoardText:
    """render_board_text's markdown-like subset, including the quote and
    link support added alongside the toolbar buttons for them (see
    board.html): both are only ever rendered from already-escaped text,
    so a post's own content can never inject a real tag of its own."""

    def test_quote_line_becomes_a_blockquote(self):
        html = str(api.render_board_text("intro\n> quoted\nafter"))
        assert "<blockquote>quoted</blockquote>" in html

    def test_non_quote_lines_are_not_touched(self):
        html = str(api.render_board_text("a > b"))
        assert "<blockquote>" not in html

    def test_bracket_link_renders_as_anchor_with_given_text(self):
        html = str(api.render_board_text("see [our site](https://example.com) now"))
        assert '<a href="https://example.com" rel="nofollow noopener noreferrer" target="_blank">our site</a>' in html

    def test_bracket_link_does_not_get_double_wrapped_by_autolink(self):
        """The autolink pass runs after link syntax is stashed behind a
        placeholder specifically so it never sees the raw URL sitting in
        the href it's about to render -- this pins that ordering."""
        html = str(api.render_board_text("[x](https://example.com)"))
        assert html.count("<a ") == 1

    def test_bare_url_still_autolinks_without_bracket_syntax(self):
        html = str(api.render_board_text("see https://example.com now"))
        assert '<a href="https://example.com"' in html
        assert html.count("<a ") == 1


class TestBoardPageRendersProfilesRepliesAndVotes:
    """HTTP-level: confirms the GET /board render path actually surfaces
    profile nicknames, reply previews, and vote tallies computed above --
    the write-side routes (board_post/board_vote) need a real signing
    node (build_and_sign_tx + submit_tx_from_api) that no fixture in this
    file provides for any route yet, not just these two, so they're
    exercised at the pure-function/mempool level in the classes above
    instead of end-to-end over HTTP."""

    def _client(self, extra_txs=()):
        import tx as tx_mod
        TAG = tx_mod.BOARD_MEMO_TAG
        cs = ChainState.from_genesis()
        for i in range(3):
            seed_balance(cs.state, i, 1000.0)
        base = [
            {"from": address(0), "nonce": 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": TAG + api.build_board_body("hi there", icon=2, nick="Al")},
        ]
        for i, t in enumerate(base + list(extra_txs)):
            cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + i,
                             "transactions": [t], "hash": f"h{i}"})
            cs.state.apply_tx(t)  # so state.nicknames reflects the profile claim
        node = _FakeNode(cs, addr=address(0))
        pool = peerpool_mod.PeerPool()
        app = api.create_private_app(node, pool)
        return app.test_client(), node

    def test_nickname_shown_instead_of_raw_address(self):
        client, _node = self._client()
        html = client.get("/board").get_data(as_text=True)
        assert ">Al<" in html
        assert 'id="board-icon-btn"' in html  # own compose box picked up the same profile

    def test_reply_preview_quotes_the_parent_post(self):
        _client, node = self._client()
        # Compute the real parent ref the same way api.py does, via tx_hash.
        import tx as tx_mod
        parent_tx = node.view.chain[1]["transactions"][0]
        parent_ref = tx_mod.tx_hash(parent_tx)[:api.REPLY_REF_LEN]
        reply_tx = {"from": address(1), "nonce": 1, "fee": 100,
                    "outputs": [{"to": "1" * 40, "amount": 1}],
                    "memo": tx_mod.BOARD_MEMO_TAG + api.build_board_body("agreed", reply_ref=parent_ref)}
        client, node = self._client(extra_txs=[reply_tx])
        html = client.get("/board").get_data(as_text=True)
        assert "replying to" in html
        assert "hi there" in html  # the quoted snippet of the parent

    def test_vote_score_rendered_on_the_post(self):
        import tx as tx_mod
        parent_tx = self._client()[1].view.chain[1]["transactions"][0]
        parent_ref = tx_mod.tx_hash(parent_tx)[:api.REPLY_REF_LEN]
        vote_tx = {"from": address(1), "nonce": 1, "fee": 100,
                   "outputs": [{"to": "1" * 40, "amount": 1}],
                   "memo": api.VOTE_UP_TAG + parent_ref}
        client, _node = self._client(extra_txs=[vote_tx])
        html = client.get("/board").get_data(as_text=True)
        assert 'class="rc-votes rc-votesPositive ' in html

    def test_pending_vote_score_stays_numeric_and_describes_pending_on_hover(self):
        import tx as tx_mod
        parent_tx = self._client()[1].view.chain[1]["transactions"][0]
        parent_ref = tx_mod.tx_hash(parent_tx)[:api.REPLY_REF_LEN]
        vote_tx = {"from": address(1), "nonce": 1, "fee": 100,
                   "outputs": [{"to": "1" * 40, "amount": 1}],
                   "memo": api.VOTE_UP_TAG + parent_ref}
        client, node = self._client()
        node.mempool.add(vote_tx)
        html = client.get("/board").get_data(as_text=True)
        assert 'class="rc-votes rc-votesPositive rc-votesPending"' in html
        assert 'title="Includes a vote not yet in a block">1</span>' in html

    def test_preview_endpoint_renders_through_the_same_function_as_posts(self):
        """No signing needed here (unlike board_post/board_vote): preview
        never touches the chain, mempool or a balance, it just renders
        text, so this one write-side-looking route IS testable over
        real HTTP."""
        client, _node = self._client()
        resp = client.post("/api/board/preview", data={"text": "**bold** and `code`"})
        assert resp.get_json()["html"] == "<strong>bold</strong> and <code>code</code>"


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


class _InfoNode(_FakeNode):
    """_FakeNode plus enough of get_info() for the dashboard and /api/info
    routes exercised below. Shape matches TestDashboardTxPaging's
    _DashNode; this one additionally carries .settings (real _FakeNode
    behavior) so HIDE_ADDRESS_PUBLICLY actually has something to read."""

    def get_info(self):
        return {"height": self.view.height, "sync_percent": 100,
                "sync_target": self.view.height, "tip_hash": self.view.tip["hash"],
                "genesis_hash": self.view.genesis_hash,
                "mempool_size": self.mempool.size(), "address": self.addr,
                "peer_count": 0, "total_minted": 0, "burned": 0,
                "circulating": 0, "can_mint": 0, "block_reward": 0,
                "block_time_ratio": None, "network_age_seconds": 0,
                "status": "ok"}


class TestAddressHiddenPublicly:
    """settings.HIDE_ADDRESS_PUBLICLY (default True): the public app must
    not reveal this node's own address, or which board post/recent
    transaction is its own, the way the private (127.0.0.1) app always
    does regardless of the setting. See api.py's _own_addr_or_hidden."""

    def _node(self, hide=None):
        cs = ChainState.from_genesis()
        seed_balance(cs.state, 0, 1000.0)
        import tx as tx_mod
        TAG = tx_mod.BOARD_MEMO_TAG
        t = {"from": address(0), "nonce": 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}], "memo": TAG + "hello"}
        cs.chain.append({"height": len(cs.chain), "timestamp": 1000,
                         "transactions": [t], "hash": "h0"})
        cs.state.total_board_posts = 1
        node = _InfoNode(cs)
        if hide is not None:
            node.settings.set(settings_mod.HIDE_ADDRESS_PUBLICLY, hide)
        return node

    def test_dashboard_hides_address_on_public_app_by_default(self):
        node = self._node()
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/").get_data(as_text=True)
        assert node.addr not in html
        assert "card-title\">Address" not in html

    def test_dashboard_shows_address_on_public_app_when_setting_off(self):
        node = self._node(hide=False)
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/").get_data(as_text=True)
        assert node.addr in html

    def test_dashboard_always_shows_address_on_private_app(self):
        """Regardless of the setting: there's nothing to protect by
        hiding an operator's own address from themselves, locally."""
        for hide in (True, False):
            node = self._node(hide=hide)
            client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
            html = client.get("/").get_data(as_text=True)
            assert node.addr in html

    def test_api_info_omits_address_on_public_app_by_default(self):
        node = self._node()
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/info").get_json()
        assert data["address"] is None

    def test_api_info_carries_address_on_private_app(self):
        node = self._node()
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/info").get_json()
        assert data["address"] == node.addr

    def test_board_hides_which_post_is_own_on_public_app_by_default(self):
        # Board posts are public, on-chain data; every poster's address
        # still shows (that's the point of a public board). What must
        # not show is which one is *this node's own* -- the "mine" class
        # -- even though this fixture's one post happens to be from the
        # same address as the node itself.
        node = self._node()
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/board").get_data(as_text=True)
        assert 'class="rc-root mine' not in html

    def test_board_shows_which_post_is_own_on_public_app_when_setting_off(self):
        node = self._node(hide=False)
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/board").get_data(as_text=True)
        assert 'class="rc-root mine' in html

    def test_api_board_omits_own_addr_on_public_app_by_default(self):
        node = self._node()
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/board").get_json()
        assert data["own_addr"] is None

    def test_api_board_carries_own_addr_on_private_app(self):
        node = self._node()
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/board").get_json()
        assert data["own_addr"] == node.addr


class TestDashboardNickname:
    """The dashboard's own-address card shows a nickname when this node
    has claimed one, but only ever alongside the address itself -- never
    as a substitute that would keep showing identity once
    HIDE_ADDRESS_PUBLICLY has hidden the address it's derived from (see
    dashboard()'s own comment on why)."""

    def _node_with_nickname(self, nick="Al", hide=None):
        import tx as tx_mod
        cs = ChainState.from_genesis()
        seed_balance(cs.state, 0, 1000.0)
        t = {"from": address(0), "nonce": 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + tx_mod.build_board_body("hi", icon=0, nick=nick)}
        cs.chain.append({"height": len(cs.chain), "timestamp": 1000,
                         "transactions": [t], "hash": "h0"})
        cs.state.apply_tx(t)
        node = _InfoNode(cs)
        if hide is not None:
            node.settings.set(settings_mod.HIDE_ADDRESS_PUBLICLY, hide)
        return node

    def test_nickname_shown_on_private_dashboard(self):
        node = self._node_with_nickname()
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        html = client.get("/").get_data(as_text=True)
        assert "Al" in html
        assert node.addr in html

    def test_api_info_carries_the_nickname_on_private_app(self):
        node = self._node_with_nickname()
        client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/info").get_json()
        assert data["nick"] == "Al"

    def test_nickname_hidden_alongside_the_address_on_public_app_by_default(self):
        node = self._node_with_nickname()
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/info").get_json()
        assert data["address"] is None
        assert data["nick"] is None
        html = client.get("/").get_data(as_text=True)
        assert "Al" not in html

    def test_nickname_shown_on_public_app_when_hiding_is_off(self):
        node = self._node_with_nickname(hide=False)
        client = api.create_app(node, peerpool_mod.PeerPool()).test_client()
        data = client.get("/api/info").get_json()
        assert data["nick"] == "Al"


def test_flatten_threads_newest_thread_first_replies_by_score():
    import api
    entries = [{"ref6": "a", "reply_ref": None},
               {"ref6": "c", "reply_ref": "a"},
               {"ref6": "d", "reply_ref": "a"},
               {"ref6": "e", "reply_ref": "c"},
               {"ref6": "b", "reply_ref": None},
               {"ref6": "z", "reply_ref": "zzzzzz"}]  # unknown parent: own thread
    score = {"c": 1, "d": 5}.get
    flat, starts = api._flatten_threads(entries, lambda r: score(r["ref6"], 0))
    assert len(starts) == 3
    assert [(e["ref6"], d) for e, d in flat] == [
        ("z", 0), ("b", 0), ("a", 0), ("d", 1), ("c", 1), ("e", 2)]
    assert [flat[i][0]["ref6"] for i in starts] == ["z", "b", "a"]


def test_board_reply_memos_round_trip_into_nested_render():
    """Replies built the way the compose path builds them (build_board_body
    with the parent's tx-hash prefix) come back nested under that parent."""
    import re
    import tx as tx_mod
    TAG = tx_mod.BOARD_MEMO_TAG
    cs = ChainState.from_genesis()
    for i in range(3):
        seed_balance(cs.state, i, 1000.0)
    posts = {}

    def post(name, msg, parent=None, who=0):
        ref = tx_mod.tx_hash(posts[parent])[:tx_mod.REPLY_REF_LEN] if parent else None
        t = {"from": address(who), "nonce": len(posts) + 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": TAG + api.build_board_body(msg, reply_ref=ref)}
        posts[name] = t
        cs.chain.append({"height": len(cs.chain), "timestamp": 1000 + len(posts),
                         "transactions": [t], "hash": f"h{len(cs.chain)}"})

    post("A", "root A")
    post("B", "root B")
    post("A1", "reply A1", "A", 1)
    post("A1a", "reply to A1", "A1", 2)
    cs.state.total_board_posts = len(posts)
    client = api.create_private_app(_FakeNode(cs), peerpool_mod.PeerPool()).test_client()
    html = client.get("/api/board/fragment?page=1").get_data(as_text=True)
    margins = {}
    for m in re.finditer(r'<div class="rc-root[^"]*"[^>]*?(?:--depth: (\d+))?">'
                         r'.*?<div class="rc-text">(.*?)</div>', html, re.S):
        margins[m.group(2).strip()] = float(m.group(1) or 0)
    assert margins == {"root B": 0, "root A": 0, "reply A1": 1, "reply to A1": 2}
    assert (html.index("root B") < html.index("root A")
            < html.index("reply A1") < html.index("reply to A1"))


def test_board_post_form_encodes_reply_ref_and_renders_nested(tmp_path):
    """The real compose path: POST /board with a reply_ref, signed by a
    real keyfile, lands in the mempool as a reply and renders nested."""
    import re
    import time
    import crypto as crypto_mod
    import tx as tx_mod
    from params import TICKS_PER_LAPSE
    PASS = "correct horse battery staple"
    sk, pk = crypto_mod.generate_keypair()
    keyfile = str(tmp_path / "node.key")
    crypto_mod.save_key(keyfile, sk, pk, PASS)
    addr = crypto_mod.public_key_to_address(pk)
    cs = ChainState.from_genesis()
    cs.state.credit(addr, 1000 * TICKS_PER_LAPSE)

    class SigningNode(_FakeNode):
        pk_hex = pk.hex()

        def __init__(self):
            super().__init__(cs, addr=addr)
            self.keyfile = keyfile

        def build_and_sign_tx(self, outs, fee=0, passphrase=None, memo=""):
            kek = crypto_mod.derive_kek(self.keyfile, passphrase)
            nonce = max(cs.state.get_nonce(addr), self.mempool.pending_nonce(addr)) + 1
            s = crypto_mod.decrypt_secret_key(self.keyfile, kek=kek)
            return tx_mod.create(addr, self.pk_hex, outs, nonce, fee, s, memo=memo), fee

        def submit_tx_from_api(self, t, timeout=5):
            ok, why = self.mempool.add(t)
            return ok, (tx_mod.tx_hash(t) if ok else why)

    node = SigningNode()
    root = {"from": addr, "pubkey": pk.hex(), "nonce": 1, "fee": 100,
            "outputs": [{"to": "1" * 40, "amount": 1}],
            "memo": tx_mod.BOARD_MEMO_TAG + "the root"}
    cs.chain.append({"height": 1, "timestamp": int(time.time()),
                     "transactions": [root], "hash": "h1"})
    cs.state.apply_tx(root)
    app = api.create_private_app(node, peerpool_mod.PeerPool())
    client = app.test_client()
    ref = tx_mod.tx_hash(root)[:tx_mod.REPLY_REF_LEN]
    from tests.browser import submit
    html = submit(client, "/board", message="the reply", passphrase=PASS,
                  reply_ref=ref).get_data(as_text=True)
    pending = list(node.mempool.all_txs())
    assert [t["memo"] for t in pending] == [tx_mod.BOARD_MEMO_TAG + f"[r:{ref}]the reply"]
    assert html.index("the root") < html.index("the reply")
    assert "--depth: 1" in html


class TestBoardCachingAndQuotes:
    def _setup(self):
        import tx as tx_mod
        cs = ChainState.from_genesis()
        t = {"from": address(0), "nonce": 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + "first post"}
        cs.chain.append({"height": 1, "timestamp": 1000, "transactions": [t], "hash": "h1"})
        node = _FakeNode(cs)
        return cs, node, api.create_private_app(node, peerpool_mod.PeerPool()).test_client()

    def test_unchanged_poll_gets_304_and_new_block_busts_the_cache(self):
        import tx as tx_mod
        cs, node, client = self._setup()
        first = client.get("/api/board/fragment?page=1")
        assert first.status_code == 200 and "first post" in first.get_data(as_text=True)
        etag = first.headers["ETag"]
        assert client.get("/api/board/fragment?page=1",
                          headers={"If-None-Match": etag}).status_code == 304
        t = {"from": address(1), "nonce": 1, "fee": 100,
             "outputs": [{"to": "1" * 40, "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + "second post"}
        cs.chain.append({"height": 2, "timestamp": 1001, "transactions": [t], "hash": "h2"})
        again = client.get("/api/board/fragment?page=1", headers={"If-None-Match": etag})
        assert again.status_code == 200 and "second post" in again.get_data(as_text=True)

    def test_quote_only_when_parent_is_not_in_the_feed(self):
        import tx as tx_mod
        cs, node, client = self._setup()
        parent_ref = tx_mod.tx_hash(cs.chain[1]["transactions"][0])[:api.REPLY_REF_LEN]
        nested = {"from": address(1), "nonce": 1, "fee": 100,
                  "outputs": [{"to": "1" * 40, "amount": 1}],
                  "memo": tx_mod.BOARD_MEMO_TAG + api.build_board_body("nested one", reply_ref=parent_ref)}
        orphan = {"from": address(2), "nonce": 1, "fee": 100,
                  "outputs": [{"to": "1" * 40, "amount": 1}],
                  "memo": tx_mod.BOARD_MEMO_TAG + api.build_board_body("orphan one", reply_ref="abcdef")}
        for i, t in enumerate((nested, orphan)):
            cs.chain.append({"height": 2 + i, "timestamp": 1001 + i, "transactions": [t], "hash": f"h{2+i}"})
        html = client.get("/api/board/fragment?page=1").get_data(as_text=True)
        assert html.count('class="rc-replyQuote"') == 1
        assert "&#8627; an earlier post" in html
