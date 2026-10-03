"""One-hop oblivious requests: the sealing, the two ends on a node, and a
client sending its wallet's address through a relay.

The "network" is in-process: each node is a real app, and requests to a node
(from the client, and from one node to another) are routed to its app.
"""

import base64
import gzip
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import crypto
import oblivious
import peerpool as peerpool_mod
import remote_reader
import tx as tx_mod
from chainstate import ChainState
from light_app import create_light_app
from oblivious import ObliviousService, open_response, pad, parse_key, seal_request, unpad
from params import TICKS_PER_LAPSE
from remote_reader import RemoteReader
from tests.browser import submit
from tests.fixtures import address
from tests.test_api import _InfoNode, _MemoryMeta
from tests.test_light import PASS, _FullNode, _Resp, _Session
from wallet import Wallet


# ---------------------------------------------------------------------------
# The sealing
# ---------------------------------------------------------------------------

class TestPadding:
    @pytest.mark.parametrize("size", [0, 1, 100, 1019, 1020, 2043, 4091, 8187])
    def test_round_trips_and_lands_on_a_bucket(self, size):
        data = os.urandom(size)
        padded = pad(data)
        assert len(padded) in oblivious.BUCKETS
        assert unpad(padded) == data

    def test_is_the_smallest_bucket_that_fits(self):
        assert len(pad(b"x" * 1020)) == 1024
        assert len(pad(b"x" * 1021)) == 2048

    def test_too_large_is_refused(self):
        with pytest.raises(ValueError):
            pad(b"x" * 8189)

    @pytest.mark.parametrize("bad", [b"", b"abc", b"\x00" * 1000, b"\x00\x00\xff\xff" + b"\x00" * 1020])
    def test_nonsense_is_refused(self, bad):
        with pytest.raises(ValueError):
            unpad(bad)


class TestKeys:
    def test_a_real_key_parses(self):
        assert len(parse_key(ObliviousService(_MemoryMeta()).public_b64)) == 32

    @pytest.mark.parametrize("bad", ["", "not base64!!", base64.b64encode(b"short").decode(),
                                     base64.b64encode(b"x" * 33).decode(), 7, None, b"x" * 32])
    def test_anything_else_does_not(self, bad):
        with pytest.raises(ValueError):
            parse_key(bad)

    def test_a_node_keeps_its_key_across_restarts(self):
        meta = _MemoryMeta()
        assert ObliviousService(meta).public_b64 == ObliviousService(meta).public_b64

    def test_two_nodes_have_two_keys(self):
        assert ObliviousService(_MemoryMeta()).public_b64 != ObliviousService(_MemoryMeta()).public_b64

    def test_a_damaged_stored_key_is_replaced_not_fatal(self):
        meta = _MemoryMeta()
        meta.set_meta(oblivious.META_KEY, "garbage")
        first = ObliviousService(meta).public_b64
        assert ObliviousService(meta).public_b64 == first        # and it stuck


