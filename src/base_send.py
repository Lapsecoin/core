"""The ETH-on-Base half of the send page, behind the two calls wallet_ui
makes: view() for what the tab shows and send() for its form. The light
client has no gas wallet and passes none.
"""

import logging

import base_wallet
import crypto as crypto_mod
import evm
import settings as settings_mod

log = logging.getLogger("ec.base_send")


class BaseSend:
    def __init__(self, node):
        self.node = node

    def _rpc_url(self):
        return (self.node.settings.get(settings_mod.BASE_RPC_URL).strip()
                or evm.DEFAULT_BASE_RPC)

    def _path(self):
        return getattr(self.node, "base_wallet_path", None)

    def _balance(self, addr):
        """Wei, or None when the endpoint cannot be reached. A page that
        cannot reach a public API should still render, just without a number
        it cannot get."""
        try:
            return evm.get_balance_wei(self._rpc_url(), addr)
        except evm.EVMError:
            return None

    def view(self):
        path = self._path()
        addr = base_wallet.load_address(path) if path else None
        balance = self._balance(addr) if addr else None
        return dict(base_addr=addr or "",
                    base_balance=("" if balance is None else evm.wei_to_str(balance)),
                    base_balance_known=balance is not None,
                    base_to_value="", base_amount_value="")

    def send(self, form, passphrase, ctx):
        to_addr = form.get("base_to", "").strip()
        amount_raw = form.get("base_amount", "").strip()
        ctx["base_to_value"] = to_addr
        ctx["base_amount_value"] = amount_raw
        if not to_addr:
            ctx["alert_err"] = "Enter a destination address."
            return
        try:
            value_wei = evm.str_to_wei(amount_raw)
        except ValueError as e:
            ctx["alert_err"] = str(e)
            return
        if not passphrase:
            ctx["alert_err"] = "Passphrase required."
            return
        path = self._path()
        if not path or not base_wallet.load_address(path):
            ctx["alert_err"] = "This node has no Base gas wallet yet. Restart the node to create one."
            return
        try:
            kek = crypto_mod.derive_kek(self.node.keyfile, passphrase)
            tx_hash = base_wallet.send_eth(path, kek, self._rpc_url(), to_addr, value_wei)
        except ValueError as e:
            ctx["alert_err"] = str(e) or "That is not this node's passphrase."
            return
        except evm.EVMError as e:
            ctx["alert_err"] = f"Error: {e}"
            return
        ctx["alert_ok_tx"] = tx_hash
        ctx["alert_ok_verb"] = "Sent on Base."
        ctx["base_to_value"] = ""
        ctx["base_amount_value"] = ""
