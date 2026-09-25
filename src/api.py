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
    GET  /network                     connected peer list
    GET  /board                       public board (tagged-memo transactions)
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

    GET  /api/address/<addr>/history[?limit=<n>&offset=<n>]
         [{"height", "tx_hash", "direction": "sent"|"received", "tx"}, ...]
         Newest first. Returns everything when limit is omitted, which is
         what it has always done; limit/offset select a slice. The full
         count is in the X-Total-Count response header either way, so a
         caller can page without first fetching everything to find out
         how much there is.

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

import collections
import logging
import math
import os
import re
import secrets
import sys
import threading

import markdown
from markupsafe import Markup, escape
from flask import Flask, jsonify, redirect, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import block as block_mod
import crypto as crypto_mod
import state as state_mod
import settings as settings_mod
import storage as storage_mod
import tx as tx_mod
from params import TICKS_PER_LAPSE, SUPPLY_CAP, MIN_RELAY_FEE_RATE
from version import LOCAL_VERSION

log = logging.getLogger("ec.api")

# Nodes keep full history, so both the block list and an address's
# transaction history are paginated rather than truncated to "recent N".
BLOCKS_PER_PAGE  = 8
HISTORY_PER_PAGE = 3
DASHBOARD_TXS_PER_PAGE = 6
BOARD_PER_PAGE = 20

# Held peers are already bounded by MAX_PEERS (125), but what they claim
# isn't: up to PEERS_PER_MESSAGE_LIMIT (50) addresses from each of up to
# MAX_PEERS introducers is a theoretical ~6,250 addresses this node never
# itself reached. Uncapped, that's both a large response on every 8s poll
# and an unreadable graph. See _capped_claims.
CLAIMED_GRAPH_LIMIT = 60

# Board tagging, burn amount and the fee-floor staircase are all consensus
# rules now (see tx.py: is_board_post, board_fee_floor and friends), not
# server-side UI defaults, so this module just aliases them.
BOARD_MEMO_TAG    = tx_mod.BOARD_MEMO_TAG
BOARD_POST_AMOUNT = tx_mod.BOARD_POST_AMOUNT



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


def fmt_lapse_dp(ticks, places=4):
    """LAPSE with decimals: "28,708.2599 LAPSE".

    For headline figures, where "28,708 LAPSE 25,987,856 ticks" is nine
    digits of tick that wrap the line and answer nothing anyone asked. The
    exact tick count is still on /api/info for anything that needs it.
    """
    return f"{ticks / TICKS_PER_LAPSE:,.{places}f} LAPSE"


def fmt_duration(seconds):
    """A span as the two largest units that fit: "1y 24d", "3d 4h", "12m".

    Two units, never more: the point is a glanceable age, and seconds of
    precision on something measured in days is noise dressed as detail.
    """
    seconds = max(int(seconds or 0), 0)
    units = (("y", 31_536_000), ("d", 86_400), ("h", 3_600), ("m", 60))
    parts = []
    for suffix, size in units:
        if seconds >= size or parts:
            count, seconds = divmod(seconds, size)
            if count or parts:
                parts.append(f"{count}{suffix}")
            if len(parts) == 2:
                return " ".join(parts)
    return " ".join(parts) if parts else "just now"


# ---------------------------------------------------------------------------
# Wealth distribution (holder-size histogram)
# ---------------------------------------------------------------------------

# Bucket edges in whole LAPSE (upper-exclusive, last bucket unbounded).
HOLDER_BUCKET_EDGES = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000]


def compute_holder_histogram(balances):
    """Count holders per order-of-magnitude LAPSE bucket. Returns a list of
    (label, count) pairs, smallest holders first."""
    edges = HOLDER_BUCKET_EDGES
    counts = [0] * (len(edges) + 1)
    for bal in balances:
        # Through the shared index helper rather than a second copy of the
        # same loop, which is what this was. bucket_index_for_lapse' own
        # docstring promised it "mirrors the bucketing loop exactly", and a
        # promise like that is better kept by there being one loop.
        counts[bucket_index_for_lapse(bal // TICKS_PER_LAPSE)] += 1

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


def _recent_committed_txs(chain, limit, offset=0):
    """Most recently committed transactions across the chain, tip first.

    Walks blocks backward from the tip so this stays cheap even on a long
    chain with sparse blocks: it touches offset+limit transactions and
    stops, never the whole chain, so a later page costs no more than the
    size of the page before it.
    """
    rows, skipped = [], 0
    for blk in reversed(chain):
        for t in reversed(blk.get("transactions", [])):
            if skipped < offset:
                skipped += 1
                continue
            rows.append((blk["height"], tx_mod.tx_hash(t), t, _tx_amount(t)))
            if len(rows) >= limit:
                return rows
    return rows


# A tiny, safe markdown-like subset for board post text, which is public
# and written by anyone: real markdown.markdown() (used for the shipped
# whitepaper.md elsewhere in this file) passes raw HTML straight through
# unless separately sanitized, and this text is the one place on the site
# that is untrusted, attacker-controlled input rendered to other people's
# browsers. Escaping happens first, so every substitution below only ever
# wraps already-escaped text in tags it introduces itself; by the time any
# pattern runs, there is no way for a post's own content to contain a
# literal '<', so nothing typed into it can inject an element of its own.
_BOARD_CODE_RE = re.compile(r'`([^`]+?)`')
_BOARD_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_BOARD_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)')
_BOARD_URL_RE = re.compile(r'(https?://[^\s<]+)')


def render_board_text(raw):
    text = str(escape(raw))
    text = _BOARD_CODE_RE.sub(r'<code>\1</code>', text)
    text = _BOARD_BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _BOARD_ITALIC_RE.sub(r'<em>\1</em>', text)
    text = _BOARD_URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" rel="nofollow noopener noreferrer" target="_blank">{m.group(1)}</a>',
        text)
    return Markup(text.replace("\n", "<br>"))


