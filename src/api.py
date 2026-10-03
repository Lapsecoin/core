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

    GET  /api/fees
         {"pending", "min", "median", "max", "next_block"}: the fee market,
         in ticks per byte. next_block is what clears the next block.

    GET  /api/state[?addr=<addr>&nick=<n>&profile=1&fees=1]
         What a wallet needs about one address in a
         single answer. Always "board_floor". With addr: "balance" and
         "nonce" (highest confirmed or pending; send nonce + 1). With nick:
         "nick_owner". With profile: "profile" ({"icon","nick"} or null).
         With fees: "fees", as /api/fees.

    GET  /api/address/<addr>/page[?page=<n>]
         One page of an address's history with its totals, as the Balance
         page shows it: {"balance", "tx_count", "page", "total_pages",
         "rows": [[height, hash, direction, amount, kind], ...]}.

    GET  /api/board/page[?chunks=<n>]
         The board, resolved (profiles, votes, replies), as the Board page
         shows it, in the first n chunks of threads. Carries an ETag; send
         it back as If-None-Match and an unchanged board is a 304. Gzipped
         when the client accepts it.

    GET  /api/peers/http
         {"nodes": ["ip:port", ...]}: this node and the peers it has found
         answering HTTP on its own chain. Short on purpose, for light
         clients (see light.py) that cannot afford /api/peers.

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
import os
import re
import secrets
import socket
import sys
import threading

import markdown
from flask import jsonify, redirect, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import block as block_mod
import crypto as crypto_mod
import hardware_info
import settings as settings_mod
import storage as storage_mod
import tx as tx_mod
from params import TICKS_PER_LAPSE, SUPPLY_CAP
from version import LOCAL_VERSION
from local_reader import (_get_address_history, _block_reward,  # noqa: F401
                          fee_estimate, local_reader_for)
from ui_common import (make_flask_app, register_static_routes, _tx_amount,
                       _pagination_window, _base_dir,  # noqa: F401
                       fmt_duration, render_board_text)
# The board names the tests and other modules still reach through api.
from board_view import (BOARD_MEMO_TAG, parse_board_body, build_board_body,  # noqa: F401
                        REPLY_REF_LEN, ICON_PALETTE, ICON_GHOST, _icon_emoji,
                        VOTE_UP_TAG, VOTE_DOWN_TAG, _nickname_owned_by,
                        _board_posts, _board_profiles_and_votes,
                        BOARD_THREADS_PER_CHUNK, _flatten_threads)
from wallet_ui import (register_address_page, register_board_pages,
                       register_data_api, register_wallet_routes)

log = logging.getLogger("ec.api")

# Nodes keep full history, so both the block list and an address's
# transaction history are paginated rather than truncated to "recent N".
BLOCKS_PER_PAGE  = 8
DASHBOARD_TXS_PER_PAGE = 5
BOARD_PER_PAGE = 20

# Held peers are already bounded by MAX_PEERS (125), but what they claim
# isn't: up to PEERS_PER_MESSAGE_LIMIT (50) addresses from each of up to
# MAX_PEERS introducers is a theoretical ~6,250 addresses this node never
# itself reached. Uncapped, that's both a large response on every 8s poll
# and an unreadable graph. See _capped_claims.
CLAIMED_GRAPH_LIMIT = 60











# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------









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


def _mempool_rows(mempool):
    """Return pending transactions in the order block assembly prefers."""
    txs = mempool.all_txs()
    txs.sort(key=lambda t: (-tx_mod.fee_rate(t), tx_mod.tx_hash(t)))
    return [{"hash": tx_mod.tx_hash(t), "tx": t} for t in txs]










def _committed_tx_count(chain):
    """How many transactions the chain holds, for paging the dashboard.

    Counts block-by-block rather than walking transactions, so it is one
    len() per block and does not touch a transaction at all.
    """
    return sum(len(blk.get("transactions", ())) for blk in chain)
















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


