"""HTTP API and browser UI for LapseCoin nodes.

Peer communication is handled separately over UDP. This module only serves
human-facing browser UI and a JSON API for wallets and block explorers.

Two Flask apps are created by the factory functions at the bottom of this file:

Public app  (default port 8333, externally reachable):
  UI:
    GET  /                            dashboard
    GET  /explorer                    recent block list
    GET  /explorer/block/<height>     block detail
    GET  /explorer/tx/<hash>          transaction detail
    GET  /address?addr=<addr>         address balance and history
    GET  /address/distribution/<n>    addresses in wealth-distribution bucket n
    GET  /whitepaper                  protocol whitepaper
    GET  /peers                       connected peer list
    GET  /send                        403 (local interface only)

  JSON API (Content-Type: application/json):
    GET  /api/info
         {"height", "tip_hash", "genesis_hash", "mempool_size",
          "address", "peer_count", "total_minted", "can_mint",
          "block_reward", "block_time_ratio", "status"}

    GET  /api/block/<height>          full block object or {"error": "not found"}
    GET  /api/tx/<hash>               transaction object (confirmed or mempool),
                                       plus "confirmations": 0 while unconfirmed,
                                       else current height minus the tx's block
                                       height, plus 1

    GET  /api/address/<addr>/balance
         {"address", "balance_ticks", "balance_lapse"}

    GET  /api/address/<addr>/history
         [{"height", "tx_hash", "direction": "sent"|"received", "tx"}, ...]

    GET  /api/address/<addr>/valid
         {"address", "valid": true|false}

    GET  /api/mempool
         {"size": <n>, "transactions": [{"hash", "from", "outputs", "fee"}, ...]}

    POST /api/tx/send                 rate-limited: 20 requests/second
         Request body (JSON): a signed plaintext tx dict, see tx.py
         (tx_mod.create): {"from", "pubkey", "outputs", "nonce", "fee",
         "signature", "memo"?}
         memo is optional, plaintext, at most tx.MAX_MEMO_BYTES bytes;
         omit the key entirely rather than sending an empty string
         Response:
           {"ok": true,  "tx_hash": <hex>}
           {"ok": false, "error": <string>}

Private app  (default port 8335 or public port +2, 127.0.0.1 only):
  All public UI and JSON API endpoints, plus:
    GET/POST /send                    build and sign a send transaction
    POST     /api/peers/add           {"host": <str>, "port": <int>}

Exchange / third-party integration:
  There is no dedicated integration API; the JSON API above is enough to
  build one, following the same shape most exchanges already expect from a
  Bitcoin-Core-style node (getbalance, listtransactions, gettransaction,
  validateaddress, sendtoaddress):
    - deposit detection: poll /api/address/<addr>/history for new entries
    - confirmation depth: the "confirmations" field on /api/tx/<hash>
    - address format check: /api/address/<addr>/valid
    - withdrawal: build and sign a tx exactly like tx.py's create() does,
      then POST it to /api/tx/send. Signing is never done over the network;
      an integrator holds and signs with their own keys, the same as any
      Bitcoin-style wallet integration would.

  One real structural difference from Bitcoin: a LapseCoin node has a
  single wallet address, not per-customer address generation (no
  getnewaddress equivalent). An integrator wanting one deposit address per
  customer needs to run one node per customer, or track customers by a
  memo/tag against a single shared address, the same way exchanges already
  handle XRP- or Monero-style account-index coins.
"""

import logging
import os
import secrets
import sys

import markdown
from flask import Flask, jsonify, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import block as block_mod
import crypto as crypto_mod
import state as state_mod
import storage as storage_mod
import tx as tx_mod
from params import TICKS_PER_LAPSE, SUPPLY_CAP
from version import LOCAL_VERSION

log = logging.getLogger("ec.api")

# Nodes keep full history, so both the block list and an address's
# transaction history are paginated rather than truncated to "recent N".
BLOCKS_PER_PAGE  = 8
PEERS_PER_PAGE   = 8
HISTORY_PER_PAGE = 3


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_balance(ticks):
    lapse = ticks // TICKS_PER_LAPSE
    rem = ticks % TICKS_PER_LAPSE
    return f"{lapse} LAPSE {rem:,} ticks"


def fmt_lapse(ticks):
    """Whole-LAPSE amount only, comma-grouped, for compact display."""
    return f"{ticks // TICKS_PER_LAPSE:,} LAPSE"


# ---------------------------------------------------------------------------
# Wealth distribution (holder-size histogram)
# ---------------------------------------------------------------------------

# Bucket edges in whole LAPSE (upper-exclusive, last bucket unbounded).
HOLDER_BUCKET_EDGES = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000]