def _board_posts(chain):
    """Every board post on chain, tip first.

    A board post is an ordinary tx whose memo starts with BOARD_MEMO_TAG,
    so finding them means reading every transaction's memo, the same full
    scan address_lookup already does for a balance's history. Small
    enough a chain for that to be fine; if it stops being one, this is
    where to add an index.
    """
    rows = []
    for blk in reversed(chain):
        for t in reversed(blk.get("transactions", [])):
            memo = t.get("memo") or ""
            if memo.startswith(BOARD_MEMO_TAG):
                rows.append((blk["height"], blk.get("timestamp"),
                             tx_mod.tx_hash(t), t))
    return rows


def _committed_tx_count(chain):
    """How many transactions the chain holds, for paging the dashboard.

    Counts block-by-block rather than walking transactions, so it is one
    len() per block and does not touch a transaction at all.
    """
    return sum(len(blk.get("transactions", ())) for blk in chain)


def _get_address_history(addr, node):
    """Every indexed transaction touching addr, newest first.

    Rows are grouped by block and each block is hashed through once, rather
    than re-hashing its whole transaction list per row. An address with
    several transactions in one block used to walk that block once per row,
    recomputing tx_hash (a full canonical serialization plus a digest) for
    every transaction on every pass.
    """
    chain = node.view.chain
    wanted = {}
    for height, tx_h in node.storage.get_tx_heights_for_addr(addr):
        if 0 <= height < len(chain):
            wanted.setdefault(height, []).append(tx_h)

    history = []
    for height, hashes in wanted.items():
        by_hash = {tx_mod.tx_hash(t): t for t in chain[height]["transactions"]}
        for tx_h in hashes:
            t = by_hash.get(tx_h)
            if t is not None:
                direction = "sent" if t.get("from") == addr else "received"
                history.append((height, tx_h, direction, t))
    history.sort(key=lambda row: row[0], reverse=True)
    return history


class _RewardSeries:
    """The mint reward at each height, replayed from genesis once and
    extended as the chain grows.

    Two callers needed this and each replayed the whole emission curve from
    genesis itself, one of them per page view: 45ms at height 100k on a
    public, unauthenticated page, growing with the chain forever. The
    series only ever gets longer at the end, so it is computed once and
    appended to.

    Read from Flask threads, which are many, and extended by whichever gets
    there first, so extension holds a lock. The list is only ever appended
    to under it, never rewritten, so a reader holding an index already
    within range does not need one.
    """

    def __init__(self):
        self._rewards = []       # reward minted by the block at each height
        self._total_minted = 0   # running total after the last entry
        self._lock = threading.Lock()

    def _extend_to(self, height):
        with self._lock:
            while len(self._rewards) <= height:
                reward = state_mod.compute_reward(self._total_minted)
                self._rewards.append(reward)
                if reward >= 1:
                    self._total_minted += reward

    def reward_at(self, height):
        if height < 0:
            return 0
        if height >= len(self._rewards):
            self._extend_to(height)
        return self._rewards[height]

    def prefix(self, count):
        """Rewards for heights [0, count), for a single walk of the chain."""
        if count <= 0:
            return []
        self.reward_at(count - 1)
        return self._rewards[:count]


_reward_series = _RewardSeries()


def _block_reward(chain, height):
    """Mint reward for the block at `height`. 0 for genesis (no builder,
    nothing minted)."""
    return _reward_series.reward_at(height)


