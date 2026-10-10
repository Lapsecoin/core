"""The light client: the same wallet pages, read from another node.

A real full-node app is the "remote node", reached through an in-process
session instead of a socket, so RemoteReader, the endpoints it calls, and
the pages it feeds are all the real code.
"""

import gzip
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import crypto
import local_reader
import peerpool as peerpool_mod
import remote_reader
import settings as settings_mod
import tx as tx_mod
from board_view import VOTE_UP_TAG
from chainstate import ChainState
from light_app import create_light_app
from node import NodeView
from params import TICKS_PER_LAPSE
from remote_reader import NodeSet, RemoteError, RemoteReader
from tests.fixtures import address
from tests.browser import submit
from tests.test_api import _MemoryMeta
import mempool as mempool_mod
from wallet import Wallet

PASS = "correct horse battery staple"
SRC = os.path.join(os.path.dirname(__file__), "..", "src")


# ---------------------------------------------------------------------------
# A full node to ask, and a session that reaches it without a socket
# ---------------------------------------------------------------------------

class _TxIndex:
    """Storage's address index, answered by walking the chain."""

    def __init__(self, cs):
        self.cs = cs

    def get_tx_heights_for_addr(self, addr):
        return [(blk["height"], tx_mod.tx_hash(t))
                for blk in self.cs.chain for t in blk.get("transactions", [])
                if t.get("from") == addr
                or any(o["to"] == addr for o in t.get("outputs", []))]


class _FullNode:
    """A node with real validation on submit, the way Node.submit_tx does it."""

    def __init__(self, cs):
        self.cs = cs
        self.storage = _TxIndex(cs)
        self.mempool = mempool_mod.Mempool()
        self.view = NodeView(cs)
        self.addr = address(0)
        self.settings = settings_mod.Settings(_MemoryMeta())

    def submit_tx_from_api(self, t, timeout=5):
        probe = self.mempool.probe_state_for(t.get("from"), self.cs.state)
        ok, err = tx_mod.validate(t, probe)
        if not ok:
            return False, err
        return self.mempool.add(t)


class _Resp:
    """The corner of requests.Response the reader uses, gzip and all."""

    def __init__(self, r):
        self.status_code = r.status_code
        self.headers = r.headers
        body = r.data
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.content = body

    def json(self):
        return json.loads(self.content)


class _Session:
    """requests.Session look-alike over a Flask test client."""

    def __init__(self, client):
        self.client = client
        self.headers = {"Accept-Encoding": "gzip, deflate"}
        self.proxies = {}
        self.log = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        parts = urlsplit(url)
        path = parts.path
        self.log.append((method, path, json, params, f"{parts.scheme}://{parts.netloc}"))
        h = dict(self.headers)
        h.update(headers or {})
        return _Resp(self.client.open(path, method=method, query_string=params,
                                      json=json, headers=h))


@pytest.fixture
def world(tmp_path):
    """A wallet with funds, a chain with one post on it, a full node, and a
    reader that talks to that node."""
    sk, pk = crypto.generate_keypair()
    keyfile = str(tmp_path / "wallet.key")
    crypto.save_key(keyfile, sk, pk, PASS)
    wallet = Wallet(keyfile, pk)

    cs = ChainState.from_genesis()
    cs.state.credit(wallet.addr, 1000 * TICKS_PER_LAPSE)
    root = {"from": address(0), "nonce": 1, "fee": 100,
            "outputs": [{"to": crypto.burn_address(), "amount": 1}],
            "memo": tx_mod.BOARD_MEMO_TAG + "the root post"}
    cs.chain.append({"height": 1, "timestamp": int(time.time()),
                     "transactions": [root], "hash": "h1"})
    payment = {"from": address(0), "nonce": 2, "fee": 100,
               "outputs": [{"to": wallet.addr, "amount": 5000}]}
    cs.chain.append({"height": 2, "timestamp": int(time.time()),
                     "transactions": [payment], "hash": "h2"})
    node = _FullNode(cs)
    app = api.create_app(node, peerpool_mod.PeerPool())
    session = _Session(app.test_client())
    reader = RemoteReader(["http://node.test"], refresh=0, session=session)
    return type("World", (), dict(wallet=wallet, cs=cs, node=node, app=app,
                                  session=session, reader=reader, root=root,
                                  local=local_reader.LocalReader(node)))


def _csrf(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"',
                     client.get(path).get_data(as_text=True)).group(1)


