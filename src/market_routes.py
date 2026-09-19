"""The Market and Trades pages.

Kept out of api.py because api.py is already long and this is a separable
feature: a node with swaps turned off never reaches any of it.

The pages are written to read as buying and selling. The increment
machinery underneath is the safety property, not the subject, so it
appears as a progress bar and one sentence about what is at stake rather
than as the interface. Nobody wants to think about step schedules; they
want to know what they pay, what they get, and what it costs them if the
other side vanishes.

Everything here lives on the private app only, like /send, because it
signs with the wallet key.
"""

import logging
import secrets as _secrets
import time

import crypto as crypto_mod
import market as market_mod
import settings as settings_mod
import swap as swap_mod
import swap_engine
import trust as trust_mod
import xlm as xlm_mod
from flask import redirect, render_template, request
from params import TICKS_PER_LAPSE
from trade_storage import (
    Increment, PeerRecord, Trade, ensure_tables,
    LEG_SETTLED, LEG_PENDING,
    TRADE_ACTIVE, TRADE_STALLED,
)

log = logging.getLogger("ec.market_routes")

STROOPS_PER_XLM = 10_000_000
LAPSE_BLOCK_SECONDS = 120


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def fmt_xlm(stroops):
    """Stroops as XLM, trailing zeros trimmed.

    A price of 0.0010000 reads as noise next to one of 0.0012345, and the
    trailing zeros are never significant here since stroops are integers.
    """
    text = f"{stroops / STROOPS_PER_XLM:.7f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_price(stroops_per_lapse):
    return fmt_xlm(stroops_per_lapse)


def short_addr(addr, words=3):
    """First few words of a twelve-word address.

    Enough to recognise a counterparty across a page without the row
    becoming a wall of text. The full address is always available in the
    trade detail, so nothing is hidden, only folded.
    """
    if not addr:
        return "?"
    parts = addr.split(".")
    if len(parts) <= words:
        return addr
    return ".".join(parts[:words]) + "…"


_LEG_LABELS = {
    LEG_SETTLED: ('<span class="leg-done">confirmed</span>'),
    "submitted": ('<span class="leg-wait">sent, confirming</span>'),
    "intent": ('<span class="leg-wait">sending</span>'),
    "dead": ('<span class="leg-wait">retrying</span>'),
    LEG_PENDING: ('<span class="leg-idle">not yet</span>'),
}


def leg_label(state):
    return _LEG_LABELS.get(state, f'<span class="leg-idle">{state}</span>')


def parse_lapse(raw):
    """A user-typed LAPSE amount into ticks.

    Rejects rather than rounds. Silently truncating somebody's amount is
    how a trade ends up being for a different number than the one they
    read back to themselves before signing.
    """
    text = (raw or "").strip().replace(",", "")
    if not text:
        raise ValueError("enter an amount")
    try:
        from decimal import Decimal, InvalidOperation
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{raw!r} is not a number")
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    ticks = value * TICKS_PER_LAPSE
    if ticks != ticks.to_integral_value():
        raise ValueError("that is more decimal places than LAPSE has")
    return int(ticks)


def parse_xlm(raw):
    """A user-typed XLM amount into stroops, same strictness."""
    text = (raw or "").strip().replace(",", "")
    if not text:
        raise ValueError("enter a price")
    try:
        from decimal import Decimal, InvalidOperation
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{raw!r} is not a number")
    if value <= 0:
        raise ValueError("price must be greater than zero")
    stroops = value * STROOPS_PER_XLM
    if stroops != stroops.to_integral_value():
        raise ValueError("XLM has seven decimal places at most")
    return int(stroops)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def xlm_keyfile_path(node):
    """Where this node's Stellar trading wallet lives, next to its own
    key file. A module-level function (rather than staying a closure
    inside register()) so /send in api.py can find the same wallet for
    withdrawals without duplicating the path logic."""
    import os
    return os.path.join(os.path.dirname(os.path.abspath(node.keyfile)),
                        "xlm_trading.key")