def _get_mined_blocks_for_addr(addr, node):
    """Blocks built by addr, with the reward + fees paid to the builder.

    Mining rewards never go through the mempool/AddrIndex (they're credited
    directly in chainstate._apply_builder_reward), so they can't be pulled
    from the same tx index as ordinary transfers. The chain is kept fully
    in memory though, so we can just walk it once, reading each height's
    reward off the shared series rather than re-deriving the emission curve
    on every lookup.
    """
    chain   = node.view.chain
    rewards = _reward_series.prefix(len(chain))
    return [(height, blk["hash"], rewards[height] + block_mod.block_fees(blk))
            for height, blk in enumerate(chain)
            if blk.get("builder") == addr]


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
        if addr.lower() == "burn":
            addr = crypto_mod.burn_address()
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
    "next_block": float}. next_block never reads below params.MIN_RELAY_FEE_RATE:
    that floor is enforced by the mempool regardless of congestion (see
    mempool.Mempool.add), so an uncongested network never actually suggests 0,
    the same reason no real network's fees are literally 0 when idle.
    """
    pending = node.mempool.all_txs()
    if not pending:
        return {"pending": 0, "min": 0, "median": 0, "max": 0,
                "next_block": MIN_RELAY_FEE_RATE}

    rates = sorted(tx_mod.fee_rate(t) for t in pending)
    n = len(rates)
    median = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2

    v = node.view
    iterations = block_mod.get_vdf_iterations(v.chain)
    board_floor = tx_mod.board_fee_floor(v.state.total_board_posts)
    candidate = block_mod.assemble(v.tip, pending, v.tip.get("builder") or "",
                                   iterations, board_fee_floor=board_floor)
    included = candidate["transactions"]
    # Full block: the going rate is the lowest fee-per-byte that still made
    # it in, whatever that is, congestion sets its own price. Otherwise
    # everything pending fits, so the relay floor is what's actually
    # required to clear the next block, not 0.
    if len(included) < n:
        next_block = min(tx_mod.fee_rate(t) for t in included)
    else:
        next_block = MIN_RELAY_FEE_RATE

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
    # The self reference line plots own_pace, the same figure the odds are
    # computed from, so the line sits in the chart's own unit (block
    # intervals) rather than a VDF wall clock drawn among them.
    own_seconds = race["own_pace"]

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


def _default_send_outputs(node):
    """What the send form starts with: nothing.

    This used to prefill a row per node seen announcing itself active, so
    an operator could pay them. Those announcements are gone (they
    published a payable address network-wide, which is the link a trading
    identity must not have), and with them the only source of addresses
    this could honestly suggest. A blank field is the truthful default:
    the node does not know who you want to pay.
    """
    return ""


_INSUFFICIENT_RE = re.compile(r"insufficient balance: have (\d+), need (\d+)")


def _reword_insufficient_balance(msg):
    """"insufficient balance: have 400000000, need 400001000" (ticks, the
    only unit consensus speaks) read back as "have 4 LAPSE 0 ticks, need
    4 LAPSE 1,000 ticks" so a person doesn't have to do the division
    themselves to see they're short."""
    m = _INSUFFICIENT_RE.search(msg)
    if not m:
        return msg
    have, need = (int(g) for g in m.groups())
    return (f"insufficient balance: have {fmt_balance(have)}, "
            f"need {fmt_balance(need)}")


def _auto_fee(node, outputs, memo="", floor=0):
    """The fee this send will actually pay: whatever fee-per-byte clears
    the next block right now (see fee_estimate), and no more. There is no
    manual fee field for the same reason a real exchange doesn't ask you
    to guess one: overpaying buys nothing, underpaying just delays the
    transaction, and the node already knows the going rate live.

    floor is a protocol-enforced minimum on top of that (board posts;
    see tx.board_fee_floor), applied after the congestion-based fee so
    it can only raise it, never lower it below what tx.validate requires.
    """
    nonce = max(node.view.state.get_nonce(node.addr),
                node.mempool.pending_nonce(node.addr)) + 1
    draft = {"from": node.addr, "pubkey": node.pk_hex, "outputs": outputs,
             "nonce": nonce, "fee": 0}
    if memo:
        draft["memo"] = memo
    size = tx_mod.tx_size(draft)
    rate = fee_estimate(node)["next_block"]
    return max(floor, math.ceil(rate * size))


def _submit_and_alert(node, outputs, passphrase, ctx, memo="", floor=0):
    if not passphrase:
        ctx["alert_err"] = "Passphrase required."
        return
    try:
        fee = _auto_fee(node, outputs, memo=memo, floor=floor)
        t, _fee = node.build_and_sign_tx(outputs, fee=fee, passphrase=passphrase or None,
                                          memo=memo)
        ok, result = node.submit_tx_from_api(t)
        if ok:
            ctx["alert_ok_tx"]   = result
            ctx["alert_ok_verb"] = "Submitted."
        else:
            ctx["alert_err"] = f"Error: {_reword_insufficient_balance(result)}"
    except Exception as e:
        log.warning("[api] tx build/submit failed  err=%s", e)
        ctx["alert_err"] = f"Error: {e}"


def _xlm_view(xlm_keyfile_path):
    """(addr, spendable, locked) for the send page's XLM tab, or a blank
    tuple if this node has no trading wallet yet. Failures against
    Horizon read as 0 rather than raising: a page that cannot reach a
    public API should still render, just without a number it cannot get.
    """
    import xlm as xlm_mod
    addr = xlm_mod.load_public_key(xlm_keyfile_path)
    if not addr:
        return "", 0, 0
    try:
        spendable = xlm_mod.get_spendable_stroops(addr)
        locked = max(xlm_mod.get_balance_stroops(addr) - spendable, 0)
    except xlm_mod.XLMError:
        spendable = locked = 0
    return addr, spendable, locked


def _submit_xlm_and_alert(node, xlm_keyfile_path, to_addr, amount_stroops,
                          passphrase, ctx, merge=False):
    """The XLM half of /send: a plain payment, or an account-merge that
    closes the wallet and reclaims its reserve (see xlm.build_account_merge).

    Mirrors _submit_and_alert's shape (build, submit, report) but this
    wallet has no crash-recovery path the way a swap's does: it is a
    one-off manual action, so nothing here persists an envelope before
    sending it. A failed submit is simply not retried; the user sees the
    error and can try again.
    """
    import xlm as xlm_mod
    if not passphrase:
        ctx["alert_err"] = "Passphrase required."
        return
    if not xlm_keyfile_path or not xlm_mod.load_public_key(xlm_keyfile_path):
        ctx["alert_err"] = "Create a Stellar trading address on the Market page first."
        return
    if not xlm_mod.is_valid_address(to_addr):
        ctx["alert_err"] = "That is not a valid Stellar address."
        return
    try:
        kek = crypto_mod.derive_kek(node.keyfile, passphrase)
        seed = xlm_mod.decrypt_seed(xlm_keyfile_path, kek=kek)
    except ValueError:
        ctx["alert_err"] = "That is not this node's passphrase."
        return
    try:
        source = xlm_mod.Keypair.from_secret(seed).public_key
        if to_addr == source:
            ctx["alert_err"] = "That is this wallet's own address."
            return
        sequence = xlm_mod.get_sequence(source)
        if merge:
            xdr, tx_hash = xlm_mod.build_account_merge(seed, to_addr, sequence)
            verb = "Wallet closed; its balance was sent."
        else:
            spendable = xlm_mod.get_spendable_stroops(source)
            if amount_stroops > spendable:
                ctx["alert_err"] = (
                    "That is more than this wallet can spend; "
                    f"{xlm_mod.stroops_to_str(spendable)} XLM is free of its reserve.")
                return
            xdr, tx_hash = xlm_mod.build_payment(seed, to_addr, amount_stroops, "", sequence)
            verb = "Sent."
        ok, tx_hash, detail = xlm_mod.submit_envelope(xdr)
        if ok:
            ctx["alert_ok_tx"] = tx_hash
            ctx["alert_ok_verb"] = verb
        else:
            ctx["alert_err"] = f"Error: {detail}"
    except xlm_mod.XLMError as e:
        ctx["alert_err"] = f"Error: {e}"
    finally:
        del seed


