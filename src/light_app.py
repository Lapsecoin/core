"""The light client's web app: the wallet pages, served from a RemoteReader.

Everything here is registration. The pages themselves are the full node's
own (wallet_ui.py), handed a reader that asks another node over HTTP and a
signer around a bare Wallet. Nothing about the VDF, the chain or the swap
code is imported, and a test keeps it that way.
"""

import secrets

from flask import jsonify, redirect, render_template, request

from remote_reader import RemoteError
from ui_common import make_flask_app, register_static_routes
from version import LOCAL_VERSION
from wallet_ui import (WalletSigner, register_address_page, register_board_pages,
                       register_data_api, register_wallet_routes)

_NAV = {"address_lookup": "address", "send": "send", "board": "board",
        "board_post": "board", "board_vote": "board", "board_delete": "board"}


def _format_bytes(n):
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1024 ** 2
                                      else f"{n / 1024 ** 2:.2f} MB")


def create_light_app(reader, wallet, port=8335):
    app = make_flask_app(__name__)
    # Per-process CSRF token, for the same reason the full node's private
    # app has one: a local, single-user, 127.0.0.1-only app with no login,
    # where a fixed unguessable token is enough to stop another site's page
    # from posting to it.
    csrf_token = secrets.token_hex(32)

    @app.context_processor
    def inject_ctx():
        usage = reader.usage()
        return {"is_private": True, "light": True,
                "private_port": port, "public_port": port,
                "update_checker": None, "updater": None,
                "csrf_token": csrf_token,
                "nav_active": _NAV.get((request.endpoint or "").split(".")[-1]),
                "local_version": LOCAL_VERSION,
                "live_status": f"{_format_bytes(usage['bytes'])} used this session, "
                               f"{usage['route']}"}

    @app.errorhandler(RemoteError)
    def remote_down(e):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=str(e)), 502
        return render_template("error.html", title="Offline", message=str(e)), 502

    register_static_routes(app)
    register_data_api(app, reader, "", full=False)
    register_address_page(app, reader, "", default_addr=lambda: wallet.addr)
    register_board_pages(app, reader, "", lambda: wallet.addr, csrf_token)
    register_wallet_routes(app, reader, WalletSigner(wallet), csrf_token)

    @app.route("/")
    def home():
        # The board, not the balance: reading it names nobody, while the
        # balance asks a node about this wallet's address. Nothing that
        # does is fetched until the user goes there.
        return redirect("/board")

    return app