class TestSealing:
    def test_a_request_reaches_the_target_intact(self):
        svc = ObliviousService(_MemoryMeta())
        blob, _ = seal_request(svc.public_b64, "GET", "/api/state?addr=x", None)
        req, _client = svc.open_request(blob)
        assert (req["m"], req["p"], req["b"]) == ("GET", "/api/state?addr=x", None)

    def test_the_answer_reaches_the_client_and_only_the_client(self):
        svc = ObliviousService(_MemoryMeta())
        blob, one_time = seal_request(svc.public_b64, "GET", "/api/fees")
        _req, client_key = svc.open_request(blob)
        answer = svc.seal_response(client_key, 200, {"next_block": 1})
        assert open_response(one_time, answer) == (200, {"next_block": 1})
        _blob2, someone_else = seal_request(svc.public_b64, "GET", "/api/fees")
        with pytest.raises(ValueError):
            open_response(someone_else, answer)

    def test_another_node_cannot_open_it(self):
        mine, theirs = ObliviousService(_MemoryMeta()), ObliviousService(_MemoryMeta())
        blob, _ = seal_request(mine.public_b64, "GET", "/api/fees")
        with pytest.raises(ValueError):
            theirs.open_request(blob)

    def test_the_relay_cannot_read_the_address_in_it(self):
        svc = ObliviousService(_MemoryMeta())
        blob, _ = seal_request(svc.public_b64, "GET", "/api/state?addr=my.secret.address")
        assert b"my.secret.address" not in blob
        assert b"my.secret.address" not in base64.b64decode(base64.b64encode(blob))

    def test_a_flipped_bit_is_caught(self):
        svc = ObliviousService(_MemoryMeta())
        blob, _ = seal_request(svc.public_b64, "GET", "/api/fees")
        bad = bytearray(blob)
        bad[-1] ^= 1
        with pytest.raises(ValueError):
            svc.open_request(bytes(bad))

    @pytest.mark.parametrize("garbage", [b"", b"x", os.urandom(200), os.urandom(5000)])
    def test_garbage_is_refused(self, garbage):
        with pytest.raises(ValueError):
            ObliviousService(_MemoryMeta()).open_request(garbage)

    def test_a_request_missing_fields_is_refused(self):
        from nacl.public import PublicKey, SealedBox
        svc = ObliviousService(_MemoryMeta())
        box = SealedBox(PublicKey(parse_key(svc.public_b64)))
        for plain in (b"{}", b'{"m": "GET"}', b'{"m": 1, "p": "/", "k": "x"}', b"not json",
                      json.dumps({"m": "GET", "p": "/", "k": "short"}).encode()):
            with pytest.raises(ValueError):
                svc.open_request(box.encrypt(pad(plain)))

    def test_lookups_look_alike_on_the_wire_and_a_transaction_is_bigger(self):
        svc = ObliviousService(_MemoryMeta())
        sizes = {len(seal_request(svc.public_b64, "GET", path)[0]) for path in (
            "/api/fees", "/api/state?addr=" + "word." * 12, "/api/address/" + "word." * 12 + "/page?page=3")}
        assert len(sizes) == 1
        tx = {"from": "a", "pubkey": "ab" * 897, "signature": "cd" * 666, "nonce": 1,
              "outputs": [{"to": "b", "amount": 1}], "fee": 1}
        assert len(seal_request(svc.public_b64, "POST", "/api/tx/send", tx)[0]) > max(sizes)

    def test_the_largest_possible_request_still_fits(self):
        svc = ObliviousService(_MemoryMeta())
        blob, _ = seal_request(svc.public_b64, "POST", "/api/tx/send", {"x": "y" * 7000})
        assert len(blob) <= oblivious.MAX_BLOB


# ---------------------------------------------------------------------------
# A small network of real nodes
# ---------------------------------------------------------------------------

class _Reply:
    """What node-to-node requests.post returns, from a Flask response."""

    def __init__(self, r):
        self.status_code = r.status_code
        self._data = r.data

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class _Routed(_Session):
    """A client's session that reaches whichever node the URL names."""

    def __init__(self, routes):
        super().__init__(None)
        self.routes = routes
        self.down = set()

    def request(self, method, url, **kw):
        host = urlsplit(url).netloc
        if host in self.down or host not in self.routes:
            raise requests.ConnectionError(f"{host} is down")
        self.client = self.routes[host]
        return super().request(method, url, **kw)


