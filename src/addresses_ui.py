"""The Addresses page: every account of the node's recovery-phrase wallet,
with its balance, a way to add the next one, and a way to export one
account's key for another machine.

Private port only, like Send. Nothing here shows the recovery phrase, and no
route returns it: the phrase is shown once, in the window or terminal that
created the wallet. The passphrase is asked for by every action that opens the
wallet, as Send and the board do."""

import logging
import secrets

from flask import jsonify, render_template, request

import forms as forms_mod
from walletfile import WalletFileError

log = logging.getLogger("ec.addresses")

MIN_PASSPHRASE = 8
WRONG_PASSPHRASE = "That is not this wallet's passphrase."


def register(app, reader, node, csrf_token):

    def _csrf_ok():
        return secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token)

    def _wallet_file():
        return getattr(getattr(node, "wallet", None), "walletfile", None)

    def _rows():
        active = getattr(node.wallet, "account", 0)
        rows = []
        for a in node.accounts():
            try:
                balance = reader.account(a["address"])["balance"]
            except Exception:           # an unreadable balance must not hide the list
                balance = None
            rows.append({**a, "balance": balance, "active": a["index"] == active})
        return rows

    def _done(payload=None):
        return forms_mod.forms_for(app).done("/addresses", payload)

    def _checked():
        """The error to answer with if this POST should not go ahead, else
        None. Spends the form's one-time token."""
        if not _csrf_ok():
            return "Session expired; reload the page and try again."
        if not forms_mod.forms_for(app).tokens.consume(request.form.get("form_token")):
            return forms_mod.ALREADY_SENT
        return None

    def _open(wf, passphrase):
        """The wallet's key-encryption key, or an error message."""
        if not passphrase:
            return None, "Passphrase required."
        try:
            kek = node.wallet.derive_kek(passphrase)
            wf.phrase(kek)              # a wrong passphrase fails here
            return kek, None
        except (ValueError, WalletFileError):
            return None, WRONG_PASSPHRASE

    @app.route("/addresses", endpoint="addresses")
    def addresses():
        f = forms_mod.forms_for(app)
        wf = _wallet_file()
        ctx = dict(title="Addresses", csrf_token=csrf_token,
                   form_token=f.tokens.issue(), rows=_rows(),
                   has_phrase=wf is not None, min_passphrase=MIN_PASSPHRASE,
                   alert_ok="", alert_err="", exported=None)
        note = f.notes.take(request.args.get("note"))
        ctx.update({k: v for k, v in note.items() if k in ("alert_ok", "alert_err", "exported")})
        return render_template("addresses.html", **ctx)

    @app.route("/addresses/new", methods=["POST"], endpoint="addresses_new")
    def addresses_new():
        bad = _checked()
        if bad:
            return _done({"alert_err": bad})
        wf = _wallet_file()
        if wf is None:
            return _done({"alert_err": "This node runs on a single key; it has no recovery phrase to add addresses to."})
        kek, why = _open(wf, request.form.get("passphrase", "").strip())
        if why:
            return _done({"alert_err": why})
        try:
            entry = wf.new_account(kek)
            label = request.form.get("label", "").strip()
            if label:
                wf.set_label(entry["index"], label)
        except (OSError, ValueError, WalletFileError) as e:
            log.warning("[wallet] could not add an address: %s", e)
            return _done({"alert_err": f"Could not add an address: {e}"})
        named = f" named {label}" if label else ""
        return _done({"alert_ok": f"Added address {entry['index']}{named}: {entry['address']}"})

    @app.route("/addresses/label", methods=["POST"], endpoint="addresses_label")
    def addresses_label():
        # A name moves no funds and can be set again, so it needs the page's
        # token but not a one-time form token.
        if not _csrf_ok():
            return _done({"alert_err": "Session expired; reload the page and try again."})
        wf = _wallet_file()
        index = request.form.get("account", type=int)
        if wf is None or index is None:
            return _done({"alert_err": "Nothing to rename."})
        try:
            wf.set_label(index, request.form.get("label", "").strip())
        except (KeyError, OSError):
            return _done({"alert_err": "That address is not in this wallet."})
        return _done()

    @app.route("/addresses/export", methods=["POST"], endpoint="addresses_export")
    def addresses_export():
        bad = _checked()
        if bad:
            return _done({"alert_err": bad})
        wf = _wallet_file()
        if wf is None:
            return _done({"alert_err": "This node runs on a single key; there is nothing to export from."})
        passphrase = request.form.get("passphrase", "").strip()
        new_pass = request.form.get("new_passphrase", "").strip()
        index = request.form.get("account", type=int)
        if index is None:
            return _done({"alert_err": "Pick an address to export."})
        if len(new_pass) < MIN_PASSPHRASE:
            return _done({"alert_err": f"The key's passphrase must be at least {MIN_PASSPHRASE} characters."})
        if new_pass == passphrase:
            return _done({"alert_err": "Choose a different passphrase for the key than the wallet's, "
                                       "so the other machine never holds it."})
        kek, why = _open(wf, passphrase)
        if why:
            return _done({"alert_err": why})
        try:
            text = wf.export_account_key(kek, index, new_pass, passphrase)
            address = wf.address(index)
        except (KeyError, ValueError) as e:
            return _done({"alert_err": "That address is not in this wallet." if isinstance(e, KeyError)
                          else str(e)})
        return _done({"exported": {"index": index, "address": address, "key": text}})

    @app.route("/api/wallet/accounts", endpoint="api_wallet_accounts")
    def api_wallet_accounts():
        """The accounts and their balances. Public facts only."""
        return jsonify([{"index": r["index"], "address": r["address"], "label": r["label"],
                         "balance": r["balance"], "active": r["active"]} for r in _rows()])
