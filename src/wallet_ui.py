"""The wallet's web pages, written once for two kinds of app.

The full node's private app and the light client both register these
handlers. They differ only in what they are handed:

  reader  where chain data comes from. LocalReader (local_reader.py) reads
          this node's own chain, RemoteReader (remote_reader.py) asks
          another node over HTTP. Both answer the same methods:

            fee_estimate()                    -> fee market dict
            account(addr, nick=, fees=)
                                              -> balance, nonce, board_floor, ...
            profile(addr)                     -> board icon and nickname, or None
            submit(tx)                        -> (ok, tx_hash | error)
            address_page(addr, page)          -> one page of history
            board_etag(chunks), board_page(chunks)
                                              -> the board, see board_view

  signer  who builds and signs a transaction: .addr, .pk_hex and
          .sign(outputs, fee, memo, passphrase, nonce). The full node's is
          api._NodeSigner (the node signs from its own state), the light
          client's is WalletSigner below (a bare Wallet, wallet.py).

Light-safe: imports nothing that pulls in the VDF, the chain database, or
the swap code, and a test keeps it that way.
"""

import gzip
import hashlib
import json
import logging
import math
import re
import secrets
from urllib.parse import urlencode

from flask import jsonify, make_response, redirect, render_template, request

import crypto as crypto_mod
import tx as tx_mod
from forms import ALREADY_SENT, forms_for
from board_view import (BOARD_MEMO_TAG, BOARD_POST_AMOUNT, DELETE_TAG,
                        MAX_POST_TEXT_BYTES, REPLY_REF_LEN, VOTE_DOWN_TAG,
                        VOTE_UP_TAG, apply_splice, board_ctx, build_board_body,
                        build_board_edit, make_splice)
from ui_common import (_pagination_window, _parse_csv_outputs,
                       _reword_insufficient_balance, fmt_balance,
                       render_board_text)

log = logging.getLogger("ec.wallet_ui")


# ---------------------------------------------------------------------------
# Fees and signing
# ---------------------------------------------------------------------------

def auto_fee(acct, addr, pk_hex, outputs, memo="", floor=0):
    """The fee this send will actually pay: whatever fee-per-byte clears
    the next block right now (see fee_estimate), and no more. There is no
    manual fee field for the same reason a real exchange doesn't ask you
    to guess one: overpaying buys nothing, underpaying just delays the
    transaction, and the node already knows the going rate live.

    acct is reader.account(addr, fees=True), the nonce and the going rate
    taken from one consistent answer. floor is a protocol-enforced minimum
    on top of that (board posts; see tx.board_fee_floor), applied after the
    congestion-based fee so it can only raise it, never lower it below what
    tx.validate requires.
    """
    draft = {"from": addr, "pubkey": pk_hex, "outputs": outputs,
             "nonce": acct["nonce"] + 1, "fee": 0}
    if memo:
        draft["memo"] = memo
    size = tx_mod.tx_size(draft)
    rate = acct["fees"]["next_block"]
    return max(floor, math.ceil(rate * size))


def submit_and_alert(reader, signer, outputs, passphrase, ctx, memo="", floor=0, acct=None):
    """Sign and send. `acct` is a reader.account(addr, fees=True, fresh=True)
    the caller already asked for, to be used as it is: every question put to
    a node is a round trip, and a caller that needed the answer to decide
    whether to go on should not make the signing ask it again."""
    if not passphrase:
        ctx["alert_err"] = "Passphrase required."
        return
    try:
        # fresh: the nonce and fee rate go into a signature, so they come
        # from the node now, not from an answer held a few seconds ago.
        acct = acct or reader.account(signer.addr, fees=True, fresh=True)
        fee = auto_fee(acct, signer.addr, signer.pk_hex, outputs, memo=memo, floor=floor)
        t = signer.sign(outputs, fee, memo, passphrase, acct["nonce"] + 1)
        ok, result = reader.submit(t)
        if ok:
            ctx["alert_ok_tx"]   = result
            ctx["alert_ok_verb"] = "Submitted."
        else:
            ctx["alert_err"] = f"Error: {_reword_insufficient_balance(result)}"
    except Exception as e:
        log.warning("[wallet] tx build/submit failed  err=%s", e)
        ctx["alert_err"] = f"Error: {e}"


_REF_RE = re.compile(r"[0-9a-f]{%d}" % REPLY_REF_LEN)


