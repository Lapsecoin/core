"""The Fees page: ask other nodes for gas on a chain where you hold a token
but no gas coin.

You pick a network and what you want to do there. The page works out what that
needs from the network's live gas price, tells you plainly what nodes will
send (they cap it) and how much of the need that covers, and has your own
wallet sign a message proving the address is yours. Then it sends an ordinary
transaction that locks a few LAPSE for a few blocks and names the network,
the target balance and the address. Nodes that can help offer on-chain, one
of them pays, and the lock is returned if nobody offered (see gaslock.py).

Everything that reads another chain happens here on the node, not in the
browser: the page only ever talks to its own node.
"""

import base64
import logging
import re
import secrets

from flask import jsonify, redirect, render_template, request

import crypto
import evm
import forms as forms_mod
import gas
import gas_status
import gas_worker
import gaslock
import relay
import settings as settings_mod
import wallet_ui

log = logging.getLogger("ec.fees")

# What the lock costs, shown to the requester. The same constant the nodes
# use to decide whether a request is worth serving.
LOCK = gas_worker.MIN_SERVED_LOCK

# A target far from what the page would work out now is not a stale quote,
# it is a different request.
TARGET_TOLERANCE = (0.5, 2.0)


def network_options():
    return [dict(slug=n.slug, name=n.name, symbol=n.symbol, vm=n.vm)
            for n in gas.NETWORKS.values()]


def action_options():
    return [dict(key=a.key, label=a.label) for a in gas.ACTIONS.values()]


def _gas_price(node, net):
    if net.vm == "svm":
        return 0
    url = (node.settings.get(settings_mod.BASE_RPC_URL).strip() or evm.DEFAULT_BASE_RPC) \
        if net.slug == "base" else net.rpc
    return evm.gas_price_wei(url)


def make_plan(node, io, net, action, dest):
    """The numbers the page shows, from live data. Raises the usual outside
    errors; the routes turn them into a message."""
    price = io.price(net)
    balance = io.dest_balance(net, dest) if dest else 0
    p = gas.plan(net, action, gas_price=_gas_price(node, net), price_usd=price, balance=balance)
    p.update(price_usd=price, balance=balance, symbol=net.symbol, decimals=net.decimals,
             floor_usd=gas.FLOOR_USD, lock=LOCK)
    return p


def decode_signature(net, raw):
    """The wallet's signature as bytes, from hex or base64, or None."""
    raw = (raw or "").strip()
    try:
        if re.fullmatch(r"(0x)?[0-9a-fA-F]+", raw):
            b = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        else:
            b = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    return b if len(b) == (65 if net.vm == "evm" else 64) else None


def _problem(message, status=400):
    return jsonify(ok=False, error=message), status