def register(app, node, csrf_token):
    """Attach the Market and Trades pages to the private app.

    Deliberately creates nothing. Registering a route should not touch a
    database: the app is built before storage is necessarily open, and a
    node that never trades should not gain swap tables just by having the
    pages wired up. Each route calls ensure_tables when it actually needs
    them.
    """

    def xlm_keyfile():
        return xlm_keyfile_path(node)

    def swaps_on():
        return node.settings.get(settings_mod.SWAP_ENABLED)

    def confirm_depth():
        return max(node.settings.get(settings_mod.SWAP_CONFIRM_DEPTH),
                   swap_engine.MIN_CONFIRM_DEPTH)

    def stranger_cap():
        return node.settings.get(settings_mod.SWAP_STRANGER_CAP_STROOPS)

    def csrf_ok():
        return _secrets.compare_digest(
            request.form.get("csrf_token", ""), csrf_token)

    def peer_trust(addr):
        return trust_mod.get_detail(
            addr,
            trust_mod.address_age_blocks(node, addr),
            node.view.state.get_balance(addr))

    def trust_badge(addr):
        detail = peer_trust(addr)
        if detail["abandoned_count"]:
            return ('<span class="trust-badge" style="color:var(--loss)" '
                    'title="Took a payment and did not reciprocate">&#9888;</span>')
        if detail["score"] > 0:
            return ('<span class="trust-badge" style="color:var(--gain)" '
                    f'title="{detail["completed_count"]} completed trades">'
                    '&#10003;</span>')
        return ""

    app.jinja_env.globals.update(
        fmt_xlm=fmt_xlm, fmt_price=fmt_price, short_addr=short_addr,
        leg_label=leg_label, trust_badge=trust_badge)

    # -- Market --------------------------------------------------------

    @app.route("/market", methods=["GET", "POST"])
    def market():
        alert_ok = alert_err = ""
        height = node.view.height
        xlm_addr = xlm_mod.load_public_key(xlm_keyfile())

        if request.method == "POST" and swaps_on():
            if not csrf_ok():
                alert_err = "That page was stale. Reload and try again."
            else:
                action = request.form.get("action", "")
                try:
                    if action == "create_xlm_wallet":
                        xlm_addr, alert_ok = _create_wallet(node, xlm_keyfile())
                    elif action == "place_order":
                        alert_ok = _place_order(node, xlm_addr, height)
                    elif action == "cancel_order":
                        alert_ok = _cancel_order(node)
                    elif action == "auto_fill":
                        result = _auto_fill(node, xlm_keyfile(), height,
                                            confirm_depth(), stranger_cap())
                        alert_ok = _auto_fill_message(result)
                except ValueError as e:
                    alert_err = str(e)
                except market_mod.OrderRejected as e:
                    alert_err = str(e)
                except Exception as e:
                    log.warning("[market] action %s failed", action, exc_info=True)
                    alert_err = f"That did not work: {e}"

        depth = market_mod.book_depth(height, exclude_maker=node.addr)
        best = market_mod.best_prices(height, exclude_maker=node.addr)
        ticker = market_mod.ticker_price(node)

        return render_template(
            "market.html", title="Market",
            swap_enabled=swaps_on(),
            alert_ok=alert_ok, alert_err=alert_err,
            depth=depth, best=best, ticker=ticker,
            lapse_addr=node.addr,
            lapse_balance=node.view.state.get_balance(node.addr),
            xlm_addr=xlm_addr,
            xlm_account_exists=_account_exists(xlm_addr),
            xlm_spendable=_spendable(xlm_addr),
            xlm_locked=_locked(xlm_addr),
            xlm_usd=xlm_mod.get_xlm_usd(),
            suggested_price=_suggested_price(best, ticker),
            my_orders=_my_orders(node, height),
            csrf_token=csrf_token)

    # -- Taking an order -----------------------------------------------

    @app.route("/market/take/<order_id>", methods=["GET", "POST"])
    def market_take(order_id):
        if not swaps_on():
            return redirect("/market")
        row = market_mod.get_order(order_id)
        if row is None:
            return render_template("error.html", title="Gone",
                                   message="That order is no longer here."), 404

        alert_err = ""
        height = node.view.height
        remaining = market_mod.remaining_ticks(row)
        detail = peer_trust(row.maker_lapse_addr)
        cap = swap_mod.exposure_cap_stroops(detail["score"], stranger_cap())

        # The taker's side is the opposite of the maker's.
        taking_side = "buy" if row.direction == "sell" else "sell"
        maker_xlm_unfunded = _maker_xlm_unfunded(row, _account_exists)
        max_fill_ticks = min(remaining, row.max_fill or remaining)
        max_safe_stroops = swap_mod.max_safe_trade_stroops(cap)
        max_safe_lapse = swap_mod.lapse_for_xlm(max_safe_stroops,
                                                row.price_stroops_per_lapse)
        too_large = max_fill_ticks > max_safe_lapse
        default_ticks = min(max_fill_ticks, max_safe_lapse)

        if request.method == "POST":
            if not csrf_ok():
                alert_err = "That page was stale. Reload and try again."
            else:
                try:
                    session_id = _start_trade(node, row, height,
                                              xlm_keyfile(), confirm_depth(),
                                              stranger_cap())
                    return redirect("/trades")
                except swap_mod.TradeTooLarge as e:
                    alert_err = (
                        "That is more than this node will risk with this "
                        "counterparty in one go. The most it will do is "
                        f"{swap_mod.lapse_for_xlm(e.max_safe_stroops, row.price_stroops_per_lapse) / TICKS_PER_LAPSE:.4f} LAPSE.")
                except (ValueError, market_mod.OrderRejected,
                       market_mod.FillRequestRejected) as e:
                    alert_err = str(e)
                except Exception as e:
                    log.warning("[market] starting a trade failed", exc_info=True)
                    alert_err = f"That did not work: {e}"

        planned_steps = _planned_steps(default_ticks, row, cap)
        return render_template(
            "market_take.html", title="Trade",
            order=row, remaining=remaining, trust=detail,
            taking_side=taking_side,
            max_fill_ticks=max_fill_ticks,
            max_fill_lapse=max_fill_ticks / TICKS_PER_LAPSE,
            default_fill=f"{default_ticks / TICKS_PER_LAPSE:.4f}",
            price_xlm=row.price_stroops_per_lapse / STROOPS_PER_XLM,
            planned_steps=planned_steps,
            step_cap_stroops=cap,
            too_large=too_large, max_safe_lapse=max_safe_lapse,
            confirm_depth=confirm_depth(),
            eta_seconds=planned_steps * confirm_depth() * LAPSE_BLOCK_SECONDS,
            maker_xlm_unfunded=maker_xlm_unfunded,
            account_min_xlm=fmt_xlm(xlm_mod.ACCOUNT_MIN_BALANCE_STROOPS),
            alert_err=alert_err, csrf_token=csrf_token)

    # -- Trades --------------------------------------------------------

    @app.route("/trades")
    def trades():
        ensure_tables()
        active, history = [], []
        for row in Trade.select().order_by(Trade.updated_at.desc()):
            view = _trade_view(row, stranger_cap(), peer_trust)
            (active if row.status in (TRADE_ACTIVE, TRADE_STALLED)
             else history).append(view)

        peers = []
        for record in PeerRecord.select():
            detail = peer_trust(record.lapse_addr)
            peers.append({"addr": record.lapse_addr, **detail})
        peers.sort(key=lambda p: (-p["score"], p["addr"]))

        return render_template("trades.html", title="Trades",
                               active=active, history=history[:50],
                               peers=peers, now=time.time(),
                               worker=_worker_view(node),
                               alert_ok="", alert_err="")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _create_wallet(node, path):
    """Create the Stellar trading wallet, sealed under the node's own key.

    Sealed with the node's key-encryption key rather than a passphrase of
    its own, which is what lets the swap worker use it. A node keeps that
    key while it runs and discards the passphrase at startup, so a wallet
    with a separate passphrase could not be opened by anything running
    unattended, and a trade already part-paid would stall until somebody
    came back to type it. The passphrase asked for here is the node's own,
    and it is verified against the node key file before anything is
    written.
    """
    import os
    if os.path.exists(path):
        raise ValueError("this node already has a Stellar trading address")
    passphrase = request.form.get("passphrase", "").strip()
    if not passphrase:
        raise ValueError("your node passphrase is required")
    try:
        kek = crypto_mod.derive_kek(node.keyfile, passphrase)
        crypto_mod.decrypt_secret_key(node.keyfile, kek=kek)
    except ValueError:
        raise ValueError("that is not this node's passphrase")
    seed, public = xlm_mod.generate_keypair()
    xlm_mod.save_key(path, seed, public, kek=kek)
    del seed
    return public, ("Stellar address created and unlocked with your node "
                    "passphrase, so trades keep running without you here. "
                    "It holds nothing yet, which is fine: you can sell "
                    "LAPSE without funding it first.")


