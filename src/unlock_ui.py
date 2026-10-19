"""The lock page of a node that started without its passphrase.

The node runs as soon as it starts (see wallet.open_locked). Until the
passphrase is typed here, every private page leads to this one, and what needs
the key (answering gas requests with the Base wallet, finishing a wallet move
or an address scan) waits. After it, the pages open as usual and nothing asks
again except the forms that already do.

Private port only, like Send."""

import logging
import os
import secrets
from urllib.parse import quote, urlparse

from flask import jsonify, redirect, render_template, request

import base_wallet
import evm
import wallet as wallet_mod

log = logging.getLogger("ec.unlock")

WRONG_PASSPHRASE = "That is not this node's passphrase."
# Endpoints a locked node still serves: this page and what it needs to draw.
_ALWAYS_OPEN = ("css", "favicon", "icon_png", "icon_png_sized")


def _base_wallet_holds_funds(node):
    """Whether the Base wallet has something in it. False when it is empty or
    the balance cannot be read: a line about it is only worth showing when
    answering gas requests is something this node will actually do."""
    path = getattr(node, "base_wallet_path", None)
    addr = base_wallet.load_address(path) if path else None
    if not addr:
        return False
    try:
        return evm.get_balance_wei(evm.DEFAULT_BASE_RPC, addr) > 0
    except evm.EVMError:
        return False


def pending_work(node, check_balance=False):
    """What the passphrase will switch on, in plain words, for this node's
    state. Reading the Base balance goes to the network, so it is only done
    when asked to; the page draws without it and asks for it after."""
    items = []
    if check_balance and _base_wallet_holds_funds(node):
        items.append("Answer gas requests from your Base wallet")
    wf = getattr(getattr(node, "wallet", None), "walletfile", None)
    if wf is not None:
        if wf.pending_sweep or os.path.exists(wallet_mod.LEGACY_KEY_FILE):
            items.append("Finish moving your old key into this wallet")
        if wf.scan_pending:
            items.append("Finish finding your other addresses from your recovery phrase")
    return items


def _local_path(text):
    """Where to go after unlocking: a path on this node, never another site."""
    if (text.startswith("/") and not text.startswith("//") and "\\" not in text
            and text.isprintable()):
        parts = urlparse(text)
        if not parts.scheme and not parts.netloc:
            return text
    return "/"


def register(app, node, csrf_token, limiter):

    def _locked():
        return bool(getattr(node, "needs_unlock", False))

    @app.before_request
    def gate():
        if not _locked():
            return None
        endpoint = request.endpoint or ""
        if endpoint in ("unlock_page", "unlock_submit", "unlock_work") or endpoint.removeprefix("priv_") in _ALWAYS_OPEN:
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="locked"), 423
        target = request.full_path.rstrip("?") if request.method == "GET" else "/"
        return redirect("/unlock?next=" + quote(target, safe="/"))

    def _page(next_path, error=None, status=200):
        return render_template("unlock.html", title="Unlock", lock_screen=True,
                               csrf_token=csrf_token, work=pending_work(node),
                               next_path=next_path, alert_err=error), status

    @app.route("/unlock", endpoint="unlock_page")
    def unlock_page():
        if not _locked():
            return redirect(_local_path(request.args.get("next", "/")))
        return _page(_local_path(request.args.get("next", "/")))

    @app.route("/unlock/work", endpoint="unlock_work")
    def unlock_work():
        if not _locked():
            return jsonify(items=[])
        return jsonify(items=pending_work(node, check_balance=True))

    @app.route("/unlock", methods=["POST"], endpoint="unlock_submit")
    @limiter.limit("10 per minute")
    def unlock_submit():
        next_path = _local_path(request.form.get("next", "/"))
        if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token):
            return _page(next_path, "Session expired; reload the page and try again.", 403)
        if not _locked():
            return redirect(next_path)
        passphrase = request.form.get("passphrase", "").strip()
        if not passphrase:
            return _page(next_path, "Enter your passphrase.", 400)
        try:
            node.unlock_with_passphrase(passphrase)
        except (ValueError, RuntimeError):
            return _page(next_path, WRONG_PASSPHRASE, 400)
        log.info("[startup] unlocked")
        return redirect(next_path)
