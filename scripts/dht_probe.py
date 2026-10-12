#!/usr/bin/env python3
"""Check, without a release, whether an early DHT put is the problem.

Run the writer on the machine that never gets found (the Colab node) and the
reader on one that should find it (your home node), with the same NAME:

    python scripts/dht_probe.py writer demo1
    python scripts/dht_probe.py reader demo1

It uses its own throwaway slots (named after NAME), never the real network's,
and needs only libtorrent and PyNaCl.

The writer puts two items: "early", one second after it starts, and "late",
60 seconds after the DHT has bootstrapped. Every 20 seconds it reads both back
the way any other node would, and prints whether each came back. The reader
only reads. What you get:

  early missing on the writer itself   -> a put sent into a young routing
                                          table is lost, the hunch is right
  late found on the writer, early not  -> same, and waiting is the fix
  both found on the writer, not on the reader
                                       -> the DHT is fine, the problem is
                                          between these two machines
  both found on both                   -> the DHT path works; look elsewhere
"""

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import libtorrent as lt
import nacl.signing

POLL = 20


def keypair(name):
    seed = hashlib.sha256(b"lapsecoin-dht-probe:" + name.encode()).digest()
    pk = bytes(nacl.signing.SigningKey(seed).verify_key)
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h), pk


def stamp():
    return time.strftime("%H:%M:%S")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("writer", "reader"):
        sys.exit(__doc__)
    role, name = sys.argv[1], sys.argv[2]
    sk, pk = keypair(name)

    settings = lt.default_settings()
    settings["enable_dht"] = True
    settings["enable_lsd"] = False
    settings["enable_upnp"] = False
    settings["enable_natpmp"] = False
    settings["listen_interfaces"] = "0.0.0.0:0"
    settings["alert_mask"] = lt.alert.category_t.dht_notification
    settings["dht_bootstrap_nodes"] = ("router.bittorrent.com:6881,router.utorrent.com:6881,"
                                       "dht.transmissionbt.com:6881,dht.aelitis.com:6881")
    ses = lt.session(settings)
    started = time.monotonic()
    bootstrapped_at = None
    found = {}      # salt -> last value seen
    puts = {}       # salt -> seconds since start when put
    print(f"[{stamp()}] {role} '{name}' started, bootstrapping", flush=True)

    def put(salt, value):
        ses.dht_put_mutable_item(sk, pk, json.dumps(value).encode(), salt)
        puts[salt] = round(time.monotonic() - started, 1)
        state = "after" if bootstrapped_at else "BEFORE"
        print(f"[{stamp()}] put {salt.decode()} {state} bootstrap finished "
              f"({puts[salt]}s in)", flush=True)

    def pump():
        nonlocal bootstrapped_at
        for a in ses.pop_alerts():
            if isinstance(a, lt.dht_bootstrap_alert) and bootstrapped_at is None:
                bootstrapped_at = time.monotonic()
                print(f"[{stamp()}] bootstrap complete "
                      f"({bootstrapped_at - started:.1f}s in)", flush=True)
            elif isinstance(a, lt.dht_put_alert):
                print(f"[{stamp()}] put reached {a.num_success} node(s)", flush=True)
            elif isinstance(a, lt.dht_mutable_item_alert) and a.authoritative:
                salt = a.salt if isinstance(a.salt, bytes) else a.salt.encode()
                item = a.item
                raw = (item.get("value") or item.get(b"value")) if isinstance(item, dict) else None
                found[salt] = raw if getattr(a, "seq", 0) > 0 else None

    early_done = late_done = False
    last_poll = 0.0
    while True:
        pump()
        now = time.monotonic()
        if role == "writer":
            if not early_done and now - started >= 1:
                put(b"probe-early", {"which": "early", "t": time.time()})
                early_done = True
            if (not late_done and bootstrapped_at
                    and now - bootstrapped_at >= 60):
                put(b"probe-late", {"which": "late", "t": time.time()})
                late_done = True
        if now - last_poll >= POLL:
            last_poll = now
            found.clear()
            for salt in (b"probe-early", b"probe-late"):
                ses.dht_get_mutable_item(pk, salt)
            time.sleep(10)
            pump()
            line = ", ".join(
                f"{s.decode()[6:]}: {'FOUND' if found.get(s) else 'missing'}"
                for s in (b"probe-early", b"probe-late"))
            print(f"[{stamp()}] read back -> {line}", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