class Net:
    SEED, B, C = "seed.test", "9.9.9.9:8333", "8.8.8.8:8333"

    def __init__(self, tmp_path, monkeypatch):
        sk, pk = crypto.generate_keypair()
        keyfile = str(tmp_path / "wallet.key")
        crypto.save_key(keyfile, sk, pk, PASS)
        self.wallet = Wallet(keyfile, pk)
        self.cs = ChainState.from_genesis()
        self.cs.state.credit(self.wallet.addr, 1000 * TICKS_PER_LAPSE)
        self.cs.chain.append({"height": 1, "timestamp": int(time.time()), "hash": "h1", "transactions": [
            {"from": address(0), "nonce": 1, "fee": 100,
             "outputs": [{"to": crypto.burn_address(), "amount": 1}],
             "memo": tx_mod.BOARD_MEMO_TAG + "hello"}]})
        self.nodes, self.services, self.pools, self.routes = {}, {}, {}, {}
        for host in (self.SEED, self.B, self.C):
            node = _Node(self.cs)
            svc = ObliviousService(_MemoryMeta())
            pool = peerpool_mod.PeerPool()
            self.nodes[host], self.services[host], self.pools[host] = node, svc, pool
        # Who knows whom: the seed knows both peers; the peers know each other.
        for host, peers in ((self.SEED, (self.B, self.C)), (self.B, (self.C,)), (self.C, (self.B,))):
            for peer in peers:
                self.pools[host].add(peer)
                self.pools[host].set_http_reachable(peer, True)
                self.pools[host].set_oblivious_key(peer, self.services[peer].public_b64)
        for host in self.nodes:
            self.routes[host] = api.create_app(
                self.nodes[host], self.pools[host], oblivious=self.services[host]).test_client()

        def post(url, data=None, timeout=None, headers=None, stream=False):
            host = urlsplit(url).netloc
            if host not in self.routes or host in self.session.down:
                raise requests.ConnectionError("down")
            self.reached.append((host, urlsplit(url).path, data))
            return _Reply(self.routes[host].post(urlsplit(url).path, data=data, headers=headers))
        self.reached = []
        monkeypatch.setattr(api.requests, "post", post)
        self.session = _Routed(self.routes)

    def reader(self, **kw):
        r = RemoteReader([f"https://{self.SEED}"], session=self.session, refresh=0, **kw)
        return r

    def heard_by(self, host):
        """What the client itself sent to host."""
        base = ("https://" if host == self.SEED else "http://") + host
        return [e for e in self.session.log if e[4] == base]


class _Node(_InfoNode):
    """A node with real validation on submit, and the info the app serves."""

    def __init__(self, cs):
        super().__init__(cs)
        self.cs = cs
        from tests.test_light import _TxIndex
        self.storage = _TxIndex(cs)

    def submit_tx_from_api(self, t, timeout=5):
        probe = self.mempool.probe_state_for(t.get("from"), self.cs.state)
        ok, err = tx_mod.validate(t, probe)
        return (False, err) if not ok else self.mempool.add(t)


