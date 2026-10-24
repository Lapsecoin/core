"""The connection check says only what the facts prove, and a fix only under one."""

import netcheck


def _facts(**kw):
    base = {"dht_hosts": {"a.example": ("1.1.1.1", 6881), "b.example": ("2.2.2.2", 6881)},
            "dht_answered": set(), "web_ip": None, "web_tried": 0,
            "peers": 0, "observed": {}}
    base.update(kw)
    return base


def _whats(findings):
    return [f.what for f in findings]


def test_udp_replies_are_reported_without_a_fix():
    out = netcheck.judge(_facts(dht_answered={"a.example"}))
    f = next(f for f in out if f.what == "Outgoing UDP works")
    assert "1 of 2" in f.why
    assert f.fix == ""


def test_no_udp_reply_states_the_fact_and_a_test_not_a_cause():
    out = netcheck.judge(_facts())
    f = next(f for f in out if "answered a UDP packet" in f.what)
    assert "None of the 2" in f.what
    assert "hotspot" in f.fix
    assert "blocked" not in f.what     # the cause is not claimed


def test_names_that_do_not_resolve_are_reported_instead_of_a_udp_verdict():
    out = netcheck.judge(_facts(dht_hosts={"a.example": None, "b.example": None}))
    assert any("name lookups" in w for w in _whats(out))
    assert not any("UDP" in w for w in _whats(out))


def test_a_port_that_differs_per_peer_is_reported():
    out = netcheck.judge(_facts(observed={"p1:1": "9.9.9.9:4000", "p2:1": "9.9.9.9:4001"}))
    assert any("different public port for each peer" in w for w in _whats(out))


def test_a_port_that_is_the_same_per_peer_is_reported():
    out = netcheck.judge(_facts(observed={"p1:1": "9.9.9.9:4000", "p2:1": "9.9.9.9:4000"}))
    assert any("same public port" in w for w in _whats(out))


def test_one_peer_is_not_enough_to_say_anything_about_ports():
    out = netcheck.judge(_facts(observed={"p1:1": "9.9.9.9:4000"}))
    assert not any("public port" in w for w in _whats(out))


def test_https_failure_is_only_reported_when_it_was_tried():
    assert not any("HTTPS" in w for w in _whats(netcheck.judge(_facts())))
    out = netcheck.judge(_facts(web_tried=3))
    assert any("HTTPS did not work" in w for w in _whats(out))


def test_peer_count_is_always_last():
    out = netcheck.judge(_facts(peers=1))
    assert out[-1].what == "1 peer connected right now"
