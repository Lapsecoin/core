"""The Market and Trades pages.

Kept out of api.py because api.py is already long and this is a
separable feature on its own.

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
    Increment, Trade, ensure_tables,
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
            node.view.state.get_balance(addr), node=node)

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

    def pending_maker_requests():
        """Live fill requests against this node's own orders that have
        not yet been answered, each with the same trust detail and cap
        check the automatic decision (swap_engine._answer_one) would
        use, so accepting or declining by hand is as informed as
        auto-accept is. A request can be stuck here for any of several
        reasons (a locked wallet, an unfundable leg, a step over the
        exposure cap, or a counterparty below SWAP_AUTO_ACCEPT_MIN_TRUST),
        and a maker deserves to see which one rather than silence.
        """
        min_trust = node.settings.get(settings_mod.SWAP_AUTO_ACCEPT_MIN_TRUST)
        rows = []
        for order_row in market_mod.orders_by_maker_with_claims(node.addr):
            for req in market_mod.requests_for_order(order_row.order_id):
                if market_mod.get_fill_response(req.request_id) is not None:
                    continue
                if Trade.get_or_none(Trade.session_id == req.session_id) is not None:
                    continue
                detail = peer_trust(req.taker_lapse_addr)
                xlm_total = swap_mod.xlm_for_lapse(
                    req.lapse_total, order_row.price_stroops_per_lapse)
                try:
                    swap_mod.plan(req.lapse_total, xlm_total, detail["score"],
                                  stranger_cap=stranger_cap())
                    fits, fit_note = True, "fits your current exposure cap"
                except swap_mod.TradeTooLarge as e:
                    fits, fit_note = False, str(e)
                below_trust_floor = detail["score"] < min_trust
                rows.append({
                    "request_id": req.request_id,
                    "order_id": order_row.order_id,
                    "direction": order_row.direction,
                    "taker_lapse_addr": req.taker_lapse_addr,
                    "lapse_total": req.lapse_total,
                    "received_at": req.received_at,
                    "trust": detail,
                    "fits_cap": fits,
                    "fit_note": fit_note,
                    "below_trust_floor": below_trust_floor,
                })
        rows.sort(key=lambda r: -r["received_at"])
        return rows

    def answer_fill_request_action():
        """Accept or decline one pending request, from the Market page's
        manual-review section. Same signing requirement as cancelling an
        order (_cancel_order): a freshly entered passphrase, because
        this produces a new signed FillResponse exactly as cancelling
        produces a new signed cancellation.
        """
        passphrase = request.form.get("passphrase", "").strip()
        if not passphrase:
            raise ValueError("a passphrase is required")
        request_id = request.form.get("request_id", "")
        decision = request.form.get("decision", "")
        if not request_id or decision not in ("accept", "decline"):
            raise ValueError("choose accept or decline")

        kek = crypto_mod.derive_kek(node.keyfile, passphrase)
        my_xlm_addr = xlm_mod.load_public_key(xlm_keyfile())
        if my_xlm_addr is None:
            raise ValueError("create a Stellar address first")

        def secrets():
            try:
                seed = xlm_mod.decrypt_seed(xlm_keyfile(), kek=kek)
            except (OSError, ValueError):
                return None, None
            return kek, seed

        engine = swap_engine.Engine(
            swap_engine.LapseAdapter(node), swap_engine.XLMAdapter(xlm_keyfile()),
            secrets)
        ok = swap_engine.decide_fill_request(
            engine, node, my_xlm_addr, stranger_cap(), confirm_depth(),
            request_id, accept=(decision == "accept"))
        if not ok:
            raise ValueError("that request could not be answered (it may "
                             "already have been handled)")
        return ("Accepted; the trade will start shortly." if decision == "accept"
                else "Declined.")

    app.jinja_env.globals.update(
        fmt_xlm=fmt_xlm, fmt_price=fmt_price, short_addr=short_addr,
        leg_label=leg_label, trust_badge=trust_badge)

    # -- Market --------------------------------------------------------

    @app.route("/market", methods=["GET", "POST"])
    def market():
        alert_ok = alert_err = ""
        height = node.view.height
        xlm_addr = xlm_mod.load_public_key(xlm_keyfile())

        if request.method == "POST":
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
                    elif action == "answer_fill_request":
                        alert_ok = answer_fill_request_action()
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

        lapse_balance = node.view.state.get_balance(node.addr)
        xlm_spendable = _spendable(xlm_addr)
        # What this node's own open orders, combined, already ask for on
        # each side, checked against what it actually holds right now.
        # Each order was affordable on its own when posted
        # (market_routes._place_order), but nothing rechecks the sum as
        # more orders pile up or a balance moves, so this is the one
        # place a maker sees "you have more posted than you can cover"
        # before a taker's fill request finds out the hard way (see
        # market.maker_committed and swap_engine._pending_send_total,
        # which is what actually keeps that discovery from costing
        # anyone real money).
        lapse_committed, xlm_committed = market_mod.maker_committed(node.addr, height)

        return render_template(
            "market.html", title="Market",
            alert_ok=alert_ok, alert_err=alert_err,
            depth=depth, best=best, ticker=ticker,
            lapse_addr=node.addr,
            lapse_balance=lapse_balance,
            lapse_committed=lapse_committed,
            lapse_overcommitted=lapse_committed > lapse_balance,
            xlm_addr=xlm_addr,
            xlm_account_exists=_account_exists(xlm_addr),
            xlm_spendable=xlm_spendable,
            xlm_committed=xlm_committed,
            xlm_overcommitted=xlm_committed > xlm_spendable,
            xlm_locked=_locked(xlm_addr),
            xlm_usd=xlm_mod.get_xlm_usd(),
            suggested_price=_suggested_price(best, ticker),
            my_orders=_my_orders(node, height),
            pending_maker_requests=pending_maker_requests(),
            csrf_token=csrf_token)

    # -- The full order book --------------------------------------------

    @app.route("/market/book")
    def market_book():
        """The whole book, one side at a time: paged and sortable, unlike
        /market's own compact top-of-book preview. Always excludes this
        node's own orders, same as /market's preview: nobody needs to
        take their own order, and orders_by_maker (the Market page's
        "Your open orders" table) is where a maker manages those.
        """
        height = node.view.height
        side = request.args.get("side", "sell")
        if side not in ("sell", "buy"):
            side = "sell"
        sort = request.args.get("sort", "price")
        if sort not in market_mod.BOOK_SORTS:
            sort = "price"
        try:
            page_num = max(int(request.args.get("page", 1)), 1)
        except ValueError:
            page_num = 1
        page_size = market_mod.BOOK_PAGE_SIZE
        rows, total = market_mod.list_orders(
            height, side, exclude_maker=node.addr,
            sort=sort, offset=(page_num - 1) * page_size, limit=page_size)
        last_page = max((total + page_size - 1) // page_size, 1)
        if page_num > last_page:
            page_num = last_page
        return render_template(
            "market_book.html", title="Order book", height=height,
            side=side, sort=sort, rows=rows, total=total,
            page=page_num, last_page=last_page, page_size=page_size)

    # -- Taking an order -----------------------------------------------

    @app.route("/market/take/<order_id>", methods=["GET", "POST"])
    def market_take(order_id):
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
        maker_lapse_overcommitted = _maker_lapse_overcommitted(row, node, height)
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
            maker_lapse_overcommitted=maker_lapse_overcommitted,
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

        # Every address this node has ever traded with, not a separately
        # maintained list: standing itself is derived straight from
        # these same Trade rows (trust.local_tally), so there is nothing
        # to keep in sync between "who do I know" and "what do I know
        # about them".
        known_addrs = {row.peer_lapse_addr for row in
                       Trade.select(Trade.peer_lapse_addr).distinct()}
        peers = []
        for addr in known_addrs:
            detail = peer_trust(addr)
            peers.append({"addr": addr, **detail})
        peers.sort(key=lambda p: (-p["score"], p["addr"]))

        return render_template("trades.html", title="Trades",
                               active=active, history=history[:50],
                               pending_requests=_pending_requests(node),
                               peers=peers, now=time.time(),
                               worker=_worker_view(node),
                               receipts=_receipt_stats(),
                               my_standing=peer_trust(node.addr),
                               my_addr=node.addr,
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
    so it stays testable and callable without a fake HTTP form.
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
        node.view.state.get_balance(order_row.maker_lapse_addr), node=node)
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


def _pending_requests(node):
    """This node's own outstanding fill requests, as a taker, that have
    not yet become a trade one way or the other.

    The taker's own view. The maker's matching view, requests still
    waiting on a decision against this node's own orders, is
    register()'s pending_maker_requests helper on the Market page, not
    here: those two lists serve different questions (mine, waiting on
    somebody else vs. somebody else's, waiting on me) and a person
    looking at either page is asking one or the other, never both at
    once. At the default trust floor most of a maker's requests never
    sit long enough to be worth a list; when one is stuck (a locked
    wallet, a step over the exposure cap, or a counterparty below the
    floor), that other list is exactly where a click belongs.

    A request that already became a trade (swap_engine.check_fill
    _responses opened it) is excluded here on purpose: it belongs in the
    active trades list once it exists, not in both.
    """
    ensure_tables()
    rows = []
    for req in market_mod.requests_by_taker(node.addr):
        if Trade.get_or_none(Trade.session_id == req.session_id) is not None:
            continue
        order_row = market_mod.get_order(req.order_id)
        resp = market_mod.get_fill_response(req.request_id)
        if resp is None:
            status, detail = "pending", "Waiting on the maker to answer."
        elif resp.accepted:
            status, detail = "accepted", "Accepted; opening as a trade shortly."
        else:
            status = "declined"
            detail = resp.reason or "Declined, no reason given."
        rows.append({
            "order_id": req.order_id,
            "direction": order_row.direction if order_row else None,
            "maker_lapse_addr": order_row.maker_lapse_addr if order_row else None,
            "lapse_total": req.lapse_total,
            "sent_at": req.received_at,
            "status": status,
            "detail": detail,
        })
    rows.sort(key=lambda r: -r["sent_at"])
    return rows


def _receipt_stats():
    """How much network-sourced reputation data this node currently
    holds, and how much of it has not yet been chain-checked into
    something trust actually counts. Verification here is lazy (see
    trust._verify_addr_receipts): a receipt sits unverified until
    something actually asks for that specific address's standing, so
    "pending" is not a queue waiting its turn, it is simply "nobody has
    needed this one yet". Without this a page has no way to show whether
    a thin-looking track record means "nobody has reported anything" or
    "something is reported but not yet confirmed"."""
    from trade_storage import StepReceipt
    ensure_tables()
    total = StepReceipt.select().count()
    verified = StepReceipt.select().where(StepReceipt.verified == True).count()  # noqa: E712
    pending = StepReceipt.select().where(StepReceipt.verified.is_null()).count()
    return {"total": total, "verified": verified, "pending": pending}


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
            # What's left cannot actually be taken any more (see
            # market.open_orders, which hides such an order from takers
            # for exactly this reason): below min_fill, but still above
            # zero, so it never showed up as "fully delivered" either.
            # Without this a maker sees a nonzero remainder with no clue
            # that nobody can act on it until it expires.
            "below_min_fill": (0 < max(row.lapse_total - delivered - reserved, 0) < row.min_fill),
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


def _maker_lapse_overcommitted(order_row, node, height):
    """Whether this maker's own live sell orders, combined, already ask
    for more LAPSE than the maker's own chain balance actually holds.

    A single order's own post-time check only ever looks at that one
    order against the balance at that moment (market_routes._place_order);
    it says nothing about a second, later order that is also
    individually affordable but, added to the first, is not. That does
    not put anything a taker sends at risk any more (see
    swap_engine._pending_send_total: an accept the maker cannot actually
    fund now gets refused there, not discovered after payment), but it
    is still worth surfacing here rather than letting a taker's fill
    request go out only to bounce, since this is free to compute: orders
    and this maker's own LAPSE balance are both already-local chain
    data, not a Horizon call.
    """
    if order_row.direction != "sell":
        return False
    committed, _xlm = market_mod.maker_committed(order_row.maker_lapse_addr, height)
    balance = node.view.state.get_balance(order_row.maker_lapse_addr)
    return committed > balance


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