@pytest.fixture
def net(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_reader, "_listening", lambda port: False)   # no Tor here
    return Net(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# The two ends, on a node
# ---------------------------------------------------------------------------

class TestAdvertising:
    def test_info_carries_the_key(self, net):
        assert net.routes[net.B].get("/api/info").get_json()["oblivious_key"] \
            == net.services[net.B].public_b64

    def test_a_node_with_the_feature_off_says_so_and_has_no_routes(self):
        app = api.create_app(_InfoNode(ChainState.from_genesis()), peerpool_mod.PeerPool())
        c = app.test_client()
        assert c.get("/api/info").get_json()["oblivious_key"] is None
        assert c.post("/api/oblivious", data=b"x").status_code == 404
        assert c.post("/api/relay", json={}).status_code == 404

    def test_the_http_list_carries_keys_for_the_peers_and_the_node_itself(self, net):
        data = net.routes[net.SEED].get("/api/peers/http").get_json()
        assert data["self_key"] == net.services[net.SEED].public_b64
        assert data["keys"] == {net.B: net.services[net.B].public_b64,
                                net.C: net.services[net.C].public_b64}

    def test_it_lists_no_key_for_a_peer_that_has_none(self, net):
        net.pools[net.SEED].set_oblivious_key(net.C, None)
        assert net.C not in net.routes[net.SEED].get("/api/peers/http").get_json()["keys"]


class TestTheTargetEnd:
    def _ask(self, net, method, path, body=None, host=None):
        host = host or net.B
        blob, one_time = seal_request(net.services[host].public_b64, method, path, body)
        resp = net.routes[host].post("/api/oblivious", data=blob)
        return resp, one_time

    def test_it_answers_a_state_lookup_sealed(self, net):
        resp, one_time = self._ask(net, "GET", f"/api/state?addr={net.wallet.addr}&fees=1")
        status, body = open_response(one_time, resp.data)
        assert status == 200 and body["balance"] == 1000 * TICKS_PER_LAPSE and "fees" in body

    def test_the_answer_is_the_same_as_asking_openly(self, net):
        path = f"/api/address/{net.wallet.addr}/page?page=1"
        resp, one_time = self._ask(net, "GET", path)
        assert open_response(one_time, resp.data) == (200, net.routes[net.B].get(path).get_json())

    def test_it_accepts_a_transaction_sealed_and_the_node_validates_it(self, net):
        t, _ = net.wallet.sign_tx([{"to": address(3), "amount": 1000}], 1, 2500, passphrase=PASS)
        resp, one_time = self._ask(net, "POST", "/api/tx/send", t)
        assert open_response(one_time, resp.data)[0] == 200
        assert [x["from"] for x in net.nodes[net.B].mempool.all_txs()] == [net.wallet.addr]

    def test_a_bad_transaction_is_refused_by_the_same_rules(self, net):
        t, _ = net.wallet.sign_tx([{"to": address(3), "amount": 1000}], 7, 2500, passphrase=PASS)
        resp, one_time = self._ask(net, "POST", "/api/tx/send", t)
        status, body = open_response(one_time, resp.data)
        assert status == 400 and body["ok"] is False
        assert net.nodes[net.B].mempool.all_txs() == []

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/mempool"), ("GET", "/api/peers"), ("GET", "/api/info"),
        ("GET", "/api/board/page"), ("GET", "/api/block/1"), ("GET", "/api/peers/http"),
        ("GET", "/api/oblivious"), ("GET", "/api/relay"), ("GET", "/"),
        ("GET", "/api/tx/send"), ("POST", "/api/state"), ("POST", "/api/fees"),
        ("DELETE", "/api/state"), ("GET", "/api/address/x/history"),
        ("GET", "/../etc/passwd"), ("GET", "//api/state"),
    ])
    def test_nothing_else_can_be_asked_this_way(self, net, method, path):
        resp, one_time = self._ask(net, method, path)
        assert open_response(one_time, resp.data)[0] == 404

    def test_garbage_is_a_400_and_says_nothing(self, net):
        resp = net.routes[net.B].post("/api/oblivious", data=os.urandom(300))
        assert resp.status_code == 400 and resp.data == b""

    def test_something_sealed_to_another_node_is_a_400(self, net):
        blob, _ = seal_request(net.services[net.C].public_b64, "GET", "/api/fees")
        assert net.routes[net.B].post("/api/oblivious", data=blob).status_code == 400

    def test_too_large_is_a_413(self, net):
        resp = net.routes[net.B].post("/api/oblivious", data=b"x" * (oblivious.MAX_BLOB + 1))
        assert resp.status_code == 413

    def test_limits_are_counted_against_whoever_passed_it_on(self, net, monkeypatch):
        """A sealed request runs through the same app, so it gets the same
        limits, counted against the node that handed it over, not against
        the one machine every sealed request would otherwise come from."""
        app = net.routes[net.B].application
        seen = []
        app.add_url_rule("/api/_who", "who", lambda: seen.append(api.request.remote_addr) or "{}")
        monkeypatch.setattr(api, "_OBLIVIOUS_PATHS", re.compile(r"^/api/_who$"))
        blob, one_time = seal_request(net.services[net.B].public_b64, "GET", "/api/_who")
        net.routes[net.B].post("/api/oblivious", data=blob,
                               environ_overrides={"REMOTE_ADDR": "203.0.113.50"})
        assert seen == ["203.0.113.50"]