def edit_text(ref, orig, new):
    """(text, None) for the edit post that turns `orig` into `new` in the
    post with reference `ref`, or (None, why not). The splice is worked
    out here, from the two texts, so the page that asks for an edit and
    the rule readers apply to it cannot disagree about what it means."""
    if not _REF_RE.fullmatch(ref or ""):
        return None, "Bad edit request."
    if new == orig:
        return None, "Nothing changed."
    if not new.strip():
        return None, "An edit cannot leave a post empty. Delete it instead."
    if len(new.encode("utf-8")) > MAX_POST_TEXT_BYTES:
        return None, f"A post can hold {MAX_POST_TEXT_BYTES} bytes at most."
    pos, ndel, inserted = make_splice(orig, new)
    if apply_splice(orig, pos, ndel, inserted) != new:
        return None, "That edit cannot be applied."
    return build_board_edit(ref, pos, ndel, inserted), None


class WalletSigner:
    """What the routes sign through, for a bare Wallet: the light client,
    which has no node to build the transaction for it. The full node's
    equivalent is api._NodeSigner, which hands the job to
    Node.build_and_sign_tx."""

    def __init__(self, wallet):
        self.wallet = wallet
        self.addr = wallet.addr
        self.pk_hex = wallet.pk_hex

    def sign(self, outputs, fee, memo, passphrase, nonce):
        t, _fee = self.wallet.sign_tx(outputs, nonce, fee, memo=memo,
                                      passphrase=passphrase)
        return t


def _insufficient(acct, required):
    if required > acct["balance"]:
        return (f"Insufficient balance: have {fmt_balance(acct['balance'])}, "
                f"need {fmt_balance(required)}.")
    return None


# ---------------------------------------------------------------------------
# Data endpoints: what a node serves so another node's wallet can read it.
# ---------------------------------------------------------------------------

def _json(data):
    """JSON, gzipped when the client accepts it and the body is worth it.
    A light client may be on a very small data allowance, and this text
    compresses to a fraction of its size."""
    body = json.dumps(data, separators=(",", ":")).encode()
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Vary"] = "Accept-Encoding"
    if len(body) > 512 and "gzip" in request.headers.get("Accept-Encoding", ""):
        resp.set_data(gzip.compress(body, 6))
        resp.headers["Content-Encoding"] = "gzip"
    return resp