def _place_order(node, xlm_addr, height):
    if not xlm_addr:
        raise ValueError("create a Stellar address first")
    passphrase = request.form.get("passphrase", "").strip()
    if not passphrase:
        raise ValueError("a passphrase is required")

    direction = request.form.get("direction", "buy")
    if direction not in ("buy", "sell"):
        raise ValueError("choose buy or sell")

    lapse_total = parse_lapse(request.form.get("amount_lapse"))
    price = parse_xlm(request.form.get("price_xlm"))
    min_fill_raw = (request.form.get("min_fill_lapse") or "").strip()
    min_fill = parse_lapse(min_fill_raw) if min_fill_raw else 0

    hours = int(request.form.get("expiry_hours") or 168)
    if not 1 <= hours <= 720:
        raise ValueError("expiry must be between 1 and 720 hours")
    expiry_block = height + max(int(hours * 3600 / LAPSE_BLOCK_SECONDS), 1)

    # Checked here rather than left for the chain, because finding out once
    # a counterparty has already committed to a trade is what turns a
    # simple mistake into a stall, and a stall is what blame is measured
    # from. Both sides of the book get the same treatment: a sell order
    # needs the LAPSE it offers, a buy order needs the XLM it would pay
    # out if filled all the way.
    if direction == "sell" and lapse_total > node.view.state.get_balance(node.addr):
        raise ValueError("you do not hold that much LAPSE")
    if direction == "buy":
        required_stroops = swap_mod.xlm_for_lapse(lapse_total, price)
        if required_stroops > _spendable(xlm_addr):
            raise ValueError("you do not hold enough XLM to fill this buy "
                             "order all the way")

    order = market_mod.build_order(
        maker_lapse_addr=node.addr, maker_xlm_addr=xlm_addr,
        direction=direction, lapse_total=lapse_total,
        price_stroops_per_lapse=price, expiry_block=expiry_block,
        pubkey_hex=node.pk_hex, min_fill=min_fill)

    kek = crypto_mod.derive_kek(node.keyfile, passphrase)
    market_mod.sign_order(order, node.keyfile, kek)
    market_mod.verify_order(order, current_height=height)
    market_mod.store_order(order)
    node.publish_order(order)
    return "Order posted and sent to your peers."