class TestTheRelayEnd:
    def _relay(self, net, host, to, blob):
        return net.routes[host].post("/api/relay", json={
            "to": to, "blob": base64.b64encode(blob).decode()})

    def test_it_passes_a_sealed_request_on_and_the_sealed_answer_back(self, net):
        blob, one_time = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        resp = self._relay(net, net.SEED, net.B, blob)
        assert resp.status_code == 200
        assert open_response(one_time, resp.data)[0] == 200

    def test_it_passes_the_bytes_on_unchanged_and_never_opens_them(self, net, monkeypatch):
        opened = []
        monkeypatch.setattr(ObliviousService, "open_request",
                            lambda self, blob: opened.append(blob) or (_ for _ in ()).throw(ValueError()))
        blob, _ = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        net.reached.clear()
        self._relay(net, net.SEED, net.B, blob)
        # the only opener called was the target's own, on the same bytes
        assert net.reached == [(net.B, "/api/oblivious", blob)]
        assert opened == [blob]

    def test_the_relay_learns_the_target_but_not_what_was_asked(self, net):
        blob, _ = seal_request(net.services[net.B].public_b64, "GET",
                               f"/api/state?addr={net.wallet.addr}")
        net.reached.clear()
        self._relay(net, net.SEED, net.B, blob)
        assert net.wallet.addr.encode() not in net.reached[0][2]

    @pytest.mark.parametrize("to", ["1.2.3.4:8333", "127.0.0.1:8335", "evil.example.org:80",
                                    "http://9.9.9.9:8333", "", None, 7, ["9.9.9.9:8333"]])
    def test_it_only_relays_to_peers_it_knows_answer_http(self, net, to):
        blob, _ = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        net.reached.clear()
        resp = net.routes[net.SEED].post("/api/relay", json={
            "to": to, "blob": base64.b64encode(blob).decode()})
        assert resp.status_code == 400 and net.reached == []

    def test_it_will_not_relay_to_a_peer_not_known_reachable(self, net):
        net.pools[net.SEED].set_http_reachable(net.B, False)
        blob, _ = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        assert self._relay(net, net.SEED, net.B, blob).status_code == 400

    @pytest.mark.parametrize("body", [None, "text", [], {}, {"to": "9.9.9.9:8333"},
                                      {"to": "9.9.9.9:8333", "blob": 5},
                                      {"to": "9.9.9.9:8333", "blob": "!!not base64!!"}])
    def test_a_malformed_request_is_a_400(self, net, body):
        resp = net.routes[net.SEED].post("/api/relay", json=body)
        assert resp.status_code == 400

    def test_too_large_is_refused(self, net):
        resp = self._relay(net, net.SEED, net.B, b"x" * (oblivious.MAX_BLOB + 1))
        assert resp.status_code == 413

    def test_a_target_that_is_down_is_a_502(self, net):
        net.session.down.add(net.B)
        blob, _ = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        assert self._relay(net, net.SEED, net.B, blob).status_code == 502

    def test_a_target_that_will_not_open_it_is_a_502(self, net):
        blob, _ = seal_request(net.services[net.C].public_b64, "GET", "/api/fees")
        assert self._relay(net, net.SEED, net.B, blob).status_code == 502

    def test_it_will_not_pass_an_oversized_answer_on(self, net, monkeypatch):
        class Big:
            status_code = 200

            def iter_content(self, n):
                yield b"x" * n
        monkeypatch.setattr(api.requests, "post", lambda *a, **k: Big())
        blob, _ = seal_request(net.services[net.B].public_b64, "GET", "/api/fees")
        assert self._relay(net, net.SEED, net.B, blob).status_code == 502


# ---------------------------------------------------------------------------
# The client, sending the wallet's address through a relay
# ---------------------------------------------------------------------------