def _board_ctx(node, page_arg, extra=None):
    """Board page context: pagination plus the post list. Shared between
    the GET route (read-only, both apps) and the private app's POST
    handler (which re-renders the same page with an alert after posting),
    so the two never drift into computing pagination differently."""
    all_rows = _board_posts(node.view.chain)
    total_pages = max(-(-len(all_rows) // BOARD_PER_PAGE), 1)
    page  = min(max(page_arg, 1), total_pages)
    start = (page - 1) * BOARD_PER_PAGE
    end   = start + BOARD_PER_PAGE
    ctx = dict(title="Board", rows=all_rows[start:end],
               post_count=len(all_rows), own_addr=node.addr,
               tag_len=len(BOARD_MEMO_TAG),
               page=page, total_pages=total_pages,
               page_window=_pagination_window(page, total_pages),
               has_prev=page > 1, has_next=end < len(all_rows))
    if extra:
        ctx.update(extra)
    return ctx


def _shared_read_only_routes(app, node, pool, limiter,
                              private_port, public_port, is_private,
                              update_checker=None, csrf_token=None):
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
            "board": "board", "network": "network", "odds": "odds",
            "whitepaper": "whitepaper", "send": "send", "rewards": "rewards",
            "settings": "settings",
            "market": "market", "market_take": "market",
            "market_book": "market_book", "trades": "trades",
        }.get(endpoint)
        return {"is_private": is_private,
                "private_port": private_port,
                "public_port": public_port,
                "update_checker": update_checker,
                "nav_active": nav_active}

    @app.route("/favicon.svg", endpoint=pfx+"favicon")
    def favicon():
        # Served straight from the repo's actual lapsecoin.svg (rather than a
        # copy baked into the HTML) so the browser tab icon always matches
        # whatever the file on disk currently looks like.
        return send_file(os.path.join(_base_dir(), "lapsecoin.svg"),
                         mimetype="image/svg+xml", max_age=3600)

    @app.route("/vendor/force-graph.min.js", endpoint=pfx+"vendor_force_graph")
    def vendor_force_graph():
        # Served from disk rather than a CDN, same reasoning as the
        # favicon above: a node's own UI shouldn't depend on a
        # third-party host being reachable. See vendor/README.md.
        return send_file(os.path.join(_base_dir(), "vendor", "force-graph.min.js"),
                         mimetype="application/javascript", max_age=86400)

    @app.route("/lapsecoin.png", endpoint=pfx+"icon_png")
    def icon_png():
        return _serve_icon(200, 200, "png")

    @app.route("/lapsecoin-<int:width>x<int:height>.<string:ext>",
               endpoint=pfx+"icon_png_sized")
    def icon_png_sized(width, height, ext):
        return _serve_icon(width, height, ext)

    def _serve_icon(width, height, ext):
        ext = ext.lower()
        mimetype = _ICON_MIMETYPES.get(ext)
        if mimetype is None:
            return jsonify(error="unsupported format"), 404
        if not (1 <= width <= _ICON_MAX_DIM and 1 <= height <= _ICON_MAX_DIM):
            return jsonify(error="size out of range"), 404
        # Cached on disk next to lapsecoin.svg after the first request; later
        # requests just serve that file instead of re-rasterizing every time.
        icon_path = os.path.join(_base_dir(), f"lapsecoin-{width}x{height}.{ext}")
        if not os.path.exists(icon_path):
            try:
                _generate_icon(icon_path, width, height, ext)
            except Exception as e:
                log.warning("[api] could not generate %s: %s", icon_path, e)
                return jsonify(error="icon generation unavailable"), 503
        return send_file(icon_path, mimetype=mimetype, max_age=3600)

    # ---- UI pages --------------------------------------------------------

    @app.route("/", endpoint=pfx+"dashboard")
    def dashboard():
        info  = node.get_info()
        chain = node.view.chain
        total = _committed_tx_count(chain)
        total_pages = max(-(-total // DASHBOARD_TXS_PER_PAGE), 1)
        page  = min(max(request.args.get("tx_page", 1, type=int) or 1, 1), total_pages)
        offset = (page - 1) * DASHBOARD_TXS_PER_PAGE
        return render_template("dashboard.html", title="Dashboard",
            info=info, supply_cap=SUPPLY_CAP,
            recent_txs=_recent_committed_txs(chain, DASHBOARD_TXS_PER_PAGE, offset),
            tx_page=page, tx_total_pages=total_pages,
            tx_page_window=_pagination_window(page, total_pages),
            tx_has_prev=page > 1, tx_has_next=page < total_pages)

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
        # Only the bundle root and this source tree. The working directory
        # used to be searched too, and markdown passes raw HTML straight
        # through to a template that renders it with |safe, so whatever
        # happened to be at ./docs/whitepaper.md when the node was started
        # decided the contents of a page served on the public port. The
        # shipped file is the only one that should ever be able to do that.
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        for candidate in [
            os.path.join(base, "docs", "whitepaper.md"),
            # One level up, for a source checkout where this file is in src/.
            os.path.join(os.path.dirname(base), "docs", "whitepaper.md"),
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

    def _peer_fork_info(chain, height, tip_hash):
        """(is_fork, depth) for a peer claiming (height, tip_hash) as its
        tip, against our own chain. is_fork is True only when we can
        actually check: we have a block of our own at that height and its
        hash disagrees with the peer's claim. depth is how many blocks
        behind our own tip that claimed height is (our_height - height),
        the same number the height column already implies, not a real
        common-ancestor search: nothing here walks the chain looking for
        where the two actually diverged, that's the expensive fork search
        this project deliberately keeps out of a peer-info display (see
        info_probe.py's module docstring). A peer claiming a height at or
        past our own tip is never flagged: without a block there ourselves
        yet, there's nothing to compare its claim against.
        """
        if not tip_hash or height is None or height < 0:
            return False, None
        our_height = len(chain) - 1
        if height > our_height:
            return False, None
        is_fork = chain[height]["hash"] != tip_hash
        return is_fork, (our_height - height) if is_fork else None

    def _peer_dicts(rows):
        chain = node.view.chain
        result = []
        for addr, last_seen, active, height, version, http_reachable, introduced_by, tip_hash in rows:
            is_fork, fork_depth = _peer_fork_info(chain, height, tip_hash)
            result.append({
                "address": addr, "last_seen": int(last_seen), "active": active,
                "height": height, "version": version,
                "http_reachable": http_reachable, "introduced_by": introduced_by,
                # True only when this node has direct evidence (its own
                # block at the peer's claimed height) that the peer is on
                # a different chain; fork_depth is that height's distance
                # behind our own tip, see _peer_fork_info.
                "is_fork": is_fork, "fork_depth": fork_depth,
            })
        return result

    def _self_info():
        return {"height": node.view.chain[-1].get("height", 0),
                "version": LOCAL_VERSION, "addr": _self_external_addr()}

    def _capped_claims(raw_claims, held_addrs):
        """Trim the addresses no held peer claims to at most
        CLAIMED_GRAPH_LIMIT, keeping the ones the most introducers
        currently agree on. Corroboration is the only signal available,
        no height/version/last-seen travels with a claim, only the
        address, but it's a real one: an address three separate held
        peers are all currently pointing at is more informative than one
        only a single peer mentions. Held addresses are never trimmed,
        they're already bounded by MAX_PEERS."""
        corroboration = collections.Counter(
            addr for addrs in raw_claims.values() for addr in addrs
            if addr not in held_addrs)
        keep = {addr for addr, _ in corroboration.most_common(CLAIMED_GRAPH_LIMIT)}
        return {introducer: [a for a in addrs if a in held_addrs or a in keep]
                for introducer, addrs in raw_claims.items()}

    @app.route("/network", endpoint=pfx+"network")
    def network():
        # The graph is the whole page now; peer_count is all it needs
        # from here, the rest (self info, per-peer detail) comes live
        # from /api/peers.
        return render_template("network.html", title="Network",
                               peer_count=pool.count())

    @app.route("/peers", endpoint=pfx+"peers_redirect")
    def peers_redirect():
        # Old bookmarks/links: the page moved to /network, keep it working.
        return redirect(f"/network?{request.query_string.decode()}"
                         if request.query_string else "/network", code=301)

    @app.route("/board", endpoint=pfx+"board")
    def board():
        floor = tx_mod.board_fee_floor(node.view.state.total_board_posts)
        extra = dict(csrf_token=csrf_token, compose_err="", compose_ok="",
                     message_value="", board_fee_floor=floor)
        if csrf_token:   # private app only: composing needs a fee suggestion
            extra["fees"] = fee_estimate(node)
        page = request.args.get("page", 1, type=int) or 1
        return render_template("board.html", **_board_ctx(node, page, extra))

    @app.route("/api/board", endpoint=pfx+"api_board")
    def api_board():
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        all_rows = _board_posts(node.view.chain)
        total_pages = max(-(-len(all_rows) // BOARD_PER_PAGE), 1)
        page  = min(page, total_pages)
        start = (page - 1) * BOARD_PER_PAGE
        end   = start + BOARD_PER_PAGE
        return jsonify({
            "post_count": len(all_rows),
            "own_addr": node.addr,
            "posts": [
                {"height": h, "timestamp": ts, "hash": hsh,
                 "from": t.get("from", ""),
                 "memo": (t.get("memo") or "")[len(BOARD_MEMO_TAG):]}
                for h, ts, hsh, t in all_rows[start:end]
            ],
        })

    # Race-odds data for the current tip, computed once per tip and held
    # here rather than in a module global: one cache per app, keyed by
    # nothing that can be absent. race_odds simulates ten thousand draws
    # (about 15ms) and nothing in its answer moves until the chain does,
    # while two routes ask for it, /odds and the /api/odds the dashboard
    # polls on a timer. They now also agree exactly instead of each
    # simulating its way to a slightly different number.
    odds_cache = {}
    odds_lock  = threading.Lock()

    def _race_for():
        tip = node.view.chain[-1]
        # The draw window is this node's own setting, read per request
        # rather than frozen at startup, so an operator who changes it
        # sees the page that explains it change too.
        window = node.settings.get(settings_mod.DRAW_WINDOW_SECONDS)
        key = (tip.get("hash"), tip.get("height"), len(node.view.chain),
               node.addr, window, node.own_vdf_median())
        with odds_lock:
            if odds_cache.get("key") == key:
                return odds_cache["race"]
        race = block_mod.race_odds(node.view.chain, node.own_vdf_median(),
                                   node.addr, window)
        with odds_lock:
            odds_cache["key"], odds_cache["race"] = key, race
        return race

    @app.route("/odds", endpoint=pfx+"odds")
    def odds():
        race = _race_for()
        chart = _race_chart(race) if race else None
        return render_template("odds.html", title="Race Odds", race=race,
                               chart=chart, reorgs=node.reorg_stats(),
                               own_is_estimate=node.own_vdf_is_estimate())

    # ---- JSON API (read-only) --------------------------------------------

    @app.route("/api/odds", endpoint=pfx+"api_odds")
    def api_odds():
        race = _race_for()
        if not race:
            return jsonify(None)
        return jsonify({
            "median": race["median"],
            "own_seconds": race["own_seconds"], "odds_pct": race["odds_pct"],
            "own_is_estimate": node.own_vdf_is_estimate(),
            # Who the odds are measured against, and what actually
            # happened, which are different claims: see block.race_odds.
            "field_blocks": race["field_blocks"],
            "own_blocks": race["own_blocks"],
            "win_share_pct": race["win_share_pct"],
            "own_pace": race["own_pace"],
            "own_pace_measured": race["own_pace_measured"],
            "entrants": race["entrants"],
            "in_draw_pct": race["in_draw_pct"],
            "draw_window": race["draw_window"],
            "field_builders": race["field_builders"],
            "window_len": len(race["window"]), "chart": _race_chart(race),
            "reorgs": node.reorg_stats(),
        })

    @app.route("/api/peers", endpoint=pfx+"api_peers")
    def api_peers():
        all_rows = sorted(pool.snapshot(), key=lambda r: r[1], reverse=True)
        return jsonify({
            "self": _self_info(),
            "peer_count": len(all_rows),
            # The whole known set: MAX_PEERS (params.py) bounds it at 125,
            # small enough to send in one response and lay out smoothly.
            "graph_peers": _peer_dicts(all_rows),
            # What this node's held peers have themselves claimed about
            # their own peers, most recent list per introducer, including
            # addresses this node never admitted (couldn't reach, or
            # hasn't tried). Lets the graph draw a relationship it has
            # real evidence for even where it isn't itself one end of it.
            # Capped (_capped_claims) so a peer that happens to know a lot
            # of other peers can't blow up the response or the graph.
            "claims": _capped_claims(pool.claims(), {r[0] for r in all_rows}),
            # Candidates discovery is actively trying to reach right now
            # (ping, relayed punch, direct punch), so the graph can show a
            # connection being attempted instead of peers only ever
            # appearing at the moment they're already admitted.
            "attempting": pool.attempting(),
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
            for height, h, t, amount in _recent_committed_txs(
                chain, DASHBOARD_TXS_PER_PAGE)
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
        """Newest first. `limit` and `offset` select a slice.

        The page next door has always shown three at a time while this
        returned every entry an address ever had, which is a strange pair:
        the caller that needs the least got the most, and the response
        grew without bound as a chain nobody prunes gets longer.

        Still a bare array and still unbounded by default, because
        integrators are already polling this and a default page size would
        silently truncate them. `limit` is the way to ask for less, the
        total is in X-Total-Count so a caller can page without guessing,
        and a bounded default belongs in a release that says so.
        """
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        rows = _get_address_history(addr, node)
        total = len(rows)

        limit  = request.args.get("limit", type=int)
        offset = max(request.args.get("offset", 0, type=int) or 0, 0)
        if limit is not None and limit < 0:
            return jsonify({"error": "limit must not be negative"}), 400
        rows = rows[offset:] if limit is None else rows[offset:offset + limit]

        resp = jsonify([
            {"height": h, "tx_hash": th, "direction": d, "tx": t}
            for h, th, d, t in rows
        ])
        resp.headers["X-Total-Count"] = str(total)
        return resp

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
    forget when it looks like a plain attribute (Node.settings.get).

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


# Formats servable by /lapsecoin-WxH.ext, all reachable from cairosvg's PNG
# output via Pillow (already a hard dependency) so no new packages are needed.
_ICON_MIMETYPES = {
    "png": "image/png", "webp": "image/webp", "bmp": "image/bmp",
    "ico": "image/x-icon", "jpg": "image/jpeg", "jpeg": "image/jpeg",
}
_ICON_MAX_DIM = 2048  # generous upper bound; keeps the route from being an easy way to burn CPU/disk


def _generate_icon(icon_path, width, height, ext):
    """Rasterize lapsecoin.svg to a transparent WxH image at icon_path.

    Runs at most once per size/format per fresh checkout/container: callers
    only invoke this when the file isn't already on disk, and the result is
    gitignored so it's never committed.
    """
    import cairosvg
    from io import BytesIO
    from PIL import Image

    svg_path = os.path.join(_base_dir(), "lapsecoin.svg")
    png_bytes = cairosvg.svg2png(url=svg_path, output_width=width, output_height=height)
    if ext == "png":
        with open(icon_path, "wb") as f:
            f.write(png_bytes)
    else:
        img = Image.open(BytesIO(png_bytes)).convert("RGBA")
        if ext in ("jpg", "jpeg", "bmp"):
            # These formats have no alpha channel; flatten onto white rather
            # than leaving transparency to be interpreted arbitrarily.
            flattened = Image.new("RGB", img.size, (255, 255, 255))
            flattened.paste(img, mask=img.getchannel("A"))
            img = flattened
        img.save(icon_path, format="ICO" if ext == "ico" else ext.upper())
    log.info("[api] generated missing %s from svg", os.path.basename(icon_path))


# Public app factory  (port 8333)
# ---------------------------------------------------------------------------

def create_app(node, pool, private_port=8335, public_port=8333,
               update_checker=None):
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_lapse=fmt_lapse,
                                 fmt_duration=fmt_duration,
                                 fmt_lapse_dp=fmt_lapse_dp,
                                 TICKS_PER_LAPSE=TICKS_PER_LAPSE,
                                 BURN_ADDRESS=crypto_mod.burn_address(),
                                 BOARD_POST_AMOUNT=BOARD_POST_AMOUNT,
                                 render_board_text=render_board_text,
                                 MAX_MEMO_BYTES=tx_mod.MAX_MEMO_BYTES)
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
                             update_checker=update_checker)

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
                       update_checker=None):
    """Full-featured app for local use. Never expose via Funnel or public port."""
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_lapse=fmt_lapse,
                                 fmt_duration=fmt_duration,
                                 fmt_lapse_dp=fmt_lapse_dp,
                                 TICKS_PER_LAPSE=TICKS_PER_LAPSE,
                                 BURN_ADDRESS=crypto_mod.burn_address(),
                                 BOARD_POST_AMOUNT=BOARD_POST_AMOUNT,
                                 render_board_text=render_board_text,
                                 MAX_MEMO_BYTES=tx_mod.MAX_MEMO_BYTES)
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
                             update_checker=update_checker, csrf_token=csrf_token)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        balance_lapse = node.view.state.get_balance(node.addr) / TICKS_PER_LAPSE
        ctx = dict(title="Settings", csrf_token=csrf_token,
                   alert_ok="", alert_err="",
                   balance_lapse=balance_lapse)

        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
                ctx["alert_err"] = "Session expired; reload the page and try again."
            else:
                errors = []
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
        ctx["own_addr"] = node.addr
        return render_template("settings.html", **ctx)

    @app.route("/send", methods=["GET", "POST"])
    def send():
        import market_routes
        v = node.view
        xlm_keyfile_path = market_routes.xlm_keyfile_path(node)
        xlm_addr, xlm_spendable, xlm_locked = _xlm_view(xlm_keyfile_path)
        ctx = dict(title="Send", from_addr=node.addr,
                   balance=v.state.get_balance(node.addr),
                   fees=fee_estimate(node), csrf_token=csrf_token,
                   outputs_value=_default_send_outputs(node),
                   memo_value="", memo_max_bytes=tx_mod.MAX_MEMO_BYTES,
                   asset="lapse",
                   xlm_addr=xlm_addr, xlm_spendable=xlm_spendable,
                   xlm_locked=xlm_locked, xlm_to_value="",
                   xlm_amount_value="", xlm_merge_value=False,
                   alert_ok_tx="", alert_ok_verb="", alert_err="", alert_err_lines=[])
        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
                ctx["alert_err"] = "Session expired; reload the page and try again."
                return render_template("send.html", **ctx)

            asset = request.form.get("asset", "lapse")
            ctx["asset"] = asset
            passphrase = request.form.get("passphrase", "").strip()

            if asset == "xlm":
                to_addr = request.form.get("xlm_to", "").strip()
                merge = request.form.get("xlm_merge") == "1"
                amount_raw = request.form.get("xlm_amount", "").strip()
                ctx["xlm_to_value"] = to_addr
                ctx["xlm_amount_value"] = amount_raw
                ctx["xlm_merge_value"] = merge
                if not to_addr:
                    ctx["alert_err"] = "Enter a destination address."
                else:
                    amount_stroops = 0
                    if not merge:
                        try:
                            amount_stroops = market_routes.parse_xlm(amount_raw)
                        except ValueError as e:
                            ctx["alert_err"] = str(e)
                    if not ctx["alert_err"]:
                        _submit_xlm_and_alert(node, xlm_keyfile_path, to_addr,
                                              amount_stroops, passphrase, ctx,
                                              merge=merge)
                        if ctx["alert_ok_tx"]:
                            ctx["xlm_to_value"] = ""
                            ctx["xlm_amount_value"] = ""
                            ctx["xlm_merge_value"] = False
                            ctx["xlm_addr"], ctx["xlm_spendable"], ctx["xlm_locked"] = \
                                _xlm_view(xlm_keyfile_path)
            else:
                outputs_raw = request.form.get("outputs", "").strip()
                memo        = request.form.get("memo", "").strip()
                csv_file    = request.files.get("csv_file")
                if csv_file and csv_file.filename:
                    outputs_raw = csv_file.read().decode()
                ctx["outputs_value"] = outputs_raw
                ctx["memo_value"] = memo
                outputs, errors = _parse_csv_outputs(outputs_raw)
                if len(memo.encode("utf-8")) > tx_mod.MAX_MEMO_BYTES:
                    errors.append(f"Memo exceeds {tx_mod.MAX_MEMO_BYTES} bytes.")
                if errors:
                    ctx["alert_err_lines"] = errors
                elif not outputs:
                    ctx["alert_err"] = "No valid outputs."
                else:
                    _submit_and_alert(node, outputs, passphrase, ctx, memo=memo)
                    if ctx["alert_ok_tx"]:
                        ctx["alert_ok_verb"] = "Sent."
                        ctx["outputs_value"] = ""
                        ctx["memo_value"] = ""
        return render_template("send.html", **ctx)

    @app.route("/api/fees", endpoint="api_fees")
    def api_fees():
        """The same fee-market picture the send page renders at load, for
        it to poll and stay live: the suggested rate is only true for as
        long as the mempool doesn't change, and it changes constantly.
        """
        return jsonify(fee_estimate(node))

    @app.route("/api/send/fee", endpoint="api_send_fee")
    def api_send_fee():
        """Whether the outputs/memo currently in the send form would go
        through right now, and at what fee, so the Sign & Send button can
        be disabled with the real reason before anything is signed rather
        than after. Reuses the same parsing and fee logic the actual POST
        handler uses, so this can't say "fine" to something that then
        fails, or the reverse.
        """
        outputs, errors = _parse_csv_outputs(request.args.get("outputs", ""))
        memo = request.args.get("memo", "")
        if len(memo.encode("utf-8")) > tx_mod.MAX_MEMO_BYTES:
            errors.append(f"Memo exceeds {tx_mod.MAX_MEMO_BYTES} bytes.")
        if errors:
            return jsonify({"ok": False, "reason": errors[0]})
        if not outputs:
            return jsonify({"ok": False, "reason": "No valid outputs."})
        fee = _auto_fee(node, outputs, memo=memo)
        total_out = sum(o["amount"] for o in outputs)
        required = total_out + fee
        balance = node.view.state.get_balance(node.addr)
        if required > balance:
            return jsonify({"ok": False, "fee": fee,
                            "reason": f"Insufficient balance: have {fmt_balance(balance)}, "
                                      f"need {fmt_balance(required)}."})
        return jsonify({"ok": True, "fee": fee, "reason": ""})

    @app.route("/api/board/fee", endpoint="api_board_fee")
    def api_board_fee():
        """The actual fee a board post of this length would pay right now,
        for the compose box to show live as the message grows instead of a
        static floor that stops being true the moment typing starts:
        tx_size (what the fee is computed against) scales with the memo, so
        a longer message really does cost more, and the mempool's own rate
        can move between keystrokes too.
        """
        msg_bytes = min(max(0, request.args.get("bytes", 0, type=int) or 0), tx_mod.MAX_MEMO_BYTES)
        # An ASCII placeholder of the same byte length: close enough for an
        # estimate, and the real message never leaves the browser until
        # actually posted.
        memo = BOARD_MEMO_TAG + ("x" * msg_bytes)
        floor = tx_mod.board_fee_floor(node.view.state.total_board_posts)
        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        fee = _auto_fee(node, outputs, memo=memo, floor=floor)
        required = BOARD_POST_AMOUNT + fee
        balance = node.view.state.get_balance(node.addr)
        if required > balance:
            return jsonify({"fee": fee, "floor": floor, "ok": False,
                            "reason": f"Insufficient balance: have {fmt_balance(balance)}, "
                                      f"need {fmt_balance(required)}."})
        return jsonify({"fee": fee, "floor": floor, "ok": True, "reason": ""})

    @app.route("/board", methods=["POST"], endpoint="board_post")
    def board_post():
        page = request.args.get("page", 1, type=int) or 1
        # The floor is read fresh on every submit: it only moves when a
        # block confirms (see tx.board_fee_floor), so this is always the
        # same value tx.validate() will check the resulting tx against,
        # modulo a block landing in between, which is exactly the rare
        # "resubmit at the new floor" case that staircase is designed for.
        floor = tx_mod.board_fee_floor(node.view.state.total_board_posts)
        message    = request.form.get("message", "").strip()
        passphrase = request.form.get("passphrase", "").strip()
        extra = dict(csrf_token=csrf_token, fees=fee_estimate(node),
                     compose_err="", compose_ok="", board_fee_floor=floor,
                     message_value=message)

        def fail(msg):
            extra["compose_err"] = msg
            return render_template("board.html", **_board_ctx(node, page, extra))

        if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
            return fail("Session expired; reload the page and try again.")
        if not message:
            return fail("Write something to post.")
        memo = BOARD_MEMO_TAG + message
        over = len(memo.encode("utf-8")) - tx_mod.MAX_MEMO_BYTES
        if over > 0:
            return fail(f"{over} byte{'s' if over != 1 else ''} too long.")

        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        alert_ctx = {}
        _submit_and_alert(node, outputs, passphrase, alert_ctx, memo=memo, floor=floor)
        if alert_ctx.get("alert_err"):
            return fail(alert_ctx["alert_err"])

        extra["message_value"] = ""
        extra["compose_ok"] = "Posted, waiting to be mined."
        return render_template("board.html", **_board_ctx(node, page, extra))

    # Market and Trades live in their own module: this file is already long
    # and a node with swaps off never reaches any of it.
    import market_routes
    market_routes.register(app, node, csrf_token)

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