def _cancel_order(node):
    passphrase = request.form.get("passphrase", "").strip()
    if not passphrase:
        raise ValueError("a passphrase is required")
    order_id = request.form.get("order_id", "")
    kek = crypto_mod.derive_kek(node.keyfile, passphrase)
    body = market_mod.cancellation_for(order_id, node.pk_hex,
                                       node.keyfile, kek)
    market_mod.apply_cancellation(order_id, node.addr)
    node.publish_order(body)
    return "Order withdrawn. Anything already delivered stands."


def _start_trade(node, order_row, height, xlm_keyfile_path, depth, cap):
    """Read the taker's form and send a fill request against one order.

    Thin on purpose: _open_trade is the reusable core, taking the amount
    and passphrase as plain arguments rather than reading request.form,
    so _auto_fill can call it once per order while sweeping the book
    without a fake HTTP form per slice.
    """
    passphrase = request.form.get("passphrase", "").strip()
    if not passphrase:
        raise ValueError("a passphrase is required")
    lapse_total = parse_lapse(request.form.get("amount_lapse"))
    return _open_trade(node, order_row, height, xlm_keyfile_path, depth, cap,
                       lapse_total, passphrase)


def _open_trade(node, order_row, height, xlm_keyfile_path, depth, cap,
                lapse_total, passphrase):
    """Send a signed fill request against one order. Returns its
    session_id.

    Nothing is created here except the request itself: no Trade exists on
    this side until the maker explicitly agrees (see
    swap_engine.check_fill_responses), so nothing is ever paid on the
    strength of this node's own say-so. depth is accepted for the
    caller's convenience (every call site already has it to hand) but is
    no longer used here; the trade that eventually opens reads its own
    confirm depth fresh when it is created, on whichever side creates it.
    """
    if not passphrase:
        raise ValueError("a passphrase is required")
    xlm_addr = xlm_mod.load_public_key(xlm_keyfile_path)
    if not xlm_addr:
        raise ValueError("create a Stellar address first")

    # market_take fetches the order by id directly rather than through
    # open_orders(), which is the only place expiry is normally filtered,
    # so a stale link or a fill submitted right as an order ages out must
    # be caught here too. Without this a request outlives the order it was
    # supposedly filling.
    if order_row.expiry_block <= height:
        raise market_mod.OrderRejected("this order has expired")

    # Checked, not just used: a wrong passphrase here would otherwise
    # surface as a request this node can never act on when it is
    # answered, discovered only once a counterparty was already waiting.
    try:
        kek = crypto_mod.derive_kek(node.keyfile, passphrase)
        crypto_mod.decrypt_secret_key(node.keyfile, kek=kek)
        xlm_mod.decrypt_seed(xlm_keyfile_path, kek=kek)
    except ValueError:
        raise ValueError("that is not this node's passphrase")

    market_mod.validate_fill(order_row, lapse_total)

    xlm_total = swap_mod.xlm_for_lapse(lapse_total,
                                       order_row.price_stroops_per_lapse)

    # A maker selling LAPSE means this node pays XLM; a maker buying LAPSE
    # means this node pays LAPSE. Checked before anything is sent: a fill
    # nobody could ever pay for should never reach the maker only to be
    # discovered unfundable after it already agreed.
    i_send = "xlm" if order_row.direction == "sell" else "lapse"
    if i_send == "xlm":
        if xlm_total > _spendable(xlm_addr):
            raise ValueError("you do not hold enough XLM for this fill")
    elif lapse_total > node.view.state.get_balance(node.addr):
        raise ValueError("you do not hold that much LAPSE")

    # A local, advisory check only: the real cap that matters is the
    # maker's own, applied when it decides (swap_engine._answer_one).
    # This exists so a taker sees "too large" immediately rather than
    # waiting a full round trip to be told the same thing.
    detail = trust_mod.get_detail(
        order_row.maker_lapse_addr,
        trust_mod.address_age_blocks(node, order_row.maker_lapse_addr),
        node.view.state.get_balance(order_row.maker_lapse_addr))
    swap_mod.plan(lapse_total, xlm_total, detail["score"], stranger_cap=cap)

    session_id = swap_mod.new_session_id(order_row.order_id, node.addr)

    req = market_mod.build_fill_request(
        order_id=order_row.order_id, session_id=session_id,
        taker_lapse_addr=node.addr, taker_xlm_addr=xlm_addr,
        lapse_total=lapse_total, pubkey_hex=node.pk_hex)
    market_mod.sign_fill_request(req, node.keyfile, kek)
    market_mod.store_fill_request(req)
    node.publish_fill_request(req)
    log.info("[market] fill request %s sent against order %s",
             session_id, order_row.order_id)
    return session_id