class TestClientUsesTheRelay:
    def _named(self, entries, addr):
        return [e for e in entries if addr in json.dumps(e[2:4])]

    def test_a_balance_lookup_never_names_the_address_to_a_relay(self, net):
        r = net.reader()
        assert r.account(net.wallet.addr, fees=True)["balance"] == 1000 * TICKS_PER_LAPSE
        relayed = [e for e in net.session.log if e[1] == "/api/relay"]
        assert relayed, "it went straight to a node"
        assert self._named(net.session.log, net.wallet.addr) == []
        assert net.wallet.addr.encode() not in b"".join(d for _, _, d in net.reached)

    def test_the_node_that_opens_it_is_never_contacted_by_the_client(self, net):
        """The client talks to the relay only. The target hears from the
        relay, which is all it can learn about who asked."""
        r = net.reader()
        r.account(net.wallet.addr, fees=True)
        relay = next(e[4] for e in net.session.log if e[1] == "/api/relay")
        target = next(h for h, p, _ in net.reached if p == "/api/oblivious")
        target_url = ("https://" if target == net.SEED else "http://") + target
        assert relay != target_url
        assert target_url not in {e[4] for e in net.session.log if e[1] != "/api/peers/http"}

    def test_the_header_says_so(self, net):
        r = net.reader()
        client = create_light_app(r, net.wallet).test_client()
        client.get("/address")
        assert "via relay" in client.get("/board").get_data(as_text=True)

    def test_a_transaction_is_submitted_through_it_too(self, net):
        r = net.reader()
        client = create_light_app(r, net.wallet).test_client()
        submit(client, "/board", passphrase=PASS, message="hi there")
        pending = [t for n in net.nodes.values() for t in n.mempool.all_txs()]
        assert [t["memo"] for t in pending] == [tx_mod.BOARD_MEMO_TAG + "hi there"]
        assert self._named(net.session.log, net.wallet.addr) == []
        assert r.usage()["route"] == "via relay"

    def test_every_wallet_feature_works_through_it(self, net):
        r = net.reader()
        client = create_light_app(r, net.wallet).test_client()
        for path in ("/board", "/address", "/send"):
            assert client.get(path).status_code == 200
        n = 0
        for path, form in (("/board", {"message": "one"}), ("/send", {"outputs": f"{address(3)},1000"}),
                           ("/board/vote", {"ref": "abcdef", "dir": "+"}),
                           ("/board/delete", {"ref": "abcdef"})):
            submit(client, path, page="/board" if path.startswith("/board") else path,
                   passphrase=PASS, **form)
            n += 1
            assert sum(len(x.mempool.all_txs()) for x in net.nodes.values()) == n, path
        assert self._named(net.session.log, net.wallet.addr) == []

    def test_reading_the_board_does_not_use_it(self, net):
        r = net.reader()
        r.board_page(1)
        r.fee_estimate()
        assert not [e for e in net.session.log if e[1] == "/api/relay"]

    def test_relayed_blobs_all_look_alike_to_the_relay_except_a_transaction(self, net):
        r = net.reader()
        r.account(net.wallet.addr, fees=True)
        r.address_page(net.wallet.addr, 1)
        sizes = {len(e[2]["blob"]) for e in net.session.log if e[1] == "/api/relay"}
        assert len(sizes) == 1

    def test_the_pair_is_kept_for_the_session(self, net):
        r = net.reader()
        for i in range(5):
            r.address_page(net.wallet.addr, i + 1)
        pairs = {(e[4], json.loads(json.dumps(e[2]))["to"])
                 for e in net.session.log if e[1] == "/api/relay"}
        assert len(pairs) == 1

    def test_the_relay_and_the_target_are_different_nodes(self, net):
        for _ in range(30):
            r = net.reader()
            r.account(net.wallet.addr)
            (relay_url, _), (target, _) = r._pair
            assert relay_url != r._plain_url(target)
            net.session.log.clear()

    def test_it_is_not_used_when_only_one_node_supports_it(self, net):
        net.pools[net.SEED] = peerpool_mod.PeerPool()
        for host in (net.B, net.C):
            net.pools[net.SEED].add(host)
            net.pools[net.SEED].set_http_reachable(host, True)       # no keys
        net.routes[net.SEED] = api.create_app(net.nodes[net.SEED], net.pools[net.SEED],
                                              oblivious=net.services[net.SEED]).test_client()
        net.session.routes = net.routes
        r = net.reader()
        r.account(net.wallet.addr)
        assert not [e for e in net.session.log if e[1] == "/api/relay"]
        assert r.usage()["route"] == "direct, encrypted"

    def test_it_falls_back_to_a_direct_request_if_nothing_relays(self, net):
        net.session.down.update({net.B, net.C})
        r = net.reader()
        assert r.account(net.wallet.addr)["balance"] == 1000 * TICKS_PER_LAPSE

    def test_a_dead_pair_is_dropped_and_another_tried(self, net):
        r = net.reader()
        r.account(net.wallet.addr)
        (relay_url, _), (target, _) = r._pair
        down = urlsplit(relay_url).netloc
        net.session.down.add(down)
        r.invalidate()
        assert r.account(net.wallet.addr)["balance"] == 1000 * TICKS_PER_LAPSE

    def test_a_target_that_changed_its_key_is_dropped(self, net):
        r = net.reader()
        r.account(net.wallet.addr)                       # learn the keys
        (_, _), (target, _) = r._pair
        r._targets[target] = ObliviousService(_MemoryMeta()).public_b64    # now wrong
        r._pair = None
        r.invalidate()
        assert r.account(net.wallet.addr)["balance"] == 1000 * TICKS_PER_LAPSE

    def test_what_a_node_says_about_keys_is_checked(self, net):
        r = net.reader()
        r._last_base = "https://seed.test"
        r._learn_keys({"9.9.9.9:8333": "not a key", "10.0.0.1:80": net.services[net.B].public_b64,
                       "evil.example.org:80": net.services[net.B].public_b64, 7: "x",
                       net.C: net.services[net.C].public_b64}, "garbage")
        assert list(r._targets) == [net.C]
        assert list(r._relays) == [r._plain_url(net.C)]

    def test_a_transaction_too_large_to_seal_goes_directly(self, net):
        r = net.reader()
        outputs = [{"to": address(i % 6 + 1), "amount": 1} for i in range(300)]
        t, _ = net.wallet.sign_tx(outputs, 1, 90000, passphrase=PASS)
        assert len(json.dumps(t)) > 8000
        ok, _ = r.submit(t)
        assert ok and not [e for e in net.session.log if e[1] == "/api/relay"]
        assert r.usage()["route"] in ("direct, encrypted", "direct, not encrypted")

    def test_it_costs_little_extra(self, net):
        direct = RemoteReader([f"https://{net.SEED}"], session=net.session, refresh=0)
        direct._relays, direct._targets = {}, {}
        direct._discovered_at = time.time()
        direct.account(net.wallet.addr, fees=True)
        relayed = net.reader()
        relayed.account(net.wallet.addr, fees=True)
        # one discovery answer, plus a bucket of padding each way
        assert relayed.bytes_in - direct.bytes_in < 5000


