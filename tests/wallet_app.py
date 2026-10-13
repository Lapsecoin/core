"""A bare-wallet web app and a small full node for the page tests.

The wallet and board pages are the full node's own (wallet_ui.py); a node
signs through its private app in production. These tests want the same pages
over a plain Wallet and a node that validates what it is sent, without the
rest of a node, so this registers them directly.
"""

import secrets

from flask import redirect, request

import fees_ui
import mempool as mempool_mod
import settings as settings_mod
import tx as tx_mod
from gas_io import ChainIO
from node import NodeView
from tests.fixtures import address
from tests.test_api import _MemoryMeta
from ui_common import NAV_ACTIVE, make_flask_app, register_static_routes
from version import LOCAL_VERSION
from wallet_ui import (register_address_page, register_board_pages, register_data_api,
                       register_wallet_routes)

PASS = "correct horse battery staple"


class WalletSigner:
    """Signs with a bare Wallet, the way a node's own signer does with its key."""

    def __init__(self, wallet):
        self.wallet = wallet
        self.addr = wallet.addr
        self.pk_hex = wallet.pk_hex

    def sign(self, outputs, fee, memo, passphrase, nonce):
        t, _fee = self.wallet.sign_tx(outputs, nonce, fee, memo=memo,
                                      passphrase=passphrase)
        return t


class _TxIndex:
    """Storage's address index, answered by walking the chain."""

    def __init__(self, cs):
        self.cs = cs

    def get_tx_heights_for_addr(self, addr):
        return [(blk["height"], tx_mod.tx_hash(t))
                for blk in self.cs.chain for t in blk.get("transactions", [])
                if t.get("from") == addr
                or any(o["to"] == addr for o in t.get("outputs", []))]


class _FullNode:
    """A node with real validation on submit, the way Node.submit_tx does it."""

    def __init__(self, cs):
        self.cs = cs
        self.storage = _TxIndex(cs)
        self.mempool = mempool_mod.Mempool()
        self.view = NodeView(cs)
        self.addr = address(0)
        self.settings = settings_mod.Settings(_MemoryMeta())

    def submit_tx_from_api(self, t, timeout=5):
        probe = self.mempool.probe_state_for(t.get("from"), self.cs.state)
        ok, err = tx_mod.validate(t, probe)
        if not ok:
            return False, err
        return self.mempool.add(t)


def create_wallet_app(reader, wallet, port=8335):
    app = make_flask_app(__name__)
    csrf_token = secrets.token_hex(32)

    @app.context_processor
    def inject_ctx():
        return {"is_private": True, "private_port": port, "public_port": port,
                "update_checker": None, "updater": None,
                "csrf_token": csrf_token,
                "nav_active": NAV_ACTIVE.get((request.endpoint or "").split(".")[-1]),
                "local_version": LOCAL_VERSION, "live_status": ""}

    register_static_routes(app)
    register_data_api(app, reader, "")
    register_address_page(app, reader, "", default_addr=lambda: wallet.addr)
    register_board_pages(app, reader, "", lambda: wallet.addr, csrf_token)
    register_wallet_routes(app, reader, WalletSigner(wallet), csrf_token)
    fees_ui.register(app, reader, WalletSigner(wallet), csrf_token, ChainIO())

    @app.route("/")
    def home():
        return redirect("/board")

    return app
