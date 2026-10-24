"""The connection check: what can be said for certain about why a node may
not be reaching peers, and the simple fix when there is one.

The rule is that a line appears only when it was measured. Anything that
could not be measured is left out, never guessed, and a fix is offered only
under a measured fact and only when it takes a few clicks. judge() is a pure
function of the facts gather() collected, so every sentence is tested without
a network (tests/test_netcheck.py).
"""

import ipaddress
import os
import socket
import threading
import time
from collections import namedtuple

Finding = namedtuple("Finding", "what why fix")

UDP_WAIT = 2.5
HTTPS_TIMEOUT = 5

# A DHT "ping" query: any DHT node answers it, and the answer proves a UDP
# packet went out and a reply came back.
_DHT_PING = b"d1:ad2:id20:" + b"\x00" * 20 + b"e1:q4:ping1:t2:aa1:y1:qe"


def _resolve(host):
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    except OSError:
        return None


def _udp_replies(targets, wait=UDP_WAIT):
    """targets: {host: (ip, port)}. Returns the set of hosts that answered."""
    answered = set()
    by_ip = {ip: host for host, (ip, _port) in targets.items()}
    if not by_ip:
        return answered
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", 0))
        for host, (ip, port) in targets.items():
            try:
                sock.sendto(_DHT_PING, (ip, port))
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and len(answered) < len(by_ip):
            sock.settimeout(max(deadline - time.monotonic(), 0.05))
            try:
                data, (src_ip, _src_port) = sock.recvfrom(2048)
            except OSError:
                break
            if (data.startswith(b"d") and src_ip in by_ip
                    and (b"1:y1:r" in data or b"1:y1:e" in data)):
                answered.add(by_ip[src_ip])
    finally:
        sock.close()
    return answered


def _https_ip(url):
    import requests
    try:
        r = requests.get(url, timeout=HTTPS_TIMEOUT)
        if r.status_code == 200:
            return r.text.strip() or None
    except Exception:
        pass
    return None


def gather(udp, pool):
    """Measure. Every I/O call here is bounded, and they run side by side."""
    from discovery import IP_ECHO_URLS
    from discovery_dht import DHT_BOOTSTRAP_HOSTS

    facts = {"dht_hosts": {}, "dht_answered": set(), "web_ip": None, "web_tried": 0,
             "peers": pool.count(), "observed": dict(udp.observed_by)}

    for host, port in DHT_BOOTSTRAP_HOSTS:
        ip = _resolve(host)
        facts["dht_hosts"][host] = (ip, port) if ip else None

    resolvable = {h: v for h, v in facts["dht_hosts"].items() if v}
    results = {}

    def udp_job():
        results["udp"] = _udp_replies(resolvable)

    def web_job():
        for url in IP_ECHO_URLS:
            facts["web_tried"] += 1
            ip = _https_ip(url)
            if ip:
                results["web"] = ip
                return

    threads = [threading.Thread(target=udp_job, daemon=True),
               threading.Thread(target=web_job, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(UDP_WAIT + HTTPS_TIMEOUT * len(IP_ECHO_URLS) + 2)
    facts["dht_answered"] = results.get("udp", set())
    facts["web_ip"] = results.get("web")
    return facts


def _split(addr):
    host, _, port = addr.rpartition(":")
    return host, port


def judge(facts):
    """Findings from facts, only those the facts prove."""
    out = []
    hosts = facts["dht_hosts"]
    resolved = [h for h, v in hosts.items() if v]
    answered = facts["dht_answered"]

    if hosts and not resolved:
        out.append(Finding(
            "None of the network's name lookups worked",
            f"The names of the {len(hosts)} public DHT hosts did not resolve to an address.",
            "Check the computer's internet connection and DNS setting. Setting DNS to "
            "1.1.1.1 or 8.8.8.8 in the network settings fixes most cases."))
    elif resolved:
        if answered:
            out.append(Finding(
                "Outgoing UDP works",
                f"{len(answered)} of {len(resolved)} public DHT hosts answered a UDP packet "
                "from this computer.", ""))
        else:
            out.append(Finding(
                f"None of the {len(resolved)} public DHT hosts answered a UDP packet",
                "The packets were sent and nothing came back in "
                f"{UDP_WAIT:g} seconds. This node talks to other nodes over UDP.",
                "To find out whether this network blocks UDP, run the node on a phone "
                "hotspot. If peers appear there, the first network is blocking UDP: check "
                "the firewall and antivirus settings, or ask whoever runs that network."))

    web = facts["web_ip"]
    if web:
        out.append(Finding("Outgoing HTTPS works",
                           f"A web service saw this connection come from {web}.", ""))
    elif facts["web_tried"]:
        out.append(Finding(
            "Outgoing HTTPS did not work",
            f"None of the {facts['web_tried']} public-address web services answered.", ""))

    seen = {}
    for peer, mine in facts["observed"].items():
        host, port = _split(mine)
        if host and port:
            seen.setdefault(host, set()).add(port)
    for host, ports in seen.items():
        if len(ports) > 1 and len(facts["observed"]) >= 2:
            out.append(Finding(
                "Your router uses a different public port for each peer",
                f"Peers saw this node at {host} on {len(ports)} different ports. Connections "
                "this node starts to nodes with a public address are not affected, but two "
                "nodes both behind such routers often cannot reach each other directly.",
                ""))
        elif len(facts["observed"]) >= 2:
            out.append(Finding(
                "Your router uses the same public port toward different peers",
                f"{len(facts['observed'])} peers all saw this node at {host}:{next(iter(ports))}. "
                "That is the kind of router that direct connections work through.", ""))
        break

    out.append(Finding(
        f"{facts['peers']} peer{'s' if facts['peers'] != 1 else ''} connected right now", "", ""))
    return out