def _plan_auto_fill(node, direction, lapse_total, max_price, height, stranger_cap_val):
    """Work out which orders _auto_fill would take and how much of each,
    without opening a single trade. Pure and side-effect-free, so it can
    be run once to check a minimum-fill tolerance before anything
    irreversible happens (see _auto_fill), and safe to re-run.

    Returns (slices, skipped): slices is [(order_row, take_ticks), ...]
    in the order they would be filled; skipped is [(order_id, reason)].
    """
    book = market_mod.book_depth(height, exclude_maker=node.addr)
    candidates = book["sells"] if direction == "buy" else book["buys"]

    remaining = lapse_total
    slices = []
    skipped = []
    for entry in candidates:
        if remaining <= 0:
            break
        if max_price is not None:
            # Sorted best-first on both sides (sells ascending, buys
            # descending; see market.book_depth), so the first entry
            # past the limit means nothing further qualifies either.
            if direction == "buy" and entry["price"] > max_price:
                break
            if direction == "sell" and entry["price"] < max_price:
                break

        order_row = market_mod.get_order(entry["order_id"])
        if order_row is None:
            continue   # cancelled between the read above and here

        detail = trust_mod.get_detail(
            order_row.maker_lapse_addr,
            trust_mod.address_age_blocks(node, order_row.maker_lapse_addr),
            node.view.state.get_balance(order_row.maker_lapse_addr))
        cap_here = swap_mod.exposure_cap_stroops(detail["score"], stranger_cap_val)
        max_safe_lapse = swap_mod.lapse_for_xlm(
            swap_mod.max_safe_trade_stroops(cap_here), order_row.price_stroops_per_lapse)
        take_ticks = min(remaining, entry["remaining"], max_safe_lapse)

        if take_ticks <= 0:
            skipped.append((order_row.order_id,
                           "no safe amount with this counterparty yet"))
            continue
        if order_row.min_fill and take_ticks < order_row.min_fill:
            skipped.append((order_row.order_id,
                           "below this order's own minimum fill"))
            continue

        slices.append((order_row, take_ticks))
        remaining -= take_ticks
    return slices, skipped


