"""Background tip-info prober for known peers.

The peers page shows each peer's height and version. Both come from one
place, PeerPool.update_info, fed by a GETINFO/INFO exchange, and a peer's
height is a claim it has to make itself. Where to pay a node is no longer
among them: that arrives as a relayed liveness note instead, which is what
keeps a payout address from being tied to an IP (see
Node._handle_inbound_alive).

Until this module existed, that exchange only ever happened as a side
effect of syncing, so the columns filled at whatever rate syncing happened
to run. That was fine while syncing was scheduled. Once it became
event-driven (Node._sync_if_triggered), a healthy node that is neither
behind nor starved of blocks syncs rarely, and the only routine exchange
left was a single random peer once per block interval. A given peer's row
then went stale for roughly peer-count block intervals at a time while the
peer itself was plainly active, since last_seen updates on any inbound
traffic.

The fix is to stop paying for this information with a sync. An INFO
exchange is one datagram each way with no fork search and no chain fetch;
a sync is the expensive part. They were coupled only because the info came
along for the ride. So this asks directly, and deliberately never triggers
a sync no matter what a peer claims: a claimed height is cheap to fake,
and only a real block is allowed to make this node go looking (see
Node._sync_if_triggered). Answers are still only believed when they match
a request we issued ourselves (UDPTransport.get_info), so nothing here
widens what an unsolicited datagram can do.

Deliberately not part of peerpool.py (see that module's own docstring: pure
data, no I/O, no threads). Same separation http_probe.py and discovery.py
already use, probe here, store there.

External interface (called from main.py):
  run(pool, udp, interval=60, timeout=3.0)   blocking, run as daemon thread
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("ec.info_probe")

MAX_WORKERS = 20


def _probe_one(udp, addr, timeout):
    try:
        return udp.get_info(addr, timeout=timeout)
    except Exception:
        return None


def probe_round(pool, udp, timeout=3.0, executor=None):
    """Ask every currently-known peer for its tip info once, in parallel
    (bounded by MAX_WORKERS so one round never issues an unbounded burst),
    and record what came back. Returns how many answered.

    A peer added or removed between rounds is picked up or dropped on the
    next one, since pool.all_addrs() is always the current membership.
    """
    addrs = pool.all_addrs()
    if not addrs:
        return 0
    if executor is None:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as own:
            return _probe_with(pool, udp, addrs, timeout, own)
    return _probe_with(pool, udp, addrs, timeout, executor)


def _probe_with(pool, udp, addrs, timeout, executor):
    answered = 0
    futures = {executor.submit(_probe_one, udp, addr, timeout): addr
               for addr in addrs}
    for future in as_completed(futures):
        try:
            info = future.result()
        except Exception:
            info = None
        if not isinstance(info, dict):
            continue
        answered += 1
        pool.update_info(futures[future],
                         height=info.get("height"),
                         version=info.get("version", ""))
    log.debug("[info_probe] asked %d peers, %d answered", len(addrs), answered)
    return answered


def run(pool, udp, interval=60, timeout=3.0):
    """Forever: one probe_round, then sleep.

    One executor for the life of the thread rather than one per round; see
    http_probe.run for why."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS,
                            thread_name_prefix="info-probe") as executor:
        while True:
            probe_round(pool, udp, timeout=timeout, executor=executor)
            time.sleep(interval)