def register_data_api(app, reader, pfx, full=True):
    """The read endpoints a light client calls. `full` is the node serving
    them; a light client's own app only needs /api/fees, for its pages'
    scripts to poll."""

    @app.route("/api/fees", endpoint=pfx + "api_fees")
    def api_fees():
        """The fee-market picture the send page renders at load, for it to
        poll and stay live: the suggested rate is only true for as long as
        the mempool doesn't change, and it changes constantly."""
        return jsonify(reader.fee_estimate())

    if not full:
        return

    @app.route("/api/state", endpoint=pfx + "api_state")
    def api_state():
        """reader.account() over HTTP: balance, nonce, the board fee floor,
        and optionally the fee market and a nickname's owner, in one
        answer."""
        addr = request.args.get("addr") or None
        if addr is not None and not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        nick = (request.args.get("nick") or "")[:16] or None
        return _json(reader.account(addr, nick=nick, fees=bool(request.args.get("fees"))))

    @app.route("/api/address/<addr>/page", endpoint=pfx + "api_address_page")
    def api_address_page(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        return _json(reader.address_page(addr, page))

    @app.route("/api/board/page", endpoint=pfx + "api_board_page")
    def api_board_page():
        """The board as board_view.board_page_data builds it. Unchanged
        since the caller's last request costs a 304 and no body."""
        chunks = min(max(request.args.get("chunks", 1, type=int) or 1, 1), 50)
        etag = reader.board_etag(chunks)
        if request.headers.get("If-None-Match") == f'"{etag}"':
            resp = make_response("", 304)
        else:
            data = reader.board_page(chunks)
            resp = _json(data)
            etag = data["etag"]
        resp.headers["ETag"] = f'"{etag}"'
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def address_ctx(reader, addr, page):
    """The part of the Balance page that depends on a looked-up address."""
    d = reader.address_page(addr, page)
    page, total_pages = d["page"], d["total_pages"]
    return dict(balance=d["balance"], tx_count=d["tx_count"],
                page=page, total_pages=total_pages,
                page_window=_pagination_window(page, total_pages),
                history=[tuple(r) for r in d["rows"]],
                has_prev=page > 1, has_next=page < total_pages)


def register_address_page(app, reader, pfx, histogram=None, default_addr=None):
    """GET /address: look up any address, or a board nickname.

    `histogram`, when given, returns (holder_count, rows, max_count) for
    the wealth distribution shown before a lookup runs; only a full node
    has the balances to compute it. `default_addr`, when given, supplies
    the address to show when none was asked for: the light client's own.
    """

    @app.route("/address", methods=["GET", "POST"], endpoint=pfx + "address_lookup")
    def address_lookup():
        addr = request.args.get("addr", "").strip()
        if not addr and default_addr is not None:
            addr = default_addr()
        # "burn" is a lot easier to type than the real twelve-word address,
        # and the real one isn't a secret, it's the wordlist's own first
        # ADDRESS_WORD_COUNT entries (see crypto.burn_address). Redirecting
        # to it rather than silently substituting it keeps the URL itself
        # the actual address, bookmarkable and shareable like any other
        # lookup, not a special case that only works when typed as "burn".
        if addr.lower() == "burn":
            query = request.args.to_dict(flat=True)
            query["addr"] = crypto_mod.burn_address()
            return redirect(f"/address?{urlencode(query)}")
        # Same redirect-to-the-real-address pattern as "burn" above: a
        # board nickname is looked up the same deterministic way every
        # node already resolves one for display (see _nickname_owned_by),
        # so typing a name here lands on exactly the address that name
        # actually belongs to, not a second, separate notion of identity.
        # A nickname is at most 16 characters, so anything longer is not
        # worth asking a node about.
        if addr and not crypto_mod.is_valid_address(addr) and len(addr) <= 16:
            owner = reader.account(None, nick=addr).get("nick_owner")
            if owner is not None:
                query = request.args.to_dict(flat=True)
                query["addr"] = owner
                return redirect(f"/address?{urlencode(query)}")
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        # The distribution histogram is only shown before a lookup runs, so
        # skip computing it once an address has actually been submitted.
        holder_count, rows, histogram_max = 0, [], 0
        if not addr and histogram is not None:
            holder_count, rows, histogram_max = histogram()
        ctx = dict(title="Balance", addr=addr, alert_err="", page=page,
                   history=None, balance=0, tx_count=0, has_prev=False, has_next=False,
                   holder_count=holder_count,
                   histogram=rows, histogram_max=histogram_max)
        if addr and not crypto_mod.is_valid_address(addr):
            ctx["alert_err"] = "Invalid address format."
            ctx["addr"] = ""
        elif addr:
            ctx.update(address_ctx(reader, addr, page))
        return render_template("address.html", **ctx)


def register_board_pages(app, reader, pfx, own_addr_fn, csrf_token=None):
    """GET /board and its live-refresh fragment, for every app that shows
    the board. own_addr_fn is the caller's call (see board_view.board_ctx)."""

    @app.route("/board", endpoint=pfx + "board")
    def board():
        own = own_addr_fn()
        # Nothing here names the viewer: a node asked for the board learns
        # no address. The compose box's own icon comes from the board itself.
        extra = dict(csrf_token=csrf_token, compose_err="", compose_ok="",
                     message_value="", form_token="")
        if csrf_token:   # private app only: there is a compose box
            forms = forms_for(app)
            # This page's compose form works once (see forms.py), and what
            # the post that brought us here has to say, shown once.
            extra["form_token"] = forms.tokens.issue()
            note = forms.notes.take(request.args.get("note"))
            extra.update({k: v for k, v in note.items() if k in ("compose_err", "message_value")})
        page = request.args.get("page", 1, type=int) or 1
        ctx = board_ctx(reader, page, own, extra)
        # What a post must pay at least follows from how many there are, so
        # it is worked out here rather than asked of a node: the page is one
        # request, or none while it is held.
        ctx["board_fee_floor"] = tx_mod.board_fee_floor(ctx["post_count"])
        return render_template("board.html", **ctx)

    @app.route("/api/board/fragment", endpoint=pfx + "api_board_fragment")
    def api_board_fragment():
        """Return live board rows without replacing the compose box."""
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        own = own_addr_fn()
        # Unchanged since the viewer's last poll: answer 304 without
        # rendering anything. Validators are the board state itself plus the
        # window size and viewer, the only inputs the fragment depends on.
        etag = '"' + hashlib.sha1(
            repr((reader.board_etag(page), page, own)).encode()).hexdigest() + '"'
        if request.headers.get("If-None-Match") == etag:
            resp = make_response("", 304)
        else:
            resp = make_response(render_template(
                "board_rows.html", **board_ctx(reader, page, own)))
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def register_wallet_routes(app, reader, signer, csrf_token, xlm=None):
    """Send, post, vote and the fee quotes behind them: everything that
    spends from `signer`'s address. `xlm`, when given, adds the XLM half of the send
    page (see api._XlmSend); the light client has none."""

    def _csrf_ok():
        return secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token)

    # What a send POST leaves for the page it redirects to (see forms.py).
    send_note_keys = ("alert_ok_tx", "alert_ok_verb", "alert_err", "alert_err_lines",
                      "outputs_value", "memo_value", "asset", "xlm_to_value",
                      "xlm_amount_value")

    def _send_post():
        """Do what the send form asks, and answer with a redirect to the
        page that shows how it went. Never with a page: a page that is the
        answer to a POST sends the payment again when it is reloaded."""
        forms = forms_for(app)
        result = dict(alert_ok_tx="", alert_ok_verb="", alert_err="", alert_err_lines=[],
                      outputs_value="", memo_value="", asset="lapse",
                      xlm_to_value="", xlm_amount_value="")
        if not _csrf_ok():
            result["alert_err"] = "Session expired; reload the page and try again."
            return forms.done("/send", result)
        if not forms.tokens.consume(request.form.get("form_token")):
            result["alert_err"] = ALREADY_SENT
            return forms.done("/send", result)

        asset = request.form.get("asset", "lapse")
        passphrase = request.form.get("passphrase", "").strip()
        if asset == "xlm" and xlm is not None:
            result["asset"] = "xlm"
            xlm.send(request.form, passphrase, result)
        else:
            outputs_raw = request.form.get("outputs", "").strip()
            memo        = request.form.get("memo", "").strip()
            csv_file    = request.files.get("csv_file")
            if csv_file and csv_file.filename:
                outputs_raw = csv_file.read().decode()
            result["outputs_value"] = outputs_raw
            result["memo_value"] = memo
            outputs, errors = _parse_csv_outputs(outputs_raw)
            if len(memo.encode("utf-8")) > tx_mod.MAX_MEMO_BYTES:
                errors.append(f"Memo exceeds {tx_mod.MAX_MEMO_BYTES} bytes.")
            if errors:
                result["alert_err_lines"] = errors
            elif not outputs:
                result["alert_err"] = "No valid outputs."
            else:
                submit_and_alert(reader, signer, outputs, passphrase, result, memo=memo)
                if result["alert_ok_tx"]:
                    result["alert_ok_verb"] = "Sent."
                    result["outputs_value"] = ""
                    result["memo_value"] = ""
        return forms.done("/send", {k: result[k] for k in send_note_keys if k in result})

    @app.route("/send", methods=["GET", "POST"], endpoint="send")
    def send():
        if request.method == "POST":
            return _send_post()
        forms = forms_for(app)
        acct = reader.account(signer.addr, fees=True)
        ctx = dict(title="Send", from_addr=signer.addr,
                   balance=acct["balance"], fees=acct["fees"],
                   csrf_token=csrf_token, form_token=forms.tokens.issue(),
                   outputs_value="", memo_value="", memo_max_bytes=tx_mod.MAX_MEMO_BYTES,
                   asset="lapse", xlm_enabled=xlm is not None,
                   alert_ok_tx="", alert_ok_verb="", alert_err="", alert_err_lines=[])
        if xlm is not None:
            ctx.update(xlm.view())
        note = forms.notes.take(request.args.get("note"))
        ctx.update({k: v for k, v in note.items() if k in send_note_keys})
        return render_template("send.html", **ctx)

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
        acct = reader.account(signer.addr, fees=True)
        fee = auto_fee(acct, signer.addr, signer.pk_hex, outputs, memo=memo)
        total_out = sum(o["amount"] for o in outputs)
        short = _insufficient(acct, total_out + fee)
        if short:
            return jsonify({"ok": False, "fee": fee, "reason": short})
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
        # Profile/reply headers are opt-in and priced exactly like the rest
        # of the memo (more bytes -> more fee), so the live estimate has to
        # account for whichever of them this particular post will actually
        # carry, not just the free-text part.
        icon = request.args.get("icon", type=int)
        nick = (request.args.get("nick") or "")[:16] or None
        reply_ref = request.args.get("reply_ref") or None
        edit_ref = request.args.get("edit_ref") or None
        acct = reader.account(signer.addr, nick=nick, fees=True)
        # Checked before quoting a fee, not just before display: paying to
        # set a name that's already someone else's is a real cost for a
        # change that would then silently never show, so the compose box
        # needs to know before the user signs anything, not after.
        owner = acct.get("nick_owner")
        if nick and owner is not None and owner != signer.addr:
            return jsonify({"fee": 0, "floor": 0, "ok": False, "nick_taken": True,
                            "reason": f"'{nick}' is already taken."})
        if edit_ref:
            # An edit is priced on what it will really send, the splice
            # and not the whole text, so it is worked out from the texts.
            text, why = edit_text(edit_ref, request.args.get("orig", ""),
                                  request.args.get("new", ""))
            if why:
                return jsonify({"fee": 0, "floor": 0, "ok": False, "reason": why})
            memo = BOARD_MEMO_TAG + build_board_body(text, icon=icon, nick=nick)
        else:
            header = build_board_body("", icon=icon, nick=nick, reply_ref=reply_ref)
            # An ASCII placeholder of the same byte length: close enough for
            # an estimate, and the real message never leaves the browser
            # until actually posted.
            memo = BOARD_MEMO_TAG + header + ("x" * msg_bytes)
        floor = acct["board_floor"]
        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        fee = auto_fee(acct, signer.addr, signer.pk_hex, outputs, memo=memo, floor=floor)
        short = _insufficient(acct, BOARD_POST_AMOUNT + fee)
        if short:
            return jsonify({"fee": fee, "floor": floor, "ok": False, "reason": short})
        return jsonify({"fee": fee, "floor": floor, "ok": True, "reason": ""})


    @app.route("/api/board/preview", methods=["POST"], endpoint="api_board_preview")
    def api_board_preview():
        """Renders exactly what board_post's own memo will render as, via
        the same render_board_text() every already-posted row goes
        through. A client-side reimplementation of that regex subset
        would drift from it eventually, this way "Preview" is never able
        to show something the real post won't.
        """
        text = request.form.get("text", "")[:tx_mod.MAX_MEMO_BYTES]
        return jsonify({"html": str(render_board_text(text))})

    @app.route("/board", methods=["POST"], endpoint="board_post")
    def board_post():
        forms = forms_for(app)
        page = request.args.get("page", 1, type=int) or 1
        message    = request.form.get("message", "").strip()
        passphrase = request.form.get("passphrase", "").strip()
        in_place = request.headers.get("X-Requested-With") == "fetch"

        def reply(error=None, keep_text=True):
            """The answer. To the page's own script, which sends in the
            background and updates in place, just the verdict and the next
            token: nothing to reload. To a browser without it, a redirect,
            never a page (see forms.py), the text typed brought back into
            the compose box so it is not lost."""
            if in_place:
                return jsonify(ok=error is None, error=error or "",
                               form_token=forms.tokens.issue())
            if error is None:
                return forms.done("/board")
            return forms.done("/board", {"compose_err": error,
                                         "message_value": message if keep_text else ""},
                              **({"page": page} if page != 1 else {}))

        def fail(msg, keep_text=True):
            return reply(msg, keep_text)

        if not _csrf_ok():
            return fail("Session expired; reload the page and try again.")
        if not forms.tokens.consume(request.form.get("form_token")):
            # Not brought back into the box: it was sent already, or may
            # have been, and putting it there invites sending it twice.
            return fail(ALREADY_SENT, keep_text=False)
        # icon/nick only ride along when the compose form's own "profile
        # changed" checkbox says so (see board.html): otherwise this post
        # costs exactly what it would with no profile feature at all.
        icon = request.form.get("icon", type=int) if request.form.get("profile_changed") else None
        nick = (request.form.get("nick") or "").strip()[:16] or None if request.form.get("profile_changed") else None
        if request.form.get("profile_changed") and icon is None:
            icon = 0
        reply_ref = request.form.get("reply_ref") or None
        edit_ref = request.form.get("edit_ref") or None
        edit_orig = request.form.get("edit_orig", "")
        # The floor is read fresh on every submit: it only moves when a
        # block confirms (see tx.board_fee_floor), so this is always the
        # same value tx.validate() will check the resulting tx against,
        # modulo a block landing in between, which is exactly the rare
        # "resubmit at the new floor" case that staircase is designed for.
        acct = reader.account(signer.addr, nick=nick, fees=True, fresh=True)
        floor = acct["board_floor"]

        if not message:
            return fail("Write something to post.")
        # Refused outright, not just silently dropped at display time: a
        # tx claiming a taken name now also fails tx.validate() itself
        # (_check_nickname_available), but catching it here first still
        # saves the round trip to a node that would only reject it anyway.
        owner = acct.get("nick_owner")
        if nick and owner is not None and owner != signer.addr:
            return fail(f"'{nick}' is already taken.")
        if edit_ref:
            # Posted as the edit it is: a splice against the text being
            # edited, which costs what any post costs.
            message, why = edit_text(edit_ref, edit_orig, message)
            if why:
                return fail(why)
            reply_ref = None
        body = build_board_body(message, icon=icon, nick=nick, reply_ref=reply_ref)
        memo = BOARD_MEMO_TAG + body
        over = len(memo.encode("utf-8")) - tx_mod.MAX_MEMO_BYTES
        if over > 0:
            return fail(f"{over} byte{'s' if over != 1 else ''} too long.")

        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        alert_ctx = {}
        submit_and_alert(reader, signer, outputs, passphrase, alert_ctx, memo=memo,
                         floor=floor, acct=acct)
        if alert_ctx.get("alert_err"):
            return fail(alert_ctx["alert_err"])

        # Page 1 regardless of which page the form was on: that's the page
        # pending posts appear on (see board_ctx), and the one this post
        # itself now shows up in, right above the compose box, so posting is
        # its own confirmation. No separate "waiting to be mined" banner
        # needed, the row is one.
        return reply()

    def _quote(memo):
        """What sending `memo` as a plain 1-tick burn would cost right now:
        real coins leave the wallet for it, so the page shows the price
        before it asks for the passphrase, not after."""
        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        acct = reader.account(signer.addr, fees=True)
        fee = auto_fee(acct, signer.addr, signer.pk_hex, outputs, memo=memo)
        short = _insufficient(acct, BOARD_POST_AMOUNT + fee)
        if short:
            return jsonify({"fee": fee, "ok": False, "reason": short})
        return jsonify({"fee": fee, "ok": True, "reason": ""})

    def _ref_tx(make_memo):
        """Votes and deletes: an ordinary, non-board-tagged transaction
        (tx.is_board_post never matches it), so it never advances
        state.total_board_posts and never pays the board fee floor, only
        the same congestion fee any other send would, plus the same 1-tick
        burn a board post makes. It carries nothing but a tag and the 6-hex
        reference to the post it is about. make_memo(form) returns the
        memo to send, or None for a request that is not well formed.

        Answered with a verdict, {"ok": bool, "error": text}: the page's own
        script sends these in the background and has no use for a page."""
        passphrase = request.form.get("passphrase", "").strip()
        if not _csrf_ok():
            return jsonify(ok=False, error="Session expired; reload the page and try again.")
        memo = make_memo(request.form)
        if memo is None:
            return jsonify(ok=False, error="Bad request.")
        outputs = [{"to": crypto_mod.burn_address(), "amount": BOARD_POST_AMOUNT}]
        alert_ctx = {}
        submit_and_alert(reader, signer, outputs, passphrase, alert_ctx, memo=memo)
        if alert_ctx.get("alert_err"):
            return jsonify(ok=False, error=alert_ctx["alert_err"])
        return jsonify(ok=True, error="")

    def _ref(source):
        return (source.get("ref") or "")[:REPLY_REF_LEN]

    @app.route("/api/board/vote_fee", endpoint="api_board_vote_fee")
    def api_board_vote_fee():
        tag = VOTE_UP_TAG if request.args.get("dir") == "+" else VOTE_DOWN_TAG
        return _quote(tag + _ref(request.args))

    @app.route("/api/board/delete_fee", endpoint="api_board_delete_fee")
    def api_board_delete_fee():
        return _quote(DELETE_TAG + _ref(request.args))

    @app.route("/board/vote", methods=["POST"], endpoint="board_vote")
    def board_vote():
        def memo(form):
            ref, direction = _ref(form), form.get("dir")
            if direction not in ("+", "-") or len(ref) != REPLY_REF_LEN:
                return None
            return (VOTE_UP_TAG if direction == "+" else VOTE_DOWN_TAG) + ref
        return _ref_tx(memo)

    @app.route("/board/delete", methods=["POST"], endpoint="board_delete")
    def board_delete():
        """Hide one of your own posts from readers. Nothing is removed from
        the chain, and readers only honor it for a post by the same sender
        (see board_view.resolve_board), so deleting someone else's post
        sends a transaction that changes nothing."""
        def memo(form):
            ref = _ref(form)
            return DELETE_TAG + ref if _REF_RE.fullmatch(ref) else None
        return _ref_tx(memo)