# ---------------------------------------------------------------------------
# Tor, when it is there
# ---------------------------------------------------------------------------

class TestTorWhenFound:
    def _reader(self, net, **kw):
        r = net.reader(proxy="auto", **kw)
        r._discovered_at = time.time()
        return r

    def test_nothing_is_asked_of_the_user(self, net):
        assert net.reader(proxy="auto")._auto_tor

    def test_it_is_used_when_found(self, net, monkeypatch):
        monkeypatch.setattr(remote_reader, "_listening", lambda port: port == 9050)
        r = self._reader(net)
        r.account(net.wallet.addr)
        proxies = set(net.session.proxies.values())
        assert len(proxies) == 1 and next(iter(proxies)).startswith("socks5h://lapse")
        assert next(iter(proxies)).endswith("@127.0.0.1:9050")
        assert r.usage()["route"] == "via Tor"

    def test_tor_browsers_port_is_found_too(self, net, monkeypatch):
        monkeypatch.setattr(remote_reader, "_listening", lambda port: port == 9150)
        r = self._reader(net)
        r.fee_estimate()
        assert next(iter(net.session.proxies.values())).endswith("@127.0.0.1:9150")

    def test_it_is_not_used_when_not_found_and_nothing_is_the_matter(self, net):
        r = self._reader(net)
        assert r.account(net.wallet.addr)["balance"] > 0
        assert net.session.proxies == {}
        assert "Tor" not in r.usage()["route"]

    def test_with_tor_the_address_is_sent_straight_through_it_not_via_a_relay(self, net, monkeypatch):
        monkeypatch.setattr(remote_reader, "_listening", lambda port: True)
        r = net.reader(proxy="auto")
        r.account(net.wallet.addr)
        assert not [e for e in net.session.log if e[1] == "/api/relay"]

    def test_it_is_picked_up_when_tor_starts_later(self, net, monkeypatch):
        up = {"v": False}
        monkeypatch.setattr(remote_reader, "_listening", lambda port: up["v"])
        r = self._reader(net)
        r.fee_estimate()
        assert net.session.proxies == {}
        up["v"] = True
        r._tor_checked = 0                     # the next look is due
        r.invalidate()
        r.fee_estimate()
        assert net.session.proxies

    def test_it_is_dropped_when_tor_stops(self, net, monkeypatch):
        up = {"v": True}
        monkeypatch.setattr(remote_reader, "_listening", lambda port: up["v"])
        r = self._reader(net)
        r.fee_estimate()
        assert net.session.proxies
        up["v"] = False
        r._tor_checked = 0
        r.invalidate()
        r.fee_estimate()
        assert net.session.proxies == {}
        assert r.usage()["route"].startswith("direct")

    def test_it_is_not_looked_for_on_every_request(self, net, monkeypatch):
        looks = []
        monkeypatch.setattr(remote_reader, "_listening", lambda port: looks.append(port) or False)
        r = self._reader(net)
        for _ in range(10):
            r.invalidate()
            r.fee_estimate()
        assert len(looks) == len(remote_reader.TOR_PORTS)

    def test_a_tor_that_fails_is_dropped_for_a_while_and_the_request_still_works(self, net, monkeypatch):
        monkeypatch.setattr(remote_reader, "_listening", lambda port: True)
        real = net.session.request

        def fail_through_a_proxy(method, url, **kw):
            if net.session.proxies:
                raise requests.exceptions.ProxyError("tor is not ready")
            return real(method, url, **kw)
        net.session.request = fail_through_a_proxy
        r = self._reader(net)
        assert r.fee_estimate()
        assert net.session.proxies == {}
        assert r._tor_bad_until > time.monotonic()
        r.invalidate()
        assert r.fee_estimate()                # still without it, not retried at once

    def test_a_proxy_the_user_named_is_never_quietly_dropped(self, net):
        def refuse(method, url, **kw):
            raise requests.exceptions.ProxyError("no")
        net.session.request = refuse
        r = net.reader(proxy="socks5h://127.0.0.1:9050")
        r._discovered_at = time.time()
        with pytest.raises(remote_reader.RemoteError, match="ProxyError"):
            r.fee_estimate()

    def test_each_run_has_a_circuit_of_its_own(self, net, monkeypatch):
        monkeypatch.setattr(remote_reader, "_listening", lambda port: True)
        logins = set()
        for _ in range(3):
            r = self._reader(net)
            r.fee_estimate()
            logins.add(next(iter(net.session.proxies.values())))
        assert len(logins) == 3