def _auto_fill(node, xlm_keyfile_path, height, depth, stranger_cap_val):
    """The market-order half of this market: fill up to a requested
    amount by sweeping the best compatible orders in the book, without
    the user ever choosing which one.

    direction is what the user wants to do ("buy" or "sell" LAPSE); it
    matches against the opposite side of the book, exactly as
    market_take already does for a single manually-picked order. Orders
    are tried best price first (book_depth's own sort order), each
    filled by as much as is safe with that specific counterparty (the
    same trust-scaled exposure cap _open_trade already enforces),
    continuing to the next order for whatever remains. max_price is the
    worst price the user will accept; omitted, this behaves like a real
    market order and takes whatever is currently on offer.

    min_total_lapse is the smallest total the user is willing to walk
    away with; below it, nothing at all is traded. This has to be
    checked with a full dry-run planning pass (_plan_auto_fill) *before*
    a single trade opens: unlike a real exchange's matching engine,
    opening a trade here is not a reversible ledger entry, it is a
    signed fill request sent to the whole network and, once the maker
    accepts, a Trade row this node's own worker starts trying to pay
    into. There is no cheap way
    to undo the first three trades of a sweep upon discovering the
    fourth can't happen; the only sound order is decide once, on a plan
    that changes nothing, then execute exactly that plan.

    Ordinary market conditions (thin book, one counterparty's own safe
    limit too small, an order's minimum fill not met, or the whole
    sweep falling short of min_total_lapse) are reported in the result,
    never raised: a market order legitimately filling only part of what
    was asked, or refusing to trade a trivial fraction of it, is success,
    not failure. Only the passphrase and wallet checks raise, since a
    wrong passphrase would fail every slice identically.
    """
    passphrase = request.form.get("passphrase", "").strip()
    if not passphrase:
        raise ValueError("a passphrase is required")
    direction = request.form.get("direction", "buy")
    if direction not in ("buy", "sell"):
        raise ValueError("choose buy or sell")
    lapse_total = parse_lapse(request.form.get("amount_lapse"))
    max_price_raw = (request.form.get("max_price_xlm") or "").strip()
    max_price = parse_xlm(max_price_raw) if max_price_raw else None
    min_total_raw = (request.form.get("min_total_lapse") or "").strip()
    min_total = parse_lapse(min_total_raw) if min_total_raw else 0

    xlm_addr = xlm_mod.load_public_key(xlm_keyfile_path)
    if not xlm_addr:
        raise ValueError("create a Stellar address first")
    # Verified once here, not left to fail inside the loop: a wrong
    # passphrase applies identically to every slice, so failing fast
    # with one clear message beats repeating the same rejection once
    # per candidate order.
    try:
        kek = crypto_mod.derive_kek(node.keyfile, passphrase)
        crypto_mod.decrypt_secret_key(node.keyfile, kek=kek)
        xlm_mod.decrypt_seed(xlm_keyfile_path, kek=kek)
    except ValueError:
        raise ValueError("that is not this node's passphrase")

    slices, skipped = _plan_auto_fill(node, direction, lapse_total, max_price,
                                      height, stranger_cap_val)
    plannable = sum(ticks for _order, ticks in slices)
    if min_total and plannable < min_total:
        return {"requested": lapse_total, "filled": 0, "remaining": lapse_total,
                "fills": [], "skipped": skipped, "min_not_met": True,
                "would_have_filled": plannable, "min_total": min_total}

    remaining = lapse_total
    fills = []
    for order_row, take_ticks in slices:
        try:
            session_id = _open_trade(node, order_row, height, xlm_keyfile_path,
                                     depth, stranger_cap_val, take_ticks, passphrase)
        except (ValueError, market_mod.OrderRejected,
               market_mod.FillRequestRejected, swap_mod.TradeTooLarge) as e:
            # The plan said this was safe; something changed between
            # planning and here (the order was cancelled, a fill request
            # cap was hit by something else). Move on rather than lose
            # the rest of an otherwise-good plan over one stale slice.
            skipped.append((order_row.order_id, str(e)))
            continue
        fills.append({"session_id": session_id, "lapse": take_ticks,
                      "price": order_row.price_stroops_per_lapse,
                      "maker": order_row.maker_lapse_addr})
        remaining -= take_ticks

    return {"requested": lapse_total, "filled": lapse_total - remaining,
            "remaining": remaining, "fills": fills, "skipped": skipped,
            "min_not_met": False}


