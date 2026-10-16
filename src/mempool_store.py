"""Mempool snapshot on disk. The pool itself stays I/O free (mempool.py); this
only turns its records into a file and back.

A snapshot is a plain JSON file written to a temp name and renamed over the
old one, so a crash mid-write leaves the previous snapshot intact rather than
half a file. It is a convenience, not a source of truth: whatever it holds is
re-validated against the chain on load, and an unreadable file is set aside
and ignored.
"""

import json
import logging
import os
import time

import tx as tx_mod

log = logging.getLogger(__name__)

FORMAT_VERSION = 1


def path_for(db_path):
    """The snapshot lives next to the chain database."""
    base = os.path.splitext(os.path.abspath(db_path))[0]
    return base + "_mempool.json"


def save(path, records, recovered=()):
    """Write [(tx, entered)] atomically. `recovered` is the set of tx hashes
    that came back from a lost branch (see Mempool.recovered_hashes), kept so
    a restart does not take their priority away. Returns the number written."""
    recovered = set(recovered)
    txs = []
    for tx, entered in records:
        rec = {"tx": tx, "entered": entered}
        if recovered and tx_mod.tx_hash(tx) in recovered:
            rec["recovered"] = True
        txs.append(rec)
    body = {
        "version": FORMAT_VERSION,
        "saved":   time.time(),
        "txs":     txs,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return len(records)


def load(path, with_recovered=False):
    """Read [(tx, entered)], oldest first, or [(tx, entered, recovered)] with
    with_recovered. Missing file is an empty pool. A
    file that cannot be read or has the wrong shape is renamed to .bad and
    treated as empty, so a damaged snapshot never stops a node starting."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = json.load(f)
        if body.get("version") != FORMAT_VERSION:
            raise ValueError("unknown snapshot version %r" % body.get("version"))
        out = []
        for rec in body["txs"]:
            tx, entered = rec["tx"], float(rec["entered"])
            if not isinstance(tx, dict):
                raise ValueError("entry is not a transaction")
            out.append((tx, entered, bool(rec.get("recovered", False))))
        out.sort(key=lambda r: r[1])
        return out if with_recovered else [(t, e) for t, e, _ in out]
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("[mempool] ignoring unreadable snapshot %s: %s", path, e)
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        return []
