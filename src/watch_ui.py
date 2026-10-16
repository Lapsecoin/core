"""What a node that holds no key shows for the pages that need one.

A node given just an address (wallet.WatchWallet) mines and builds blocks as
that address and cannot sign anything. The pages that exist to sign (Send, the
fee request form, board posts, votes and deletes, the address actions) say so
instead of asking for a passphrase that would open nothing. Everything else on
the private port works as it does for any node."""

from flask import render_template, request

# Endpoints whose whole job is to sign a transaction.
SIGNING_ENDPOINTS = frozenset({
    "send", "fees", "fees_submit",
    "board_post", "board_delete", "board_vote",
    "addresses_new", "addresses_label", "addresses_export",
})


def register(app, node):
    @app.before_request
    def watch_only_gate():
        if getattr(node, "can_sign", True):
            return None
        if (request.endpoint or "") not in SIGNING_ENDPOINTS:
            return None
        status = 200 if request.method == "GET" else 403
        return render_template("watch_only.html", title="No key", address=node.addr), status