def register(app, node, reader, signer, csrf_token, io=None):
    """The private app's Fees routes. `io` is injectable for tests."""
    io = io or getattr(node, "gas_io", None) or gas_worker.LiveIO(node)

    def _csrf_ok():
        return secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token)

    def _lookup(args):
        net = gas.network(args.get("network", ""))
        action = gas.ACTIONS.get(args.get("action", ""))
        dest = args.get("dest", "").strip()
        if net is None:
            return None, None, None, "Pick a network."
        if action is None:
            return None, None, None, "Pick what you want to do."
        if dest and not gas.is_valid_address(net, dest):
            return None, None, None, f"That is not a valid {net.name} address."
        return net, action, dest, None

    @app.route("/fees", endpoint="fees")
    def fees():
        f = forms_mod.forms_for(app)
        ctx = dict(title="Fees", csrf_token=csrf_token, form_token=f.tokens.issue(),
                   networks=network_options(), actions=action_options(),
                   lock=LOCK, node_cap_usd=gas.NODE_CAP_USD,
                   alert_err="", alert_ok="", form={})
        ctx.update({k: v for k, v in f.notes.take(request.args.get("note")).items()
                    if k in ("alert_err", "form")})
        return render_template("fees.html", **ctx)

    @app.route("/api/fees/plan", endpoint="api_fees_plan")
    def api_fees_plan():
        net, action, dest, err = _lookup(request.args)
        if err:
            return _problem(err)
        try:
            return jsonify(ok=True, **make_plan(node, io, net, action, dest))
        except (evm.EVMError, relay.RelayError) as e:
            log.info("[fees] plan failed: %s", e)
            return _problem("Could not read the network just now. Try again in a moment.", 502)

    @app.route("/api/fees/prepare", endpoint="api_fees_prepare")
    def api_fees_prepare():
        """The message the destination's wallet signs. Fixed to this sender,
        this nonce and these numbers, so the signature cannot be reused."""
        net, action, dest, err = _lookup(request.args)
        if err or not dest:
            return _problem(err or "Enter the address to fund.")
        try:
            plan = make_plan(node, io, net, action, dest)
        except (evm.EVMError, relay.RelayError):
            return _problem("Could not read the network just now. Try again in a moment.", 502)
        if not plan["needs_help"]:
            return _problem("That address already holds enough gas for this.")
        nonce = reader.account(signer.addr, fresh=True)["nonce"] + 1
        msg = gas.request_message(net.slug, plan["target"], dest, signer.addr, nonce)
        return jsonify(ok=True, message=msg.decode(), target=plan["target"], nonce=nonce)

    @app.route("/fees", methods=["POST"], endpoint="fees_submit")
    def fees_submit():
        f = forms_mod.forms_for(app)
        form = {k: request.form.get(k, "").strip()
                for k in ("network", "action", "dest", "target")}

        def back(message):
            return f.done("/fees", {"alert_err": message, "form": form})

        if not _csrf_ok():
            return back("Session expired; reload the page and try again.")
        if not f.tokens.consume(request.form.get("form_token")):
            return back(forms_mod.ALREADY_SENT)
        net, action, dest, err = _lookup(form)
        if err or not dest:
            return back(err or "Enter the address to fund.")
        passphrase = request.form.get("passphrase", "").strip()
        if not passphrase:
            return back("Passphrase required.")
        sig = decode_signature(net, request.form.get("signature"))
        if sig is None:
            return back("Sign the message with the wallet that holds that address first.")
        try:
            target = int(form["target"])
            plan = make_plan(node, io, net, action, dest)
        except ValueError:
            return back("Prepare the request again.")
        except (evm.EVMError, relay.RelayError):
            return back("Could not read the network just now. Try again in a moment.")
        lo, hi = TARGET_TOLERANCE
        if not plan["needs_help"]:
            return back("That address already holds enough gas for this.")
        if not lo * plan["target"] <= target <= hi * plan["target"]:
            return back("The price moved. Prepare the request again.")
        acct = reader.account(signer.addr, fees=True, fresh=True)
        req = dict(network=net.slug, target=target, dest=dest, signature=sig)
        if not gas.verify_request_signature(req, signer.addr, acct["nonce"] + 1):
            return back("That signature does not match this request. Prepare it again.")
        memo = gas.build_request_memo(net.slug, target, dest, sig)
        outputs = [{"to": crypto.escrow_address(), "amount": LOCK}]
        try:
            fee = wallet_ui.auto_fee(acct, signer.addr, signer.pk_hex, outputs, memo=memo)
            if acct["balance"] < LOCK + fee:
                return back("Not enough LAPSE for the lock and the network fee.")
            t = signer.sign(outputs, fee, memo, passphrase, acct["nonce"] + 1)
            ok, result = reader.submit(t)
        except ValueError as e:
            return back(str(e) or "That is not this node's passphrase.")
        if not ok:
            return back(f"Error: {result}")
        return redirect(f"/explorer/tx/{result}", code=303)


def register_status(app, node, pfx, io=None):
    """The live status of a request, on both apps: it reads the chain and the
    destination's own balance, nothing private."""
    io = io or getattr(node, "gas_io", None) or gas_worker.LiveIO(node)

    @app.route("/api/gas/<txid>", endpoint=pfx + "api_gas_status")
    def api_gas_status(txid):
        return jsonify(status_for(node, io, txid))


def status_for(node, io, txid):
    """The request's status dict, or {"stage": "none"} when txid is not a
    fee request (or is not on the chain yet)."""
    height = node.storage.get_tx_height(txid)
    mempool_tx = None if height is not None else node.mempool.get(txid)
    if height is None:
        stage = "pending" if mempool_tx is not None and gas.parse_request_memo(
            mempool_tx.get("memo")) else "none"
        return {"stage": stage}
    view = node.view

    def dest_funded():
        req = gas.parse_request_memo(_memo_at(view, height, txid))
        net = gas.NETWORKS[req["network"]]
        try:
            return gas.payout_units(net, req["target"],
                                    io.dest_balance(net, req["dest"]), io.price(net)) == 0
        except (evm.EVMError, relay.RelayError):
            return False

    st = gas_status.request_status(view.chain, view.height, txid, height, dest_funded)
    return st or {"stage": "none"}


def _memo_at(view, height, txid):
    import tx as tx_mod
    for t in view.chain[height]["transactions"]:
        if tx_mod.tx_hash(t) == txid:
            return t.get("memo", "")
    return ""