def _race_chart(race, nicknames_by_addr=None):
    """Precompute SVG pixel geometry for the race-odds chart.

    nicknames_by_addr: addr -> nickname (built from _board_profiles_and_votes'
    own profiles dict), or None for a caller that doesn't
    have one handy -- every "nick" this function attaches then just
    reads as None, which the template already treats as "show the
    address" (unchanged behaviour), so a caller that skips wiring this
    up isn't required to.

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
    nicknames_by_addr = nicknames_by_addr or {}
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
               "builder": builder, "nick": nicknames_by_addr.get(builder),
               "color": color_by_builder.get(builder, _OTHER_COLOR)}
              for idx, (h, seconds, builder) in enumerate(rows)]

    legend = [{"label": b, "nick": nicknames_by_addr.get(b),
               "color": color_by_builder[b], "count": builder_counts[b]}
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


class _NodeSigner:
    """What the wallet routes sign through on a full node: the node itself
    builds and signs, from its own chain state (see Node.build_and_sign_tx).
    The light client has no node, so it signs with a bare Wallet instead
    (wallet_ui.WalletSigner)."""

    def __init__(self, node):
        self.node = node

    @property
    def addr(self):
        return self.node.addr

    @property
    def pk_hex(self):
        return self.node.pk_hex

    def sign(self, outputs, fee, memo, passphrase, nonce):
        t, _fee = self.node.build_and_sign_tx(outputs, fee=fee,
                                              passphrase=passphrase or None, memo=memo)
        return t


class _XlmSend:
    """The XLM half of the send page, behind the two calls wallet_ui makes:
    view() for what the XLM tab shows and send() for its form. The light
    client has no trading wallet and passes none."""

    def __init__(self, node):
        self.node = node

    def _path(self):
        import market_routes
        return market_routes.xlm_keyfile_path(self.node)

    def view(self):
        addr, spendable, locked = _xlm_view(self._path())
        return dict(xlm_addr=addr, xlm_spendable=spendable, xlm_locked=locked,
                    xlm_to_value="", xlm_amount_value="")

    def send(self, form, passphrase, ctx):
        import market_routes
        to_addr = form.get("xlm_to", "").strip()
        amount_raw = form.get("xlm_amount", "").strip()
        ctx["xlm_to_value"] = to_addr
        ctx["xlm_amount_value"] = amount_raw
        if not to_addr:
            ctx["alert_err"] = "Enter a destination address."
            return
        try:
            amount_stroops = market_routes.parse_xlm(amount_raw)
        except ValueError as e:
            ctx["alert_err"] = str(e)
            return
        path = self._path()
        _submit_xlm_and_alert(self.node, path, to_addr, amount_stroops,
                              passphrase, ctx)
        if ctx["alert_ok_tx"]:
            ctx["xlm_to_value"] = ""
            ctx["xlm_amount_value"] = ""
            ctx["xlm_addr"], ctx["xlm_spendable"], ctx["xlm_locked"] = _xlm_view(path)


def _submit_xlm_and_alert(node, xlm_keyfile_path, to_addr, amount_stroops,
                          passphrase, ctx):
    """The XLM half of /send. Mirrors _submit_and_alert's shape (build,
    submit, report) but this wallet has no crash-recovery path the way a
    swap's does: it is a one-off manual action, so nothing here persists
    an envelope before sending it. A failed submit is simply not retried;
    the user sees the error and can try again.
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


