def _auto_fill_message(result):
    lapse = lambda ticks: ticks / TICKS_PER_LAPSE

    if result.get("min_not_met"):
        return (f"Only {lapse(result['would_have_filled']):.4f} LAPSE was "
                f"available on acceptable terms, short of the "
                f"{lapse(result['min_total']):.4f} LAPSE minimum you set, so "
                f"nothing was traded. Lower the minimum, raise your price "
                f"limit, or post a resting order instead.")

    if not result["fills"]:
        return ("Nothing could be filled right now: nothing on the book met "
                "your price, or safe limits with those counterparties were "
                "already reached. Post a resting order instead if you are "
                "willing to wait for one.")

    # One line per distinct price actually paid, in the order filled,
    # so "bought 2 at x, 3 at y" reads as the tiers it actually was
    # rather than one blended number that hides what happened.
    tiers = []
    for fill in result["fills"]:
        if tiers and tiers[-1]["price"] == fill["price"]:
            tiers[-1]["lapse"] += fill["lapse"]
            tiers[-1]["trades"] += 1
        else:
            tiers.append({"price": fill["price"], "lapse": fill["lapse"], "trades": 1})
    tier_text = ", ".join(
        f"{lapse(t['lapse']):.4f} LAPSE at {fmt_price(t['price'])} XLM each"
        for t in tiers)
    n = len(result["fills"])
    msg = f"Filled {tier_text} ({n} trade{'' if n == 1 else 's'} total)."
    if result["remaining"] > 0:
        msg += (f" {lapse(result['remaining']):.4f} LAPSE could not be filled "
               f"right now (nothing left on the book met your price, or safe "
               f"limits with those counterparties were reached); post a "
               f"resting order for the rest if you want to wait for one.")
    return msg


# ---------------------------------------------------------------------------
# View building
# ---------------------------------------------------------------------------

def _trade_view(row, cap, peer_trust):
    steps = list(Increment.select()
                 .where(Increment.session_id == row.session_id)
                 .order_by(Increment.n))
    done = [s for s in steps
            if s.out_state == LEG_SETTLED and s.in_state == LEG_SETTLED]
    delivered = sum(s.lapse_amount for s in done)
    # What is actually outstanding right now: a leg this node sent that the
    # counterparty has not matched. Zero when it is their turn to move
    # first, since nothing of ours is out there.
    at_risk = sum(s.xlm_amount for s in steps
                  if s.out_state == LEG_SETTLED and s.in_state != LEG_SETTLED)
    return {
        "session_id": row.session_id,
        "status": row.status,
        "peer_lapse_addr": row.peer_lapse_addr,
        "i_receive_lapse": row.i_send == "xlm",
        "lapse_total": row.lapse_total,
        "xlm_total": row.xlm_total,
        "increment_count": row.increment_count,
        "done_steps": len(done),
        "delivered_lapse": delivered,
        "pct": int(len(done) * 100 / row.increment_count) if row.increment_count else 0,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "steps": steps,
        "step_cap": max((s.xlm_amount for s in steps), default=0),
        "at_risk": at_risk,
    }


