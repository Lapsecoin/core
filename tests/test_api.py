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
    def test_empty_mempool_returns_all_zero(self):
        node, _ = fresh()
        fees = api.fee_estimate(node)
        assert fees == {"pending": 0, "min": 0, "median": 0, "max": 0, "next_block": 0}

    def test_reports_pending_count_and_rates(self):
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=10)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["pending"] == 1
        # min/median/max should all equal the single tx's own fee-per-byte
        assert fees["min"] == fees["max"] == fees["median"]
        assert fees["min"] > 0

    def test_next_block_zero_when_mempool_below_capacity(self):
        """A mempool that easily fits in one block needs no minimum fee to
        clear the next block."""
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=0)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["next_block"] == 0

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


class TestOddsPage:
    """The self-build figure on /odds is sometimes measured and sometimes a
    calibration estimate, and the page has to say which. A node slower than
    the field never finishes an evaluation, so on exactly the node whose
    number comes from calibration, that number never becomes a measurement:
    presenting it as one would be permanently wrong there."""

    class _OddsNode:
        """The surface /odds and /api/odds touch, and nothing else."""

        def __init__(self, is_estimate):
            self.view = SimpleNamespace(chain=[
                {"height": 0, "timestamp": 1000, "vdf_iterations": 100},
                {"height": 1, "timestamp": 1120, "vdf_iterations": 100,
                 "builder": address(1)},
            ])
            self._is_estimate = is_estimate

        def own_vdf_median(self):
            return 90.0

        def own_vdf_is_estimate(self):
            return self._is_estimate

        def reorg_stats(self):
            return {"deepest": 0, "count": 0}

    def _client(self, is_estimate):
        return api.create_private_app(self._OddsNode(is_estimate),
                                      peerpool_mod.PeerPool()).test_client()

    def test_page_marks_a_calibrated_figure_as_an_estimate(self):
        html = self._client(True).get("/odds").get_data(as_text=True)
        assert ('<div class="stat-sub" id="own-median-sub">'
                'estimated, no build finished yet') in html

    def test_page_marks_a_measured_figure_as_measured(self):
        html = self._client(False).get("/odds").get_data(as_text=True)
        # The rendered element, not a loose substring: the page also ships
        # the refresh script, which carries both labels as literals.
        assert '<div class="stat-sub" id="own-median-sub">from completed builds' in html
        assert '<div class="stat-sub" id="own-median-sub">estimated' not in html

    def test_the_json_carries_the_same_distinction(self):
        assert self._client(True).get("/api/odds").get_json()["own_is_estimate"] is True
        assert self._client(False).get("/api/odds").get_json()["own_is_estimate"] is False


class TestPeersPage:
    """Smoke test the /peers route end to end: real PeerPool.snapshot()
    shape, self-row wiring, and the height/version columns all render
    without a template/route mismatch. There is deliberately no wallet
    column: a payout address is not something a peer tells us, and not
    something this node publishes (see Node._handle_inbound_alive)."""

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
        resp = self._client().get("/peers")
        assert resp.status_code == 200

    def test_peers_page_shows_self_and_peer_data(self):
        html = self._client().get("/peers").get_data(as_text=True)
        assert ">self<" in html  # falls back to "self" when own external addr is unknown
        assert "1.2.3.4:9000" in html
        assert "5.6.7.8:9000" in html
        assert "unknown" in html  # peer with no cached version yet
        assert "?" in html        # peer with no cached height yet
        assert "9.9.9.9:9000" in html
        assert "0.2.0" in html  # peer's confirmed version
        assert "Wallet" not in html, \
            "the peers page must not publish a payout address per IP"


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
        return app.test_client().get("/peers").get_data(as_text=True)

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


class TestLivenessOnThePeersPage:
    """What replaced the wallet column. A count, not a list: the addresses
    are payable and deliberately not attributable to any IP, and listing
    them beside a peer table is how the directory this replaced came
    about."""

    def _client(self, alive):
        cs = ChainState.from_genesis()
        seed_balance(cs.state, 0, 1000.0)
        node = _FakeNode(cs, alive=alive)
        pool = peerpool_mod.PeerPool()
        pool.add("1.2.3.4:9000")
        return api.create_private_app(node, pool).test_client()

    def test_the_count_is_shown(self):
        html = self._client({address(1), address(2)}).get("/peers").get_data(as_text=True)
        assert "2 nodes announced active" in html

    def test_it_reads_singular_for_one(self):
        html = self._client({address(1)}).get("/peers").get_data(as_text=True)
        assert "1 node announced active" in html

    def test_the_api_reports_a_count_and_never_the_addresses(self):
        resp = self._client({address(1), address(2)}).get("/api/peers")
        data = resp.get_json()
        assert data["alive_count"] == 2
        body = resp.get_data(as_text=True)
        assert address(1) not in body and address(2) not in body, \
            "announced addresses must not be published next to peer IPs"

    def test_no_peer_row_carries_a_payout_address(self):
        data = self._client({address(1)}).get("/api/peers").get_json()
        for peer in data["peers"]:
            assert "wallet" not in peer
        assert "wallet" not in data["self"]
