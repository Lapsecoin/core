"""Background HTTP-reachability prober for known peers.

The peers page wants to link a peer's address to its own web UI, but a
successful UDP handshake (what PeerPool already tracks) says nothing about
whether that peer's HTTP port. Same number, different protocol, possibly
a different firewall rule, actually accepts inbound connections. This
module answers that question directly instead of guessing from unrelated
signals: it periodically makes a real, short-timeout HTTP request to each
known peer and records whether it succeeded.

Deliberately not part of peerpool.py (see that module's own docstring: pure
data, no I/O, no threads). This is the same separation discovery.py already
uses for UDP, probe here, store there.

External interface (called from main.py):
  run(pool, interval=120, timeout=2.5)   blocking, run as daemon thread
"""

import logging
import time
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from public_url import parse_public_url

log = logging.getLogger("ec.http_probe")

# A cheap, always-present, unauthenticated endpoint, just confirms the
# peer's HTTP server on this address answers at all. Whether the request
# succeeds is what matters; the body is read only for the instance id (to
# spot this node under another address) and for the HTTPS address the peer
# advertises, if any, which light clients are told about (see
# /api/peers/http).
PROBE_PATH = "/api/info"
MAX_WORKERS = 20


def _probe_one(addr, timeout, own_iid=None):
    """(ok, public_url) for addr. ok is True if it answers, False if not,
    and None when the answer carries our own instance id, i.e. addr is this
    node under another address. public_url is the HTTPS address it
    advertises, if it advertises one that is acceptable (see
    public_url.parse_public_url), else None."""
    url = f"http://{addr}{PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return False, None
            try:
                info = json.loads(resp.read(1 << 20))
            except Exception:
                return True, None
            if own_iid and info.get("iid") == own_iid:
                return None, None
            try:
                return True, parse_public_url(info.get("public_url"))
            except (ValueError, TypeError):
                return True, None
    except Exception:
        return False, None


def run(pool, interval=120, timeout=2.5):
    """Forever: probe every currently-known peer address once, in parallel
    (bounded by MAX_WORKERS so one round never issues an unbounded burst of
    requests), then sleep. A peer added or removed between rounds is simply
    picked up or dropped on the next one. No separate bookkeeping needed
    since pool.snapshot() is always the current membership.

    One executor for the life of the thread, not one per round: rounds are
    forever, and building and tearing down twenty OS threads every two
    minutes is a cost with nothing to show for it."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS,
                            thread_name_prefix="http-probe") as pool_exec:
        while True:
            addrs = pool.all_addrs()
            if addrs:
                futures = {pool_exec.submit(_probe_one, addr, timeout, pool.instance_id): addr
                           for addr in addrs}
                for future in as_completed(futures):
                    try:
                        ok, public_url = future.result()
                    except Exception:
                        ok, public_url = False, None
                    if ok is None:
                        pool.mark_self(futures[future])
                        continue
                    pool.set_http_reachable(futures[future], ok)
                    pool.set_public_url(futures[future], public_url)
                log.debug("[http_probe] checked %d peers", len(addrs))
            time.sleep(interval)