# ---------------------------------------------------------------------------
# It stays light
# ---------------------------------------------------------------------------

def test_the_light_client_never_loads_the_full_node():
    """The point of the build: none of what makes a node heavy (the VDF, the
    chain database, the torrent and swap code) may be imported, directly or
    through anything the light client does import."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import light, light_app, remote_reader, wallet_ui, board_view, ui_common, wallet\n"
        "banned = ['chiavdf', 'peewee', 'libtorrent', 'cairosvg', 'pystray',\n"
        "          'block', 'vdf', 'storage', \n"
        "          'node', 'chainstate', 'state', 'mempool', 'gossip', 'syncer',\n"
        "          'discovery', 'peer_udp', 'api', 'local_reader', 'hardware_info']\n"
        "print('IMPORTED:' + ','.join(m for m in banned if m in sys.modules))\n" % SRC)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    # liboqs prints a line of its own on import, so the answer is the last one.
    line = [l for l in out.stdout.splitlines() if l.startswith("IMPORTED:")][-1]
    imported = line[len("IMPORTED:"):]
    assert imported == "", f"light client imported: {imported}"
    assert not any(m in imported.split(",") for m in (
        "chiavdf", "peewee", "libtorrent", "block", "vdf", "storage",
        "node", "chainstate", "api", "local_reader")), imported


# ---------------------------------------------------------------------------
# The same answers, local or remote
# ---------------------------------------------------------------------------

class TestReadersAgree:
    def test_account(self, world):
        addr = world.wallet.addr
        kw = dict(nick="nobody", fees=True)
        assert world.reader.account(addr, **kw) == world.local.account(addr, **kw)

    def test_account_without_an_address_still_has_the_floor(self, world):
        got = world.reader.account(None)
        assert got == {"board_floor": tx_mod.board_fee_floor(0)}

    def test_address_page(self, world):
        addr = world.wallet.addr
        assert world.reader.address_page(addr, 1) == json.loads(
            json.dumps(world.local.address_page(addr, 1)))

    def test_board_page(self, world):
        remote = world.reader.board_page(1)
        local = json.loads(json.dumps(world.local.board_page(1)))
        assert remote == local
        assert [r["text"] for r in remote["rows"]] == ["the root post"]

    def test_fee_estimate(self, world):
        assert world.reader.fee_estimate() == world.local.fee_estimate()

    def test_a_board_row_carries_no_transaction(self, world):
        """A transaction is a public key and a signature, over two kilobytes
        of hex the page never shows. A row is its text and who wrote it."""
        row = world.reader.board_page(1)["rows"][0]
        assert "tx" not in row and "signature" not in json.dumps(row)
        assert set(row) == {"height", "ts", "hash", "pending", "depth", "from", "text",
                            "reply_ref", "edited", "deleted"}

    def test_a_bad_address_is_refused(self, world):
        r = world.app.test_client().get("/api/state?addr=nonsense")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# The same pages, local or remote
# ---------------------------------------------------------------------------

class TestLightPages:
    def _light(self, world):
        return create_light_app(world.reader, world.wallet).test_client()

    def test_board_fragment_is_what_the_full_node_renders(self, world):
        """Same address, same chain, same code: byte for byte the same rows."""
        world.node.addr = world.wallet.addr
        full = api.create_private_app(world.node, peerpool_mod.PeerPool()).test_client()
        light = self._light(world)
        a = full.get("/api/board/fragment?page=1").get_data(as_text=True)
        b = light.get("/api/board/fragment?page=1").get_data(as_text=True)
        assert "the root post" in a
        assert a == b

    def test_board_page_renders_with_a_compose_box(self, world):
        html = self._light(world).get("/board").get_data(as_text=True)
        assert "the root post" in html
        assert 'id="board-compose"' in html

    def test_nav_offers_only_what_the_light_client_has(self, world):
        html = self._light(world).get("/board").get_data(as_text=True)
        nav = html[html.index('<nav class="mainnav" id="mainnav">'):html.index("</nav>")]
        for present in ("/address", "/send", "/board"):
            assert f'href="{present}"' in nav
        for absent in ("/explorer", "/blocks", "/network", "/mempool", "/settings", "/odds"):
            assert f'href="{absent}"' not in nav

    def test_balance_page_shows_the_wallet(self, world):
        html = self._light(world).get("/address").get_data(as_text=True)
        assert "1000 LAPSE" in html
        assert "received" in html and ">5000<" in html

    def test_send_page_is_lapse_only(self, world):
        html = self._light(world).get("/send").get_data(as_text=True)
        assert "lapse-panel" in html
        assert "asset-picker" not in html

    def test_every_page_says_what_it_has_cost(self, world):
        html = self._light(world).get("/board").get_data(as_text=True)
        assert "used this session" in html

    def test_an_unreachable_node_is_a_page_not_a_crash(self, world):
        def boom(*a, **k):
            raise remote_reader.requests.ConnectionError("down")
        world.session.request = boom
        world.reader.invalidate()
        client = self._light(world)
        r = client.get("/board")
        assert r.status_code == 502 and "No node could be reached" in r.get_data(as_text=True)
        r = client.get("/api/fees")
        assert r.status_code == 502 and r.get_json()["ok"] is False


class TestLightSpends:
    """What the light client signs must be accepted by a real node's own
    validation: signature, nonce, fee floor, balance."""

    def _post(self, world, **form):
        client = create_light_app(world.reader, world.wallet).test_client()
        return client, submit(client, "/board", passphrase=PASS, **form)

    def test_a_board_post_reaches_the_node(self, world):
        client, resp = self._post(world, message="hello from light")
        pending = world.node.mempool.all_txs()
        assert [t["memo"] for t in pending] == [tx_mod.BOARD_MEMO_TAG + "hello from light"]
        assert pending[0]["from"] == world.wallet.addr
        assert "hello from light" in resp.get_data(as_text=True)

    def test_a_reply_nests_under_its_parent(self, world):
        ref = tx_mod.tx_hash(world.root)[:tx_mod.REPLY_REF_LEN]
        _client, resp = self._post(world, message="the reply", reply_ref=ref)
        assert [t["memo"] for t in world.node.mempool.all_txs()] == [
            tx_mod.BOARD_MEMO_TAG + f"[r:{ref}]the reply"]
        assert "--depth: 1" in resp.get_data(as_text=True)

    def test_a_vote_is_accepted(self, world):
        ref = tx_mod.tx_hash(world.root)[:tx_mod.REPLY_REF_LEN]
        client = create_light_app(world.reader, world.wallet).test_client()
        token = _csrf(client, "/board")
        client.post("/board/vote", data={"csrf_token": token, "passphrase": PASS,
                                         "ref": ref, "dir": "+"})
        assert [t["memo"] for t in world.node.mempool.all_txs()] == [VOTE_UP_TAG + ref]

    def test_a_send_is_accepted(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        resp = submit(client, "/send", passphrase=PASS, outputs=f"{address(3)},1000")
        assert "Sent." in resp.get_data(as_text=True)
        assert [t["outputs"][0]["amount"] for t in world.node.mempool.all_txs()] == [1000]

    def test_two_sends_in_a_row_use_consecutive_nonces(self, world):
        """The second must not sign with a nonce held over from before the
        first: a stale one is rejected by the node."""
        world.reader.refresh = 3600           # would hold the first answer forever
        client = create_light_app(world.reader, world.wallet).test_client()
        for _ in range(2):
            resp = submit(client, "/send", passphrase=PASS, outputs=f"{address(3)},1000")
            assert "Sent." in resp.get_data(as_text=True)
        assert sorted(t["nonce"] for t in world.node.mempool.all_txs()) == [1, 2]

    def test_a_wrong_passphrase_signs_nothing(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        resp = submit(client, "/send", passphrase="wrong", outputs=f"{address(3)},1000")
        assert "Error" in resp.get_data(as_text=True)
        assert world.node.mempool.all_txs() == []

    def test_a_post_without_the_csrf_token_is_refused(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        client.post("/board", data={"passphrase": PASS, "message": "sneaky"})
        assert world.node.mempool.all_txs() == []

    def test_the_node_never_sees_the_passphrase_or_the_key(self, world):
        self._post(world, message="private")
        sk = crypto.decrypt_secret_key(world.wallet.keyfile, passphrase=PASS)
        sent = json.dumps(world.session.log)
        assert "private" in sent                  # the post itself did go out
        assert PASS not in sent
        assert sk.hex() not in sent
        assert "passphrase" not in sent


# ---------------------------------------------------------------------------
# It stays cheap
# ---------------------------------------------------------------------------

class TestFrugal:
    def _reader(self, world, refresh):
        return RemoteReader(["http://node.test"], refresh=refresh, session=world.session)

    def test_repeated_asks_inside_the_window_cost_one_request(self, world):
        r = self._reader(world, refresh=3600)
        before = len(world.session.log)
        for _ in range(5):
            r.board_page(1)
            r.account(world.wallet.addr, fees=True)
            r.fee_estimate()
        asked = [e[1] for e in world.session.log[before:] if e[1] != "/api/peers/http"]
        assert sorted(asked) == ["/api/board/page", "/api/fees", "/api/state"]

    def test_an_unchanged_board_costs_a_304_and_no_body(self, world):
        r = self._reader(world, refresh=0)
        r.board_page(1)
        used = r.bytes_in
        r.board_page(1)
        assert r.bytes_in - used < 300     # headers only, no body
        assert r.board_page(1)["rows"]

    def test_a_changed_board_is_fetched_again(self, world):
        r = self._reader(world, refresh=0)
        assert len(r.board_page(1)["rows"]) == 1
        t = {"from": address(0), "nonce": 3, "fee": 100,
             "outputs": [{"to": crypto.burn_address(), "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + "a second post"}
        world.cs.chain.append({"height": 3, "timestamp": int(time.time()),
                               "transactions": [t], "hash": "h3"})
        assert len(r.board_page(1)["rows"]) == 2

    def test_responses_are_gzipped_and_much_smaller(self, world):
        for i in range(60):
            t = {"from": address(0), "nonce": 2 + i, "fee": 100,
                 "outputs": [{"to": crypto.burn_address(), "amount": 1}],
                 "memo": tx_mod.BOARD_MEMO_TAG + f"post number {i} with some words in it"}
            world.cs.chain.append({"height": 3 + i, "timestamp": 1000 + i,
                                   "transactions": [t], "hash": f"h{i}"})
        client = world.app.test_client()
        plain = client.get("/api/board/page?chunks=5")
        packed = client.get("/api/board/page?chunks=5",
                            headers={"Accept-Encoding": "gzip"})
        assert packed.headers["Content-Encoding"] == "gzip"
        assert len(packed.data) < len(plain.data) / 2
        assert gzip.decompress(packed.data) == plain.data

    def test_the_node_being_down_serves_what_was_held(self, world):
        r = self._reader(world, refresh=0)
        first = r.board_page(1)

        def boom(*a, **k):
            raise remote_reader.requests.ConnectionError("down")
        world.session.request = boom
        assert r.board_page(1) == first

    def test_usage_counts_bytes(self, world):
        r = self._reader(world, refresh=0)
        r.fee_estimate()
        assert r.usage()["bytes"] > 0 and r.usage()["requests"] >= 1


# ---------------------------------------------------------------------------
# Which node, and what happens when one fails
# ---------------------------------------------------------------------------

class TestNodeSet:
    def test_seeds_come_before_discovered_nodes(self):
        ns = NodeSet(["https://seed.example"])
        ns.add(["1.2.3.4:8333"])
        assert ns.pick() == "https://seed.example"

    def test_a_discovered_node_is_http(self):
        ns = NodeSet([])
        ns.add(["1.2.3.4:8333"])
        assert ns.all() == ["http://1.2.3.4:8333"]

    def test_it_sticks_to_a_working_node(self):
        ns = NodeSet(["http://a", "http://b", "http://c"])
        first = ns.pick()
        assert all(ns.pick() == first for _ in range(20))

    def test_a_failing_node_is_dropped_for_another(self):
        ns = NodeSet(["http://a", "http://b"])
        first = ns.pick()
        ns.strike(first)
        assert ns.pick() != first

    def test_a_struck_node_sits_out_a_cooldown(self):
        ns = NodeSet(["http://a"])
        ns.strike("http://a")
        assert ns.pick() is None

    def test_a_good_answer_forgives(self):
        ns = NodeSet(["http://a"])
        ns.strike("http://a")
        ns.ok("http://a")
        assert ns.pick() == "http://a"

    def test_the_list_is_bounded(self):
        ns = NodeSet([])
        ns.add([f"10.0.0.{i}:8333" for i in range(200)])
        assert len(ns.all()) == remote_reader.MAX_NODES

    def test_failover_reaches_a_second_node(self, world):
        calls = []

        class Flaky(_Session):
            def request(self, method, url, **kw):
                calls.append(url)
                if url.startswith("http://bad"):
                    raise remote_reader.requests.ConnectionError("down")
                return super().request(method, url, **kw)

        r = RemoteReader(["http://bad.test", "http://good.test"], refresh=0,
                         session=Flaky(world.app.test_client()))
        assert r.fee_estimate() == world.local.fee_estimate()

    def test_all_nodes_down_says_so(self, world):
        class Dead(_Session):
            def request(self, *a, **k):
                raise remote_reader.requests.ConnectionError("down")

        r = RemoteReader(["http://a.test"], session=Dead(world.app.test_client()))
        with pytest.raises(RemoteError, match="No node could be reached"):
            r.fee_estimate()

    def test_discovery_learns_http_nodes_and_remembers_them(self, world, tmp_path):
        cache = str(tmp_path / "nodes.json")
        r = RemoteReader(["http://node.test"], refresh=0, session=world.session,
                         cache_file=cache)
        r.fee_estimate()
        assert os.path.exists(cache)
        assert json.load(open(cache))[0] == "http://node.test"
        again = RemoteReader(["http://node.test"], session=world.session, cache_file=cache)
        assert again.nodes.all() == ["http://node.test"]


class TestPeersHttpEndpoint:
    def test_lists_http_reachable_peers_and_self_only(self, world):
        pool = peerpool_mod.PeerPool()
        pool.add("8.8.8.8:8333")
        pool.add("8.8.4.4:8333")
        pool.set_http_reachable("8.8.8.8:8333", True)
        pool.set_http_reachable("8.8.4.4:8333", False)
        app = api.create_app(world.node, pool)
        nodes = app.test_client().get("/api/peers/http").get_json()["nodes"]
        assert "8.8.8.8:8333" in nodes
        assert "8.8.4.4:8333" not in nodes


# ---------------------------------------------------------------------------
# What the node learns
# ---------------------------------------------------------------------------

class TestWhatTheNodeLearns:
    """A node that is asked something can tie it to the asker's IP. So what
    names the wallet's address is kept to what needs it, and reading the
    board is not among it."""

    def _everything_sent(self, world):
        return json.dumps(world.session.log)

    def test_opening_the_app_and_reading_the_board_names_nobody(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        world.session.log.clear()
        assert client.get("/").headers["Location"].endswith("/board")
        client.get("/board")
        client.get("/api/board/fragment?page=1")
        client.get("/api/board/fragment?page=2")
        assert world.session.log, "nothing was asked at all"
        assert world.wallet.addr not in self._everything_sent(world)

    def test_the_balance_page_does_name_it_because_that_is_the_question(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        world.session.log.clear()
        client.get("/address")
        assert world.wallet.addr in self._everything_sent(world)

    def test_asking_for_a_profile_asks_the_node_nothing(self, world):
        world.reader.board_page(1)
        world.session.log.clear()
        world.reader.profile(world.wallet.addr)
        assert world.session.log == []

    def test_the_compose_box_still_shows_your_own_icon_from_the_board_itself(self, world):
        sk = crypto.decrypt_secret_key(world.wallet.keyfile, passphrase=PASS)
        mine = tx_mod.create(world.wallet.addr, world.wallet.pk_hex,
                             [{"to": crypto.burn_address(), "amount": 1}], 1, 100, sk,
                             memo=tx_mod.BOARD_MEMO_TAG + "[p:5:]my post, my icon")
        world.cs.chain.append({"height": 3, "timestamp": int(time.time()),
                               "transactions": [mine], "hash": "h3"})
        client = create_light_app(world.reader, world.wallet).test_client()
        world.session.log.clear()
        html = client.get("/board").get_data(as_text=True)
        assert 'data-own-icon-idx="5"' in html
        assert world.wallet.addr not in self._everything_sent(world)

    def test_posting_does_name_it_because_the_transaction_carries_it(self, world):
        client = create_light_app(world.reader, world.wallet).test_client()
        world.session.log.clear()
        submit(client, "/board", passphrase=PASS, message="hi")
        assert world.wallet.addr in self._everything_sent(world)


class TestHowItReachesTheNode:
    def test_a_direct_plain_http_node_says_so(self, world):
        world.reader.fee_estimate()
        assert world.reader.usage()["route"] == "direct, not encrypted"
        html = create_light_app(world.reader, world.wallet).test_client() \
            .get("/board").get_data(as_text=True)
        assert "direct, not encrypted" in html

    def test_an_https_node_says_so(self, world):
        r = RemoteReader(["https://secure.test"], session=world.session)
        r.nodes.pick()
        assert r.usage()["route"] == "direct, encrypted"

    def test_a_proxy_says_so(self, world):
        r = RemoteReader(["http://node.test"], proxy="http://127.0.0.1:8118",
                         session=world.session)
        assert r.usage()["route"] == "via proxy"

    def test_no_proxy_means_none(self):
        assert remote_reader.resolve_proxy(None) is None
        assert remote_reader.resolve_proxy("") is None

    def test_names_are_resolved_by_the_proxy_not_by_this_machine(self):
        assert remote_reader.resolve_proxy("socks5://127.0.0.1:9050").startswith("socks5h://")

    def test_each_run_gets_a_circuit_of_its_own(self):
        a = remote_reader.resolve_proxy("socks5h://127.0.0.1:9050")
        b = remote_reader.resolve_proxy("socks5h://127.0.0.1:9050")
        assert a != b and a.endswith("@127.0.0.1:9050") and "lapse" in a

    def test_a_login_the_user_chose_is_left_alone(self):
        url = "socks5h://me:secret@127.0.0.1:9050"
        assert remote_reader.resolve_proxy(url) == url

    def test_an_http_proxy_is_passed_through(self):
        assert remote_reader.resolve_proxy("http://127.0.0.1:8118") == "http://127.0.0.1:8118"

    def test_the_reader_actually_uses_the_proxy_it_resolved(self, world):
        r = RemoteReader(["http://node.test"], proxy="socks5://127.0.0.1:9050",
                         session=world.session, refresh=0)
        r._discovered_at = time.time()
        r.fee_estimate()
        assert set(world.session.proxies.values()) == {r.proxy}
        assert r.proxy.startswith("socks5h://")


class TestStartup:
    def _out(self, monkeypatch, tmp_path, capsys, *argv):
        import light
        monkeypatch.setenv("LAPSECOIN_PASSPHRASE", PASS)
        monkeypatch.setattr(light, "_serve", lambda *a, **k: None)
        monkeypatch.chdir(tmp_path)
        light.main(["--keyfile", str(tmp_path / "k.json"), "--no-browser", *argv])
        return capsys.readouterr().out

    def test_it_says_what_names_the_address_and_how_it_is_kept_private(self, monkeypatch, tmp_path, capsys):
        out = self._out(monkeypatch, tmp_path, capsys)
        assert "Looking at the board names nobody" in out
        for route in ("Tor", "relay", "straight to a node"):
            assert route in out

    @pytest.mark.parametrize("value", ["auto", "none", "http://127.0.0.1:8118",
                                       "socks5h://127.0.0.1:9050"])
    def test_proxy_takes_auto_none_or_a_url(self, monkeypatch, tmp_path, capsys, value):
        self._out(monkeypatch, tmp_path, capsys, "--proxy", value)

    @pytest.mark.parametrize("value", ["tor", "yes", "9050", ""])
    def test_anything_else_is_refused_with_the_choices(self, monkeypatch, tmp_path, value):
        with pytest.raises(SystemExit, match="auto, none, or a proxy URL"):
            self._out(monkeypatch, tmp_path, None, "--proxy", value)

    def test_tor_is_found_with_no_option_at_all(self, monkeypatch, tmp_path, capsys):
        """The default is auto: nothing to ask for."""
        import light
        assert light.build_parser().parse_args([]).proxy == "auto"

    def test_there_is_no_flag_to_allow_plain_http_any_more(self):
        import light
        with pytest.raises(SystemExit):
            light.build_parser().parse_args(["--allow-plain-http"])


class _OnlyAnswers(_Session):
    """Answers for some hosts and refuses the rest, as a down node would."""

    def __init__(self, client, up):
        super().__init__(client)
        self.up = up

    def request(self, method, url, **kw):
        if urlsplit(url).netloc not in self.up:
            raise remote_reader.requests.ConnectionError("down")
        return super().request(method, url, **kw)


class TestNothingIsRefused:
    """Where the wallet's address goes is a preference, never a condition:
    every feature works whatever nodes there are, and when only plain http
    is left the header says so."""
    PLAIN = "9.9.9.9:8333"
    PLAIN_URL = "http://9.9.9.9:8333"

    def _reader(self, world, session=None, seeds=("https://secure.test",), plain=(), **kw):
        r = RemoteReader(list(seeds), session=session or world.session, refresh=0, **kw)
        r.nodes.add(list(plain))
        r._discovered_at = time.time()           # do not go looking in these tests
        return r

    def test_an_encrypted_node_is_preferred_for_the_address(self, world):
        r = self._reader(world, plain=[self.PLAIN])
        r.account(world.wallet.addr, fees=True)
        r.address_page(world.wallet.addr, 1)
        assert {e[4] for e in world.session.log} == {"https://secure.test"}

    def test_with_the_encrypted_node_down_it_still_works_through_the_plain_one(self, world):
        session = _OnlyAnswers(world.app.test_client(), up={"9.9.9.9:8333"})
        r = self._reader(world, session, plain=[self.PLAIN])
        assert r.account(world.wallet.addr, fees=True)["balance"] > 0
        assert r.address_page(world.wallet.addr, 1)["rows"]
        assert {e[4] for e in session.log} == {self.PLAIN_URL}

    def test_and_the_header_says_it_was_not_encrypted(self, world):
        session = _OnlyAnswers(world.app.test_client(), up={"9.9.9.9:8333"})
        r = self._reader(world, session, plain=[self.PLAIN])
        client = create_light_app(r, world.wallet).test_client()
        client.get("/address")
        assert "direct, not encrypted" in client.get("/board").get_data(as_text=True)

    def test_every_feature_works_with_only_a_plain_node(self, world):
        session = _OnlyAnswers(world.app.test_client(), up={"9.9.9.9:8333"})
        r = self._reader(world, session, plain=[self.PLAIN])
        client = create_light_app(r, world.wallet).test_client()
        for path in ("/board", "/api/board/fragment?page=1", "/api/fees", "/address", "/send"):
            assert client.get(path).status_code == 200, path
        before = len(world.node.mempool.all_txs())
        for path, form in (("/board", {"message": "hello"}),
                           ("/send", {"outputs": f"{address(3)},1000"}),
                           ("/board/vote", {"ref": "abcdef", "dir": "+"}),
                           ("/board/delete", {"ref": "abcdef"})):
            submit(client, path, page="/board" if path.startswith("/board") else path,
                   passphrase=PASS, **form)
            assert len(world.node.mempool.all_txs()) == before + 1, path
            before += 1

    def test_a_node_the_user_named_is_preferred_even_over_plain_http(self, world):
        r = self._reader(world, seeds=["http://mine.test:8333"], plain=[self.PLAIN])
        r.account(world.wallet.addr)
        assert {e[4] for e in world.session.log} == {"http://mine.test:8333"}

    def test_https_peers_are_tried_before_plain_ones_of_the_same_kind(self):
        ns = NodeSet([])
        ns.add(["https://a.example.org", "http://9.9.9.9:8333"])
        assert ns.pick() == "https://a.example.org"

    def test_a_safe_node_wins_for_the_address_and_any_node_serves_a_read(self):
        ns = NodeSet(["http://seed.test"])
        ns.add(["https://peer.example.org"])
        assert ns.pick(for_address=True) == ns.pick()      # one node for everything


class TestWhatDiscoveryMayAdd:
    def test_only_acceptable_nodes_are_kept(self):
        got = RemoteReader._acceptable([
            "https://ok.example.org", "9.9.9.9:8333", "http://8.8.4.4:8333",
            "https://10.0.0.1", "https://localhost", "http://evil.example.org/x",
            "10.0.0.5:8333", "127.0.0.1:8333", "192.168.1.1:8333", "169.254.1.1:80",
            "9.9.9.9:0", "9.9.9.9:99999", "9.9.9.9:nope", "nonsense",
            7, None, ["x"], {"a": 1}, b"9.9.9.9:8333", "",
        ])
        assert got == ["https://ok.example.org", "http://9.9.9.9:8333", "http://8.8.4.4:8333"]

    def test_https_comes_first(self):
        assert RemoteReader._acceptable(["9.9.9.9:8333", "https://a.example.org"]) == [
            "https://a.example.org", "http://9.9.9.9:8333"]

    def test_an_ipv6_peer_gets_a_valid_url(self):
        assert RemoteReader._acceptable(["2606:4700:4700::1111:8333"]) == [
            "http://[2606:4700:4700::1111]:8333"]

    def test_discovery_from_a_node_with_an_https_address_learns_it(self, world):
        from tests.test_api import _InfoNode
        app = api.create_app(_InfoNode(world.cs), peerpool_mod.PeerPool(),
                             public_url="https://node.example.org")
        session = _Session(app.test_client())
        r = RemoteReader(["http://node.test"], session=session, refresh=0)
        r.fee_estimate()
        assert "https://node.example.org" in r.nodes.all()

    def test_a_node_that_lies_in_its_list_adds_nothing_unsafe(self, world):
        class Liar(_Session):
            def request(self, method, url, **kw):
                if urlsplit(url).path == "/api/peers/http":
                    r = _Resp(self.client.get("/api/fees"))
                    r.content = json.dumps({"nodes": ["127.0.0.1:8335", "10.0.0.1:80", 5],
                                            "https": ["https://127.0.0.1", "http://x.org",
                                                      "https://fine.example.org"]}).encode()
                    return r
                return super().request(method, url, **kw)

        r = RemoteReader(["http://node.test"], session=Liar(world.app.test_client()), refresh=0)
        r.fee_estimate()
        assert sorted(r.nodes.all()) == ["http://node.test", "https://fine.example.org"]

    def test_a_poisoned_node_file_adds_nothing_unsafe(self, world, tmp_path):
        cache = tmp_path / "nodes.json"
        cache.write_text(json.dumps(["http://127.0.0.1:8335", "https://10.0.0.1", 3,
                                     "https://fine.example.org", "9.9.9.9:8333"]))
        r = RemoteReader(["http://node.test"], session=world.session, cache_file=str(cache))
        assert sorted(r.nodes.all()) == ["http://9.9.9.9:8333", "http://node.test",
                                         "https://fine.example.org"]

    def test_a_node_file_that_is_not_a_list_is_ignored(self, world, tmp_path):
        cache = tmp_path / "nodes.json"
        cache.write_text('{"a": 1}')
        r = RemoteReader(["http://node.test"], session=world.session, cache_file=str(cache))
        assert r.nodes.all() == ["http://node.test"]


class TestRouteForALocalNode:
    def test_a_node_on_this_machine_says_so(self, world):
        r = RemoteReader(["http://127.0.0.1:8333"], session=world.session, refresh=0)
        r._discovered_at = time.time()
        r.fee_estimate()
        assert r.usage()["route"] == "direct, this machine"


class TestStartupWarnsAboutAPlainNamedNode:
    def _out(self, monkeypatch, tmp_path, capsys, *argv):
        import light
        monkeypatch.setenv("LAPSECOIN_PASSPHRASE", PASS)
        monkeypatch.setattr(light, "_serve", lambda *a, **k: None)
        monkeypatch.chdir(tmp_path)
        light.main(["--keyfile", str(tmp_path / "k.json"), "--no-browser", *argv])
        return capsys.readouterr().out

    def test_naming_a_plain_remote_node_is_a_warning(self, monkeypatch, tmp_path, capsys):
        out = self._out(monkeypatch, tmp_path, capsys, "--node", "http://203.0.113.9:8333")
        assert "Warning: --node http://203.0.113.9:8333 is plain http" in out

    def test_naming_a_node_on_this_machine_is_not(self, monkeypatch, tmp_path, capsys):
        out = self._out(monkeypatch, tmp_path, capsys, "--node", "http://127.0.0.1:8333")
        assert "Warning: --node" not in out

    def test_naming_an_https_node_is_not(self, monkeypatch, tmp_path, capsys):
        out = self._out(monkeypatch, tmp_path, capsys, "--node", "https://node.example.org")
        assert "Warning: --node" not in out


class TestWhyNoNodeAnswered:
    def test_the_reason_survives_the_cooldown(self, world):
        """The first failure strikes the node, so the next request finds
        none to ask. It must still say what went wrong, not "no node"."""
        class Dead(_Session):
            def request(self, *a, **k):
                raise remote_reader.requests.ConnectionError("proxy refused")

        r = RemoteReader(["https://only.test"], session=Dead(world.app.test_client()))
        with pytest.raises(RemoteError, match="only.test: ConnectionError"):
            r.fee_estimate()
        with pytest.raises(RemoteError, match="only.test: ConnectionError"):
            r.fee_estimate()                 # now in cooldown, same reason

    def test_a_missing_socks_library_would_be_named_not_hidden(self, world):
        class NoSocks(_Session):
            def request(self, *a, **k):
                raise remote_reader.requests.exceptions.InvalidSchema(
                    "Missing dependencies for SOCKS support.")

        r = RemoteReader(["https://only.test"], session=NoSocks(world.app.test_client()))
        with pytest.raises(RemoteError, match="InvalidSchema"):
            r.fee_estimate()