def _shared_read_only_routes(app, node, pool, limiter,
                              private_port, public_port, is_private,
                              update_checker=None, csrf_token=None,
                              updater=None):
    """Register all read-only UI and API routes on app."""
    # Use a prefix so public and private apps don't collide on endpoint names
    pfx = "priv_" if is_private else "pub_"

    def _own_addr_or_hidden():
        # The private app always sees the real address -- there's nothing
        # to gain by hiding an operator's own address from themselves.
        # The public app hides it too, unless the operator explicitly
        # turned that off (see settings.HIDE_ADDRESS_PUBLICLY).
        if is_private or not node.settings.get(settings_mod.HIDE_ADDRESS_PUBLICLY):
            return node.addr
        return None

    reader = local_reader_for(node)
    register_static_routes(app, pfx)
    register_board_pages(app, reader, pfx, _own_addr_or_hidden, csrf_token)
    register_data_api(app, reader, pfx)

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
            "board": "board", "mempool": "mempool", "network": "network", "odds": "odds",
            "whitepaper": "whitepaper", "send": "send", "rewards": "rewards",
            "settings": "settings",
            "market": "market", "market_take": "market",
            "market_book": "market_book", "trades": "trades",
        }.get(endpoint)
        return {"is_private": is_private,
                "private_port": private_port,
                "public_port": public_port,
                "update_checker": update_checker,
                "updater": updater,
                # None on the public app (never passed a csrf_token), which
                # is fine: the one thing this token guards, the self-update
                # trigger button, is only ever rendered when is_private.
                "csrf_token": csrf_token,
                "nav_active": nav_active,
                "local_version": LOCAL_VERSION}

    @app.route("/api/update/status", endpoint=pfx+"api_update_status")
    def api_update_status():
        # Available on both apps (same reasoning as /api/peers): stage and
        # install_type aren't sensitive, and the browser polling this may
        # well be looking at the public app. detail/error are different --
        # they can hold a raw pip output line or an "X isn't writable"
        # message naming this machine's own install path, so those are
        # only ever sent to the private (127.0.0.1-only) app; a public,
        # possibly internet-facing one only gets the stage name itself.
        # Only /api/update/start (private-only, below) can ever change
        # anything either way.
        if updater is None:
            snap = {"stage": "idle", "detail": "", "error": None,
                   "install_type": None, "can_self_update": False}
        else:
            snap = updater.status()
        if not is_private:
            snap = {"stage": snap["stage"], "install_type": snap["install_type"],
                    "can_self_update": snap["can_self_update"]}
        return jsonify(**snap)


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
        info = dict(node.get_info())
        info["address"] = _own_addr_or_hidden()
        # Only ever looked up when the address itself is already showing:
        # a nickname here would be exactly the identity-revealing side
        # channel HIDE_ADDRESS_PUBLICLY exists to close if it appeared
        # while the address itself stayed hidden.
        info["nick"] = _builder_nicknames_for().get(info["address"]) if info["address"] else None
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

    @app.route("/mempool", endpoint=pfx+"mempool")
    def mempool_page():
        return render_template("mempool.html", title="Mempool",
                               transactions=_mempool_rows(node.mempool))

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

    def _holder_histogram():
        all_balances = node.view.state.get_all_balances()
        histogram = compute_holder_histogram(all_balances)
        return (len(all_balances), histogram,
                max((c for _, c in histogram), default=0))

    register_address_page(app, reader, pfx, histogram=_holder_histogram)

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
            "own_addr": _own_addr_or_hidden(),
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

    # Same tip-keyed cache pattern as odds_cache, kept separate since this
    # depends only on the chain's board posts and state.nicknames, not on
    # the draw window or own_vdf_median odds_cache's own key includes.
    nickname_cache = {}

    def _builder_nicknames_for():
        tip = node.view.chain[-1]
        key = (tip.get("hash"), tip.get("height"))
        with odds_lock:
            if nickname_cache.get("key") == key:
                return nickname_cache["nicknames"]
        profiles, _votes, _pending = _board_profiles_and_votes(node.view.chain, node.view.state.nicknames)
        nicknames = {addr: prof["nick"] for addr, prof in profiles.items()}
        with odds_lock:
            nickname_cache["key"], nickname_cache["nicknames"] = key, nicknames
        return nicknames

    @app.route("/odds", endpoint=pfx+"odds")
    def odds():
        race = _race_for()
        chart = _race_chart(race, _builder_nicknames_for()) if race else None
        hardware = (hardware_info.describe()
                    if node.settings.get(settings_mod.SHOW_HARDWARE_DETAILS)
                    else None)
        return render_template("odds.html", title="Race Odds", race=race,
                               chart=chart, reorgs=node.reorg_stats(),
                               own_is_estimate=node.own_vdf_is_estimate(),
                               hardware=hardware,
                               mining_enabled=node.settings.get(settings_mod.MINING_ENABLED),
                               # Transient, not a setting: this cycle measured
                               # 0% and the node is watching for the field
                               # before it builds anyway (see
                               # Node._wait_for_field_or_own_pace). Only that
                               # one status_line value starts this way.
                               waiting_zero_odds=getattr(node, "status_line", "").startswith("waiting"))

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
            "window_len": len(race["window"]), "chart": _race_chart(race, _builder_nicknames_for()),
            "reorgs": node.reorg_stats(),
            "mining_enabled": node.settings.get(settings_mod.MINING_ENABLED),
            "waiting_zero_odds": getattr(node, "status_line", "").startswith("waiting"),
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

    @app.route("/api/peers/http", endpoint=pfx+"api_peers_http")
    def api_peers_http():
        """Only the nodes a light client can use: this node itself and the
        peers it has found answering HTTP and believes are on its own
        chain, best-connected first. /api/peers describes the whole peer
        graph for the network page; a light client on a small data
        allowance needs a short list of addresses and nothing else."""
        rows = sorted(pool.snapshot(), key=lambda r: r[3] or 0, reverse=True)
        nodes = [p["address"] for p in _peer_dicts(rows)
                 if p["http_reachable"] and p["active"] and not p["is_fork"]]
        me = _self_external_addr()
        if me and me not in nodes:
            nodes.insert(0, me)
        return jsonify({"nodes": nodes})

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
        info["iid"] = pool.instance_id
        info["address"] = _own_addr_or_hidden()
        info["nick"] = _builder_nicknames_for().get(info["address"]) if info["address"] else None
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

    @app.route("/api/mempool/fragment", endpoint=pfx+"api_mempool_fragment")
    def api_mempool_fragment():
        rows = _mempool_rows(node.mempool)
        html = render_template("mempool_rows.html", transactions=rows)
        return jsonify({"size": len(rows), "html": html})

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
               update_checker=None, updater=None):
    app = make_flask_app(__name__)
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
                             update_checker=update_checker, updater=updater)

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
                       update_checker=None, updater=None):
    """Full-featured app for local use. Never expose via Funnel or public port."""
    app = make_flask_app(__name__)
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
                             update_checker=update_checker, csrf_token=csrf_token,
                             updater=updater)

    @app.route("/api/update/start", methods=["POST"])
    def api_update_start():
        # Same CSRF token /settings and /send already use: fixed for this
        # process's lifetime, checked with a constant-time compare, never
        # put in a URL. Private-only (127.0.0.1), same as those two, since
        # this is the one route that actually changes anything about the
        # install rather than just reporting on it.
        if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
            return jsonify(ok=False, error="Session expired; reload the page and try again."), 403
        if updater is None or not updater.can_self_update():
            return jsonify(ok=False, error="This install can't auto-update."), 400
        target_version = request.form.get("target_version", "").strip()
        # This becomes part of a github.com download URL (see updater.py's
        # DOWNLOAD_BASE); a plain semver shape can't smuggle in a path
        # segment or otherwise change what that URL points to, but there's
        # no reason to accept anything else here regardless -- the browser
        # only ever sends back the value update_checker itself reported.
        if not re.fullmatch(r"\d+\.\d+\.\d+", target_version):
            return jsonify(ok=False, error="invalid target_version"), 400
        if not updater.start(target_version):
            return jsonify(ok=False, error="An update is already running."), 409
        return jsonify(ok=True)

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

        def _row(s_):
            value = node.settings.get(s_)
            return {"key": s_.key, "label": s_.label, "help": s_.help,
                    "is_bool": s_.kind is bool,
                    "minimum": s_.minimum, "slider_max": s_.slider_max,
                    "value": value,
                    "supports_inf": s_.supports_inf,
                    "is_inf": s_.supports_inf and value == float("inf"),
                    "forced": node.settings.forced_by_env(s_),
                    "env_name": s_.env_name}
        ctx["settings"] = [_row(s_) for s_ in settings_mod.ALL]
        ctx["own_addr"] = node.addr
        return render_template("settings.html", **ctx)









    register_wallet_routes(app, local_reader_for(node), _NodeSigner(node), csrf_token,
                           xlm=_XlmSend(node))

    # Market and Trades live in their own module: this file is already long
    # and a node with swaps off never reaches any of it.
    import market_routes
    market_routes.register(app, node, csrf_token)

    @app.route("/api/peers/add", methods=["POST"])
    def api_add_peer():
        data = request.get_json(silent=True)
        host = data.get("host") if data else None
        port = data.get("port") if data else None
        if not (isinstance(host, str) and host.strip()
                and isinstance(port, int) and 0 < port <= 65535):
            return jsonify({"ok": False, "error": "need valid host and port"}), 400
        host = host.strip()
        try:
            ip = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                                    socket.SOCK_DGRAM)[0][4][0]
        except (socket.gaierror, UnicodeError, OSError):
            return jsonify({"ok": False, "error": f"cannot resolve {host}"}), 400
        addr = f"{ip}:{port}"
        # Contact it first, like any other source: nothing is admitted on
        # the operator's say-so alone. A PONG proves a live node of this
        # chain is there; a reply carrying our own instance id (whatever
        # port or IP got us back here) marks it as this very process.
        try:
            udp = node.gossip.udp
        except AttributeError:
            udp = None
        if udp is not None:
            try:
                answered = udp.ping(addr) is not None
            except Exception:
                answered = False
            if pool.is_self(addr):
                return jsonify({"ok": False,
                                "error": "that address is this node itself"}), 400
            if not answered:
                return jsonify({"ok": False,
                                "error": "no answer: not reachable, or not a "
                                         "node on this chain"}), 400
        pool.add(addr, allow_private=True)
        return jsonify({"ok": True})

    return app