def _worker_view(node):
    """What the swap worker is doing right now, for the Trades page.

    Without this a trade that is not progressing looks identical whether
    the wallet is locked, Horizon is unreachable, or the counterparty
    genuinely stopped answering. Those need different reactions from the
    user, so the state has to be visible rather than inferred from silence.
    """
    worker = getattr(node, "swap_worker", None)
    if worker is None:
        return {"state": "off", "message": "", "last_error": "", "passes": 0}

    status = worker.status()
    if not status["enabled"]:
        return {"state": "off", "message": "", "last_error": "", "passes": 0}
    if not status["running"]:
        return {"state": "stopped",
                "message": "The swap worker is not running. Restart this "
                           "node to resume trading.",
                "last_error": status["last_error"], "passes": status["passes"]}
    if not status["unlocked"]:
        return {"state": "locked",
                "message": "Your wallet is locked, so no trade can send its "
                           "next step. Unlock this node to keep them moving.",
                "last_error": status["last_error"], "passes": status["passes"]}
    now = time.time()
    if status["paused_until"] > now:
        wait = int(status["paused_until"] - now)
        message = f"Stellar's network could not be reached; retrying in {wait}s."
        if status["last_error"]:
            message += f" ({status['last_error']})"
        return {"state": "paused", "message": message,
                "last_error": status["last_error"], "passes": status["passes"]}
    return {"state": "ok", "message": "Running normally.",
            "last_error": status["last_error"], "passes": status["passes"]}


def _my_orders(node, height):
    rows = []
    for row in market_mod.orders_by_maker(node.addr, height):
        delivered = market_mod.delivered_ticks(row.order_id)
        reserved = market_mod.reserved_ticks(row.order_id)
        rows.append({
            "order_id": row.order_id,
            "direction": row.direction,
            "lapse_total": row.lapse_total,
            "remaining": max(row.lapse_total - delivered - reserved, 0),
            "delivered": delivered,
            # Accepted but not yet settled: someone has committed to
            # this much (see market.reserved_ticks) but nothing has
            # actually moved for it yet, which is a different thing to
            # tell a maker than "delivered" is.
            "reserved": reserved,
            "pct_delivered": int(delivered * 100 / row.lapse_total) if row.lapse_total else 0,
            "price_stroops_per_lapse": row.price_stroops_per_lapse,
            "blocks_left": max(row.expiry_block - height, 0),
        })
    return rows


def _planned_steps(ticks, order_row, cap):
    if ticks <= 0:
        return swap_mod.MIN_INCREMENTS
    try:
        stroops = swap_mod.xlm_for_lapse(ticks, order_row.price_stroops_per_lapse)
        return swap_mod.increment_count(stroops, cap)
    except (swap_mod.TradeTooLarge, ValueError):
        return swap_mod.MAX_INCREMENTS


def _maker_xlm_unfunded(order_row, account_exists):
    """Whether a taker filling this order is about to pay for creating
    the maker's Stellar account, on top of the agreed amount.

    A maker selling LAPSE means the taker pays XLM (see _start_trade),
    and the first payment to a maker address with no account yet costs
    at least the network minimum regardless of the agreed step (see
    swap_engine._xlm_send_amount). Only true on a definite "no account"
    (account_exists returns False), never on "unknown" (None, an
    unreachable Horizon), so a transient outage never reads to a taker
    as a cost that may not even exist.
    """
    taker_pays_xlm = order_row.direction == "sell"
    return taker_pays_xlm and account_exists(order_row.maker_xlm_addr) is False


def _suggested_price(best, ticker=None):
    """What to prefill the order form's price field with.

    Top of book wins when there is one, since it is a live, standing
    offer. An empty book falls back to this node's own ticker (see
    market.ticker_price) rather than leaving a new poster with nothing:
    a stale trade price is still a better starting point than a blank
    field, as long as the page says which one it is (see market.html).
    """
    price = best["best_sell"] or best["best_buy"] or ticker
    return fmt_xlm(price) if price else ""


def _account_exists(addr):
    """Whether the Stellar account is live, or None when Horizon is down.

    None rather than False on a network failure: telling somebody their
    account does not exist because a public API timed out would send them
    to fund an account they already have.
    """
    if not addr:
        return False
    try:
        return xlm_mod.account_exists(addr)
    except xlm_mod.XLMError:
        return None


def _spendable(addr):
    if not addr:
        return 0
    try:
        return xlm_mod.get_spendable_stroops(addr)
    except xlm_mod.XLMError:
        return 0


def _locked(addr):
    if not addr:
        return 0
    try:
        return max(xlm_mod.get_balance_stroops(addr)
                   - xlm_mod.get_spendable_stroops(addr), 0)
    except xlm_mod.XLMError:
        return 0