def compute_holder_histogram(balances):
    """Count holders per order-of-magnitude LAPSE bucket. Returns a list of
    (label, count) pairs, smallest holders first."""
    lapse_amounts = [b // TICKS_PER_LAPSE for b in balances]
    edges = HOLDER_BUCKET_EDGES
    counts = [0] * (len(edges) + 1)
    for amt in lapse_amounts:
        i = 0
        while i < len(edges) and amt >= edges[i]:
            i += 1
        counts[i] += 1

    labels = [f"<{edges[0]:,}"]
    for lo, hi in zip(edges, edges[1:]):
        labels.append(f"{lo:,}-{hi:,}")
    labels.append(f"{edges[-1]:,}+")
    return list(zip(labels, counts))


def bucket_index_for_lapse(lapse_amt):
    """Which HOLDER_BUCKET_EDGES bucket a whole-LAPSE amount falls into.
    Mirrors the bucketing loop in compute_holder_histogram exactly, so a
    balance always lands in the same bucket as its own histogram bar."""
    edges = HOLDER_BUCKET_EDGES
    i = 0
    while i < len(edges) and lapse_amt >= edges[i]:
        i += 1
    return i


def bucket_label(i):
    """Label for bucket index i, matching compute_holder_histogram's labels."""
    edges = HOLDER_BUCKET_EDGES
    if i == 0:
        return f"<{edges[0]:,}"
    if i == len(edges):
        return f"{edges[-1]:,}+"
    return f"{edges[i - 1]:,}-{edges[i]:,}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tx_amount(t):
    """Total transfer amount for display."""
    return sum(o["amount"] for o in t.get("outputs", []))


def _pagination_window(page, total_pages, radius=2):
    """Page numbers to render as links: always the first and last page,
    the current page and `radius` neighbors on each side, and None where
    a gap between those is skipped (rendered as an ellipsis)."""
    if total_pages <= 1:
        return [1]
    keep = {1, total_pages}
    for p in range(page - radius, page + radius + 1):
        if 1 <= p <= total_pages:
            keep.add(p)
    window = []
    prev = None
    for p in sorted(keep):
        if prev is not None and p - prev > 1:
            window.append(None)
        window.append(p)
        prev = p
    return window


def _recent_committed_txs(chain, limit):
    """Most recently committed transactions across the chain, tip first.
    Walks blocks backward from the tip so this stays cheap even on a long
    chain with sparse blocks. It stops as soon as `limit` is reached."""
    rows = []
    for blk in reversed(chain):
        for t in reversed(blk.get("transactions", [])):
            rows.append((blk["height"], tx_mod.tx_hash(t), t, _tx_amount(t)))
            if len(rows) >= limit:
                return rows
    return rows


def _get_address_history(addr, node):
    v = node.view
    history = []
    for h_height, h in node.storage.get_tx_heights_for_addr(addr):
        chain = v.chain
        if 0 <= h_height < len(chain):
            for t in chain[h_height]["transactions"]:
                if tx_mod.tx_hash(t) == h:
                    direction = "sent" if t.get("from") == addr else "received"
                    history.append((h_height, h, direction, t))
                    break
    return history


def _block_reward(chain, height):
    """Mint reward for the block at `height`, replaying total_minted from
    genesis the same way _get_mined_blocks_for_addr and the real ledger do,
    so this always agrees with what the address page shows for the same
    block. 0 for genesis (no builder, nothing minted)."""
    total_minted = 0
    for h in range(height):
        r = state_mod.compute_reward(total_minted)
        if r >= 1:
            total_minted += r
    return state_mod.compute_reward(total_minted)


def _get_mined_blocks_for_addr(addr, node):
    """Blocks built by addr, with the reward + fees paid to the builder.

    Mining rewards never go through the mempool/AddrIndex (they're credited
    directly in chainstate._apply_builder_reward), so they can't be pulled
    from the same tx index as ordinary transfers. The chain is kept fully
    in memory though, so we can just walk it once, replaying total_minted
    the same way the ledger does, to recover the exact historical reward
    for each block.
    """
    mined = []
    total_minted = 0
    for height, blk in enumerate(node.view.chain):
        reward = state_mod.compute_reward(total_minted)
        if reward >= 1:
            total_minted += reward
        if blk.get("builder") == addr:
            fees = block_mod.block_fees(blk)
            mined.append((height, blk["hash"], reward + fees))
    return mined


def _parse_csv_outputs(outputs_raw):
    outputs, errors = [], []
    for i, line in enumerate(outputs_raw.strip().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) != 2:
            errors.append(f"Line {i}: expected 'address,amount'")
            continue
        addr, amt_str = parts[0].strip(), parts[1].strip()
        if not crypto_mod.is_valid_address(addr):
            errors.append(f"Line {i}: invalid address")
            continue
        try:
            amt = int(amt_str)
        except ValueError:
            errors.append(f"Line {i}: invalid amount '{amt_str}'")
            continue
        if amt < 0:
            errors.append(f"Line {i}: amount must not be negative")
            continue
        if amt == 0:
            # A zero-amount output is never valid on the wire (tx_mod.validate
            # rejects it), so this isn't a real output. It's the untouched
            # half of a prefilled "address,0" line the sender left as-is.
            continue
        outputs.append({"to": addr, "amount": amt})
    return outputs, errors


def fee_estimate(node):
    """Current mempool fee-per-byte picture for the send UI.

    Reuses block.assemble() itself (rather than reimplementing its
    fee-per-byte packing logic) to find the "next block" clearing rate, so
    this can never quietly drift out of sync with what actually gets a
    transaction included.

    Returns {"pending": int, "min": float, "median": float, "max": float,
    "next_block": float}. next_block is 0 when the mempool doesn't fill a
    block at all, any non-negative fee would be included right now.
    """
    pending = node.mempool.all_txs()
    if not pending:
        return {"pending": 0, "min": 0, "median": 0, "max": 0, "next_block": 0}

    rates = sorted(tx_mod.fee_rate(t) for t in pending)
    n = len(rates)
    median = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2

    v = node.view
    iterations = block_mod.get_vdf_iterations(v.chain)
    candidate = block_mod.assemble(v.tip, pending, v.tip.get("builder") or "", iterations)
    included = candidate["transactions"]
    # Full block: the going rate is the lowest fee-per-byte that still made
    # it in. Otherwise everything pending fits, so nothing is required to
    # clear the next block.
    next_block = min((tx_mod.fee_rate(t) for t in included), default=0) if len(included) < n else 0

    return {"pending": n, "min": rates[0], "median": median, "max": rates[-1],
            "next_block": next_block}


# ---------------------------------------------------------------------------
# Race-odds chart (plain inline SVG. No JS charting library, matches the
# rest of the site's self-contained, offline-friendly UI)
# ---------------------------------------------------------------------------

_CHART_W, _CHART_H = 860, 220
# The bottom padding was already empty space under the plot; the height
# axis moves into it rather than making the chart taller, so the page
# still fits without scrolling.
_CHART_PAD_L, _CHART_PAD_T, _CHART_PAD_B = 46, 10, 22


_TICK_COUNT = 5  # labeled horizontal gridlines, evenly spaced across the axis

# Labeled block heights along the bottom. Five is chosen to match the y
# axis, and because every one of them but the two pinned ends is a lever
# the reader can drag: four segments is enough to isolate a region without
# turning the axis into a row of controls to aim at.
_X_TICK_COUNT = 5
# Below this a window is too short for the interior ticks to land on
# distinct blocks, so it gets end labels only and nothing to drag.
_X_TICK_MIN_BLOCKS = 3 * _X_TICK_COUNT

# Builder identity colors, capped at 3: any two points on this chart can end
# up adjacent regardless of building order (it's effectively a scatter over
# time, not a fixed-order series), and a validated categorical palette only
# holds a colorblind- and normal-vision-safe distinction for *every* pair,
# not just neighboring ones, up to 3 slots, a 4th fails the normal-vision
# floor even with a legend (see dataviz skill's palette validator). Every
# other builder folds into "other" instead of a generated 4th-plus color.
_BUILDER_COLOR_SLOTS = ["var(--series-1)", "var(--series-2)", "var(--series-3)"]
_OTHER_COLOR = "var(--series-other)"


def _x_ticks(rows):
    """Labeled positions along the height axis, oldest first.

    `idx` is the position in the window, `height` the block it names, and
    `frac` where it sits across the plot as a fraction of the width. At
    rest the fracs are evenly spaced, which makes the axis plain linear
    and identical to what it has always drawn.

    They are fractions rather than pixels because the page stores whatever
    arrangement the reader drags them into, and the window slides as blocks
    arrive: a saved height is a different block ten minutes later, while a
    saved "this tick sits 73% across" is still the same arrangement. The
    labels re-derive from whichever heights are in the window now.
    """
    n = len(rows)
    if n < _X_TICK_MIN_BLOCKS:
        idxs = sorted({0, n - 1})
    else:
        idxs = sorted({round(i * (n - 1) / (_X_TICK_COUNT - 1))
                       for i in range(_X_TICK_COUNT)})
    # The rest frac comes from the block index, not from the tick number.
    # Interior indices are rounded to land on real blocks, so spacing them
    # evenly by tick number instead would leave the axis subtly non-linear
    # before anyone has touched it, and every point between two ticks
    # drawn a few pixels off where the server itself put it.
    span = max(n - 1, 1)
    return [{"idx": idx, "height": rows[idx][0], "frac": round(idx / span, 6)}
            for idx in idxs]


def _race_chart(race):
    """Precompute SVG pixel geometry for the race-odds chart.

    Axis range is a display decision, kept separate from the stats: it
    hugs the typical cluster of values (within 2x of the median either
    way), not the raw min/max, so one real stall doesn't compress every
    other point into a sliver at the bottom of the chart. That point
    still renders, clipped to the plot edge with a marker, and its real
    value is still in the tooltip. This has zero effect on race_odds's
    own median/odds_pct, which already use every interval unclipped (see
    that function's docstring). Only where the line gets drawn changes.

    Points are colored by builder to make dominance visible: the top 3
    builders (by block count in this window) each get a fixed, validated
    color; everyone else shares one neutral "other" color plus a legend
    entry, rather than an unbounded set of generated colors, see
    _BUILDER_COLOR_SLOTS.
    """
    rows = race["window"]
    n = len(rows)
    plot_w = _CHART_W - _CHART_PAD_L
    plot_h = _CHART_H - _CHART_PAD_T - _CHART_PAD_B
    median = race["median"]
    own_seconds = race["own_seconds"]

    typical = [s for _, s, _ in rows if median / 2 <= s <= median * 2] or [s for _, s, _ in rows]
    domain = list(typical)
    if own_seconds is not None:
        domain.append(own_seconds)
    data_lo, data_hi = min(domain), max(domain)
    margin = max((data_hi - data_lo) * 0.08, 1.0)
    axis_lo = max(0.0, data_lo - margin)
    axis_hi = data_hi + margin

    def x_at(idx):
        return _CHART_PAD_L + (idx / max(n - 1, 1)) * plot_w

    def y_at(seconds):
        clamped = min(max(seconds, axis_lo), axis_hi)
        frac = (clamped - axis_lo) / (axis_hi - axis_lo)
        return _CHART_PAD_T + plot_h - frac * plot_h

    builder_counts = {}
    for _, _, builder in rows:
        if builder:
            builder_counts[builder] = builder_counts.get(builder, 0) + 1
    # Which three get a color is by block count, but ties break on the
    # address so the same three are chosen every time rather than by
    # whatever order the chain happened to put equal builders in.
    top_builders = sorted(builder_counts,
                          key=lambda b: (-builder_counts[b], b))[:3]
    # Which color each one gets is by address, not by rank. Assigning by
    # rank meant two builders one block apart swapped colors the moment
    # they swapped places, which is every time either of them wins, so the
    # chart appeared to recolor itself constantly while nothing about who
    # built what had changed. Sorting the chosen three by address instead
    # makes a builder's color depend only on which builders are on screen.
    color_by_builder = {b: _BUILDER_COLOR_SLOTS[i]
                        for i, b in enumerate(sorted(top_builders))}

    points = [{"x": round(x_at(idx), 1), "y": round(y_at(seconds), 1),
               "idx": idx, "height": h, "seconds": seconds,
               "clipped": seconds > axis_hi or seconds < axis_lo,
               "builder": builder, "color": color_by_builder.get(builder, _OTHER_COLOR)}
              for idx, (h, seconds, builder) in enumerate(rows)]

    legend = [{"label": b, "color": color_by_builder[b], "count": builder_counts[b]}
              for b in top_builders]
    other_count = sum(c for b, c in builder_counts.items() if b not in color_by_builder)
    if other_count:
        legend.append({"label": None, "color": _OTHER_COLOR, "count": other_count})

    own_y = own_clipped = None
    if own_seconds is not None:
        own_y = round(y_at(own_seconds), 1)
        own_clipped = own_seconds > axis_hi or own_seconds < axis_lo

    # The end ticks sit exactly on the plot edges, which are also where
    # anything off scale gets clamped to. Say so on the axis when that
    # actually happened: a line resting on the edge is otherwise reading
    # against a bound that means "this or more" while the label says a
    # plain number. Marked only when something really is clipped there,
    # since with nothing off scale the bound is just a bound.
    clipped_hi = any(s > axis_hi for _, s, _ in rows) or (
        own_seconds is not None and own_seconds > axis_hi)
    clipped_lo = any(s < axis_lo for _, s, _ in rows) or (
        own_seconds is not None and own_seconds < axis_lo)

    ticks = []
    for i in range(_TICK_COUNT):
        value = axis_lo + i * (axis_hi - axis_lo) / (_TICK_COUNT - 1)
        suffix = ""
        if i == _TICK_COUNT - 1 and clipped_hi:
            suffix = "+"
        elif i == 0 and clipped_lo:
            suffix = "-"
        ticks.append({"value": value, "y": round(y_at(value), 1), "suffix": suffix})

    # plot_w/pad_l go out with the rest because the page recomputes each
    # point's x when the reader drags the height axis around. Everything
    # else, y included, is unaffected by that: dragging redistributes the
    # width the same points are drawn across, it never changes which
    # points are on screen, so the vertical scale and its clipping stay
    # exactly as computed here.
    return {"points": points, "own_y": own_y, "own_clipped": own_clipped,
            "median_y": round(y_at(median), 1), "ticks": ticks,
            "x_ticks": _x_ticks(rows),
            "width": _CHART_W, "height": _CHART_H,
            "pad_l": _CHART_PAD_L, "plot_w": plot_w,
            "plot_top": _CHART_PAD_T, "plot_bottom": _CHART_PAD_T + plot_h,
            "axis_lo": axis_lo, "axis_hi": axis_hi, "legend": legend}


def _peers_for_download(known_addrs, self_addr):
    """The address list /api/peers/download hands out: known_addrs plus
    self_addr, deduped, self_addr omitted entirely when not yet known.
    Pulled out as a pure function so it's testable without a running app;
    see api_peers_download for why self is included at all."""
    if self_addr and self_addr not in known_addrs:
        return known_addrs + [self_addr]
    return list(known_addrs)


def _default_send_outputs(pool):
    """One 'wallet,0' line per known peer with a confirmed wallet address,
    so the sender can just change the one 0 they actually want to send
    and leave the rest, _parse_csv_outputs drops any line still at 0."""
    seen, lines = set(), []
    for row in sorted(pool.snapshot(), key=lambda r: r[1], reverse=True):
        wallet = row[4]
        if wallet and wallet not in seen:
            seen.add(wallet)
            lines.append(f"{wallet},0")
    return "\n".join(lines)


def _submit_and_alert(node, outputs, fee, passphrase, ctx, memo=""):
    if not passphrase:
        ctx["alert_err"] = "Passphrase required."
        return
    try:
        t, _fee = node.build_and_sign_tx(outputs, fee=fee, passphrase=passphrase or None,
                                          memo=memo)
        ok, result = node.submit_tx_from_api(t)
        if ok:
            ctx["alert_ok_tx"]   = result
            ctx["alert_ok_verb"] = "Submitted."
        else:
            ctx["alert_err"] = f"Error: {result}"
    except Exception as e:
        log.warning("[api] tx build/submit failed  err=%s", e)
        ctx["alert_err"] = f"Error: {e}"


def _shared_read_only_routes(app, node, pool, limiter,
                              private_port, public_port, is_private,
                              update_checker=None, rewarder=None):
    """Register all read-only UI and API routes on app."""
    # Use a prefix so public and private apps don't collide on endpoint names
    pfx = "priv_" if is_private else "pub_"

    @app.context_processor
    def inject_ctx():
        endpoint = (request.endpoint or "").split(".")[-1]
        for prefix in ("pub_", "priv_"):
            if endpoint.startswith(prefix):
                endpoint = endpoint[len(prefix):]
        nav_active = {
            "dashboard": "dashboard", "explorer": "explorer",
            "block_detail": "explorer", "tx_detail": "explorer",
            "address_lookup": "address", "distribution_bucket": "address",
            "peers": "peers", "odds": "odds",
            "whitepaper": "whitepaper", "send": "send", "rewards": "rewards",
            "settings": "settings",
        }.get(endpoint)
        return {"is_private": is_private,
                "private_port": private_port,
                "public_port": public_port,
                "update_checker": update_checker,
                # Remaining budget for the masthead badge, visible on both
                # apps, the full settings page (/rewards) only exists on
                # the private one. 0 (the default) reads as "off".
                "rewards_remaining_lapse":
                    rewarder.status()["remaining_ticks"] / TICKS_PER_LAPSE if rewarder else 0,
                "nav_active": nav_active}

    @app.route("/favicon.svg", endpoint=pfx+"favicon")
    def favicon():
        # Served straight from the repo's actual lapsecoin.svg (rather than a
        # copy baked into the HTML) so the browser tab icon always matches
        # whatever the file on disk currently looks like.
        return send_file(os.path.join(_base_dir(), "lapsecoin.svg"),
                         mimetype="image/svg+xml", max_age=3600)

    # ---- UI pages --------------------------------------------------------

    @app.route("/", endpoint=pfx+"dashboard")
    def dashboard():
        info = node.get_info()
        chain = node.view.chain
        return render_template("dashboard.html", title="Dashboard",
            info=info, supply_cap=SUPPLY_CAP,
            recent_txs=_recent_committed_txs(chain, limit=6))

    @app.route("/explorer", endpoint=pfx+"explorer")
    def explorer():
        chain = node.view.chain
        total = len(chain)
        total_pages = max(-(-total // BLOCKS_PER_PAGE), 1)
        page  = min(max(request.args.get("page", 1, type=int) or 1, 1), total_pages)
        end   = max(total - (page - 1) * BLOCKS_PER_PAGE, 0)
        start = max(end - BLOCKS_PER_PAGE, 0)
        return render_template("explorer.html", title="Explorer",
            recent=chain[start:end][::-1], page=page, total_pages=total_pages,
            page_window=_pagination_window(page, total_pages),
            has_prev=page > 1, has_next=start > 0)

    @app.route("/explorer/block/<int:height>", endpoint=pfx+"block_detail")
    def block_detail(height):
        chain = node.view.chain
        if height < 0 or height >= len(chain):
            return render_template("error.html", title="Not found",
                message="Block not found."), 404
        b = chain[height]
        tx_rows = [(tx_mod.tx_hash(t), t, _tx_amount(t)) for t in b["transactions"]]
        reward = (_block_reward(chain, height) + block_mod.block_fees(b)
                  if b.get("builder") else 0)
        return render_template("block_detail.html", title=f"Block {height}",
            b=b, tx_rows=tx_rows, has_next=height + 1 < len(chain),
            time_stats=block_mod.block_time_stats(chain, height), reward=reward)

    @app.route("/explorer/tx/<tx_hash>", endpoint=pfx+"tx_detail")
    def tx_detail(tx_hash):
        found = found_height = None
        height = node.storage.get_tx_height(tx_hash)
        if height is not None:
            chain = node.view.chain
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == tx_hash:
                        found, found_height = t, height
                        break
        if not found:
            found = node.mempool.get(tx_hash)
        if not found:
            return render_template("error.html", title="Not found",
                message="Transaction not found."), 404
        location = (f"Block {found_height}"
                    if found_height is not None else "Mempool (unconfirmed)")
        return render_template("tx_detail.html", title="Transaction",
            tx_hash=tx_hash, tx=found, location=location)

    @app.route("/address", methods=["GET", "POST"], endpoint=pfx+"address_lookup")
    def address_lookup():
        addr = request.args.get("addr", "").strip()
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        v = node.view
        # The distribution histogram is only shown before a lookup runs, so
        # skip computing it once an address has actually been submitted.
        holder_count, histogram, histogram_max = 0, [], 0
        if not addr:
            all_balances = v.state.get_all_balances()
            holder_count = len(all_balances)
            histogram = compute_holder_histogram(all_balances)
            histogram_max = max((c for _, c in histogram), default=0)
        ctx = dict(title="Balance", addr=addr, alert_err="", page=page,
                   history=None, balance=0, tx_count=0, has_prev=False, has_next=False,
                   holder_count=holder_count,
                   histogram=histogram, histogram_max=histogram_max)
        if addr and not crypto_mod.is_valid_address(addr):
            ctx["alert_err"] = "Invalid address format."
            ctx["addr"] = ""
        elif addr:
            ctx["balance"]  = v.state.get_balance(addr)
            ctx["tx_count"] = v.state.get_nonce(addr)
            tx_rows = [(h, hsh, direction, t, "tx")
                       for h, hsh, direction, t in _get_address_history(addr, node)]
            mined_rows = [(h, hsh, "mined", amount, "block")
                          for h, hsh, amount in _get_mined_blocks_for_addr(addr, node)]
            newest_first = sorted(tx_rows + mined_rows, key=lambda r: r[0], reverse=True)
            total_pages = max(-(-len(newest_first) // HISTORY_PER_PAGE), 1)
            page = min(page, total_pages)
            start = (page - 1) * HISTORY_PER_PAGE
            end   = start + HISTORY_PER_PAGE
            ctx["page"] = page
            ctx["total_pages"] = total_pages
            ctx["page_window"] = _pagination_window(page, total_pages)
            ctx["history"]  = newest_first[start:end]
            ctx["has_prev"] = page > 1
            ctx["has_next"] = end < len(newest_first)
        return render_template("address.html", **ctx)

    @app.route("/address/distribution/<int:bucket>", endpoint=pfx+"distribution_bucket")
    def distribution_bucket(bucket):
        edges = HOLDER_BUCKET_EDGES
        if bucket < 0 or bucket > len(edges):
            return render_template("error.html", title="Not found",
                message="No such distribution bucket."), 404
        v = node.view
        rows = sorted(
            ((addr, bal) for addr, bal in v.state.all_balances().items()
             if bucket_index_for_lapse(bal // TICKS_PER_LAPSE) == bucket),
            key=lambda r: r[1], reverse=True)
        total = len(rows)
        total_pages = max(-(-total // BLOCKS_PER_PAGE), 1)
        page = min(max(request.args.get("page", 1, type=int) or 1, 1), total_pages)
        start = (page - 1) * BLOCKS_PER_PAGE
        end = start + BLOCKS_PER_PAGE
        return render_template("distribution.html", title="Distribution",
            label=bucket_label(bucket), bucket=bucket, rows=rows[start:end], total=total,
            page=page, total_pages=total_pages,
            page_window=_pagination_window(page, total_pages),
            has_prev=page > 1, has_next=end < total)

    @app.route("/whitepaper", endpoint=pfx+"whitepaper")
    def whitepaper():
        import sys
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        # Also try one level up in case api.py is in a subdirectory
        for candidate in [
            os.path.join(base, "docs", "whitepaper.md"),
            os.path.join(os.path.dirname(base), "docs", "whitepaper.md"),
            os.path.join(os.getcwd(), "docs", "whitepaper.md"),
        ]:
            if os.path.isfile(candidate):
                try:
                    with open(candidate) as f:
                        rendered = markdown.markdown(
                            f.read(), extensions=["fenced_code", "tables"])
                    return render_template("whitepaper.html", title="Whitepaper",
                                           rendered=rendered)
                except Exception:
                    break
        rendered = "<p>whitepaper.md not found.</p>"
        return render_template("whitepaper.html", title="Whitepaper",
                               rendered=rendered)

    def _self_external_addr():
        # node.gossip (and its .udp) may not exist on every node object this
        # is called with, e.g. lightweight test doubles, and plain
        # getattr(node.gossip.udp, ...) still evaluates node.gossip first,
        # so it can't catch a missing .gossip itself. Walk it defensively.
        try:
            return node.gossip.udp.our_external_addr
        except AttributeError:
            return None

    def _peer_dicts(rows):
        return [
            {"address": addr, "last_seen": int(last_seen), "active": active,
             "height": height, "wallet": wallet,
             "version": version,
             "http_reachable": http_reachable}
            for addr, last_seen, active, height, wallet, version, http_reachable in rows
        ]

    def _self_info():
        return {"wallet": node.advertised_addr, "height": node.view.chain[-1].get("height", 0),
                "version": LOCAL_VERSION, "addr": _self_external_addr()}

    @app.route("/peers", endpoint=pfx+"peers")
    def peers():
        all_rows = sorted(pool.snapshot(), key=lambda r: r[1], reverse=True)
        total_pages = max(-(-len(all_rows) // PEERS_PER_PAGE), 1)
        page  = min(max(request.args.get("page", 1, type=int) or 1, 1), total_pages)
        start = (page - 1) * PEERS_PER_PAGE
        end   = start + PEERS_PER_PAGE
        self_height = node.view.chain[-1].get("height", 0)
        return render_template("peers.html", title="Peers", rows=all_rows[start:end],
                               peer_count=len(all_rows), page=page, total_pages=total_pages,
                               page_window=_pagination_window(page, total_pages),
                               has_prev=page > 1, has_next=end < len(all_rows),
                               self_height=self_height, self_wallet=node.advertised_addr,
                               self_version=LOCAL_VERSION, self_addr=_self_external_addr())

    @app.route("/odds", endpoint=pfx+"odds")
    def odds():
        race = block_mod.race_odds(node.view.chain, node.own_vdf_median())
        chart = _race_chart(race) if race else None
        return render_template("odds.html", title="Race Odds", race=race,
                               chart=chart, reorgs=node.reorg_stats())

    # ---- JSON API (read-only) --------------------------------------------

    @app.route("/api/odds", endpoint=pfx+"api_odds")
    def api_odds():
        race = block_mod.race_odds(node.view.chain, node.own_vdf_median())
        if not race:
            return jsonify(None)
        return jsonify({
            "median": race["median"],
            "own_seconds": race["own_seconds"], "odds_pct": race["odds_pct"],
            "window_len": len(race["window"]), "chart": _race_chart(race),
            "reorgs": node.reorg_stats(),
        })

    @app.route("/api/peers", endpoint=pfx+"api_peers")
    def api_peers():
        all_rows = sorted(pool.snapshot(), key=lambda r: r[1], reverse=True)
        total_pages = max(-(-len(all_rows) // PEERS_PER_PAGE), 1)
        page  = min(max(request.args.get("page", 1, type=int) or 1, 1), total_pages)
        start = (page - 1) * PEERS_PER_PAGE
        end   = start + PEERS_PER_PAGE
        return jsonify({
            "self": _self_info(),
            "peer_count": len(all_rows),
            "peers": _peer_dicts(all_rows[start:end]),
        })

    @app.route("/api/peers/download", endpoint=pfx+"api_peers_download")
    def api_peers_download():
        # Same shape discovery.py's own PEER_CACHE_FILE reads and writes
        # (a flat list of "ip:port" strings, pool.all_addrs()). This is
        # meant to be saved as lapsecoin_peers.json in a new node's working
        # directory so it bootstraps from it on startup, not just a data
        # export for humans to read.
        #
        # This node's own address is included too, deliberately: the
        # whole point of handing this file to someone starting a new
        # node is that they can reach nodes on it, and this one is a
        # perfectly good candidate the moment it exists. It's excluded
        # everywhere a node reads a peers file back in (see
        # Discovery._is_own_addr), so a node loading its own exported
        # file, or one two nodes swapped, never tries to dial itself.
        addrs = _peers_for_download(pool.all_addrs(), _self_external_addr())
        resp = jsonify(addrs)
        resp.headers["Content-Disposition"] = 'attachment; filename="lapsecoin_peers.json"'
        return resp

    @app.route("/api/info", endpoint=pfx+"api_info")
    def api_info():
        info = dict(node.get_info())
        chain = node.view.chain
        info["recent_txs"] = [
            {"height": height, "hash": h, "from": t.get("from", ""), "amount": amount}
            for height, h, t, amount in _recent_committed_txs(chain, limit=6)
        ]
        return jsonify(info)

    @app.route("/api/block/<int:height>", endpoint=pfx+"api_block")
    def api_block(height):
        chain = node.view.chain
        if 0 <= height < len(chain):
            return jsonify(chain[height])
        return jsonify({"error": "not found"}), 404

    @app.route("/api/tx/<tx_hash_val>", endpoint=pfx+"api_get_tx")
    def api_get_tx(tx_hash_val):
        t = node.mempool.get(tx_hash_val)
        if t:
            return jsonify(dict(t, confirmations=0))
        height = node.storage.get_tx_height(tx_hash_val)
        if height is not None:
            chain = node.view.chain
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == tx_hash_val:
                        confirmations = len(chain) - height
                        return jsonify(dict(t, confirmations=confirmations))
        return jsonify({"error": "not found"}), 404

    @app.route("/api/address/<addr>/valid", endpoint=pfx+"api_valid_address")
    def api_valid_address(addr):
        return jsonify({"address": addr, "valid": crypto_mod.is_valid_address(addr)})

    @app.route("/api/address/<addr>/balance", endpoint=pfx+"api_balance")
    def api_balance(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        balance = node.view.state.get_balance(addr)
        return jsonify({"address": addr, "balance_ticks": balance,
                        "balance_lapse": balance / TICKS_PER_LAPSE})

    @app.route("/api/address/<addr>/history", endpoint=pfx+"api_history")
    def api_history(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        return jsonify([
            {"height": h, "tx_hash": th, "direction": d, "tx": t}
            for h, th, d, t in _get_address_history(addr, node)
        ])

    @app.route("/api/mempool", endpoint=pfx+"api_mempool")
    def api_mempool():
        txs = node.mempool.all_txs()

        def _summarize(t):
            return {"hash": tx_mod.tx_hash(t), "from": t["from"],
                    "outputs": t["outputs"], "fee": t["fee"]}

        return jsonify({"size": len(txs), "transactions": [_summarize(t) for t in txs]})

    @app.route("/api/tx/send", methods=["POST"], endpoint=pfx+"api_send_tx")
    @limiter.limit("20 per second")
    def api_send_tx():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"ok": False, "error": "no JSON body"}), 400
        ok, result = node.submit_tx_from_api(data)
        if ok:
            return jsonify({"ok": True, "tx_hash": result})
        return jsonify({"ok": False, "error": result}), 400


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PyInstaller-aware base path
# ---------------------------------------------------------------------------

def _close_db_after_request(app):
    """Release this thread's database connection when the request ends.

    Both apps run on Werkzeug's threaded server, which handles every
    request on a brand new thread, and peewee keeps its connection in
    thread-local state: the first query a thread runs opens a connection
    of its own. Nothing closes it when the thread exits, so each request
    that touches storage leaves an open sqlite handle behind (two, with
    WAL) until the garbage collector happens to get to it. A polled
    endpoint opens them faster than that, and the process dies on EMFILE
    with hundreds of handles to the same database file.

    Registered per app rather than inside a route, so it also covers any
    route added later that touches storage, and covers the ones that do so
    indirectly: reading a setting is a database read, which is easy to
    forget when it looks like a plain attribute (Node.advertised_addr).

    Only ever closes the calling thread's own connection, which is why
    this cannot disturb the node loop's.
    """
    @app.teardown_request
    def _teardown(_exc=None):
        try:
            if not storage_mod.db.is_closed():
                storage_mod.db.close()
        except Exception:
            log.debug("[api] closing request db connection failed", exc_info=True)


def _base_dir():
    """Return the directory that contains templates_html/, working both from
    source (repo root) and inside a PyInstaller bundle (sys._MEIPASS)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


# Public app factory  (port 8333)
# ---------------------------------------------------------------------------

def create_app(node, pool, private_port=8335, public_port=8333,
               update_checker=None, rewarder=None):
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_lapse=fmt_lapse,
                                 TICKS_PER_LAPSE=TICKS_PER_LAPSE)
    app.logger.setLevel(logging.WARNING)
    # Deliberately not touching the werkzeug logger. main.py already sets it
    # to ERROR, and this line used to put it back to INFO, which is a
    # per-request access log: the dashboard polls /api/info on a timer and
    # every peer's reachability prober hits /api/info too, so it buried
    # everything the node itself had to say. An operator who wants the
    # access log can raise that logger; nothing here should decide it for
    # them, least of all by overriding what startup chose.
    _close_db_after_request(app)

    # Public port is externally reachable; give every route a sane default
    # so a route added later isn't unprotected by omission. /api/tx/send
    # keeps its own stricter per-route limit on top of this.
    limiter = Limiter(get_remote_address, app=app,
                      default_limits=["60 per minute"], storage_uri="memory://")

    _shared_read_only_routes(app, node, pool, limiter,
                             private_port, public_port, is_private=False,
                             update_checker=update_checker, rewarder=rewarder)

    # Send disabled on public port; show locked page
    @app.route("/send")
    def send_locked():
        return render_template("error.html", title="Send",
            message=f"Send is only available on the local interface "
                    f"(localhost:{private_port})."), 403

    # Settings disabled on public port; show locked page
    @app.route("/settings")
    def settings_locked():
        return render_template("error.html", title="Settings",
            message=f"Settings are only available on the "
                    f"local interface (localhost:{private_port})."), 403

    return app


# ---------------------------------------------------------------------------
# Private app factory  (port 8335, 127.0.0.1 only)
# ---------------------------------------------------------------------------

def create_private_app(node, pool, private_port=8335, public_port=8333,
                       update_checker=None, rewarder=None):
    """Full-featured app for local use. Never expose via Funnel or public port."""
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_lapse=fmt_lapse,
                                 TICKS_PER_LAPSE=TICKS_PER_LAPSE)
    app.logger.setLevel(logging.WARNING)
    _close_db_after_request(app)

    limiter = Limiter(get_remote_address, app=app, default_limits=[],
                      storage_uri="memory://")

    # Per-process CSRF token for the /send form (and /rewards below, same
    # reasoning applies). This is a private, single-user, 127.0.0.1-only
    # app with no session/login, so a synchronizer token that's fixed for
    # the process lifetime (rather than per-request) is sufficient:
    # same-origin policy already stops a cross-site page from reading it
    # out of the rendered page, so all it needs to do is not be guessable
    # and not travel to another origin. Both hold here since it's
    # rendered in a hidden field, never in a URL.
    csrf_token = secrets.token_hex(32)

    _shared_read_only_routes(app, node, pool, limiter,
                             private_port, public_port, is_private=True,
                             update_checker=update_checker, rewarder=rewarder)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        import settings as settings_mod
        balance_lapse = node.view.state.get_balance(node.addr) / TICKS_PER_LAPSE
        ctx = dict(title="Settings", csrf_token=csrf_token,
                   alert_ok="", alert_err="", rewarder_available=rewarder is not None,
                   balance_lapse=balance_lapse,
                   suggested_lapse=balance_lapse * 0.05)
        if rewarder is not None:
            status = rewarder.status()
            ctx["remaining_lapse"] = status["remaining_ticks"] / TICKS_PER_LAPSE
            ctx["pending"] = status["pending"]

        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
                ctx["alert_err"] = "Session expired; reload the page and try again."
            else:
                errors = []
                if rewarder is not None:
                    budget_raw = request.form.get("budget_lapse", "").strip()
                    try:
                        new_budget = float(budget_raw)
                        if new_budget < 0:
                            raise ValueError
                        current = rewarder.status()["remaining_ticks"] / TICKS_PER_LAPSE
                        rewarder.adjust_budget(new_budget - current)
                        ctx["remaining_lapse"] = new_budget
                    except ValueError:
                        errors.append("Budget must be a non-negative number.")

                # Anything forced by an environment variable is shown but
                # not editable here: a launch-time override shouldn't be
                # silently rewritten by a page that can't see it.
                for setting in settings_mod.ALL:
                    if node.settings.forced_by_env(setting):
                        continue
                    if setting.kind is bool:
                        node.settings.set(setting, setting.key in request.form)
                        continue
                    raw = request.form.get(setting.key, "").strip()
                    if raw == "" and setting.default == "":
                        node.settings.set(setting, "")   # clearing is meaningful here
                        continue
                    try:
                        # Check before storing. Writing an unparseable value
                        # would leave the operator looking at a page that
                        # says saved while the node quietly runs the
                        # default, since that is what get() falls back to.
                        node.settings.set(setting, setting.parse(raw))
                    except (TypeError, ValueError) as e:
                        errors.append(f"{setting.label}: {e}"
                                      if str(e) else f"{setting.label} is not valid.")

                if errors:
                    ctx["alert_err"] = " ".join(errors)
                else:
                    ctx["alert_ok"] = "Settings saved."

        ctx["settings"] = [
            {"key": s_.key, "label": s_.label, "help": s_.help,
             "is_bool": s_.kind is bool,
             "value": node.settings.get(s_),
             "forced": node.settings.forced_by_env(s_),
             "env_name": s_.env_name}
            for s_ in settings_mod.ALL
        ]
        ctx["advertised_addr"] = node.advertised_addr
        ctx["own_addr"] = node.addr
        ctx["privacy_addr"] = node.privacy_addr
        return render_template("settings.html", **ctx)

    @app.route("/send", methods=["GET", "POST"])
    def send():
        v = node.view
        ctx = dict(title="Send", from_addr=node.addr,
                   balance=v.state.get_balance(node.addr),
                   fees=fee_estimate(node), csrf_token=csrf_token,
                   outputs_value=_default_send_outputs(pool),
                   memo_value="", memo_max_bytes=tx_mod.MAX_MEMO_BYTES,
                   alert_ok_tx="", alert_ok_verb="", alert_err="", alert_err_lines=[])
        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
                ctx["alert_err"] = "Session expired; reload the page and try again."
                return render_template("send.html", **ctx)
            outputs_raw = request.form.get("outputs", "").strip()
            fee_raw     = request.form.get("fee", "0").strip()
            passphrase  = request.form.get("passphrase", "").strip()
            memo        = request.form.get("memo", "").strip()
            csv_file    = request.files.get("csv_file")
            if csv_file and csv_file.filename:
                outputs_raw = csv_file.read().decode()
            ctx["outputs_value"] = outputs_raw
            ctx["memo_value"] = memo
            outputs, errors = _parse_csv_outputs(outputs_raw)
            try:
                fee = int(fee_raw or "0")
                if fee < 0:
                    raise ValueError
            except ValueError:
                errors.append("Fee must be a non-negative integer.")
                fee = 0
            if len(memo.encode("utf-8")) > tx_mod.MAX_MEMO_BYTES:
                errors.append(f"Memo exceeds {tx_mod.MAX_MEMO_BYTES} bytes.")
            if errors:
                ctx["alert_err_lines"] = errors
            elif not outputs:
                ctx["alert_err"] = "No valid outputs."
            else:
                _submit_and_alert(node, outputs, fee, passphrase, ctx, memo=memo)
                if ctx["alert_ok_tx"]:
                    ctx["alert_ok_verb"] = "Sent."
                    ctx["outputs_value"] = ""
                    ctx["memo_value"] = ""
        return render_template("send.html", **ctx)

    @app.route("/api/peers/add", methods=["POST"])
    def api_add_peer():
        data = request.get_json(silent=True)
        host = data.get("host") if data else None
        port = data.get("port") if data else None
        if (isinstance(host, str) and host
                and isinstance(port, int) and 0 < port <= 65535):
            pool.add(f"{host}:{port}", allow_private=True)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "need valid host and port"}), 400

    return app
