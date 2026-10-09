"""The ETH-on-Base half of the send page, behind the calls wallet_ui makes:
view() for what the tab shows, preview() for the quote under the form and
send() for its submit. The light client has no gas wallet and passes none.

Sending to a Base address in ETH is a plain transfer. Anything else, another
network or a token, is a swap and bridge through Relay, quoted first.
"""

import logging

import base_wallet
import crypto as crypto_mod
import evm
import gas
import relay
import settings as settings_mod

log = logging.getLogger("ec.base_send")

NATIVE = gas.NETWORKS["base"].currency


class BaseSend:
    def __init__(self, node):
        self.node = node

    def _rpc_url(self):
        return (self.node.settings.get(settings_mod.BASE_RPC_URL).strip()
                or evm.DEFAULT_BASE_RPC)

    def _relay_key(self):
        return self.node.settings.get(settings_mod.RELAY_API_KEY).strip()

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
        order = ["base"] + [s for s in gas.NETWORKS if s != "base"]
        return dict(base_addr=addr or "",
                    base_balance=("" if balance is None else evm.wei_to_str(balance)),
                    base_balance_known=balance is not None,
                    base_networks=[dict(slug=s, name=gas.NETWORKS[s].name) for s in order],
                    base_to_value="", base_amount_value="",
                    base_network_value="base", base_token_value=NATIVE)

    def tokens(self, slug):
        """The tokens that can be sent to on this network, the gas coin first.
        Falls back to the gas coin alone if Relay cannot be reached."""
        net = gas.network(slug)
        if net is None:
            return None
        native = [{"symbol": net.symbol, "address": net.currency, "decimals": net.decimals}]
        try:
            listed = relay.tokens(self._relay_key()).get(net.chain_id, [])
        except relay.RelayError:
            return native
        rest = [t for t in listed if t["address"].lower() != net.currency.lower()]
        return native + rest

    def _parse(self, args):
        """(net, token, dest, wei) or raises ValueError with the message."""
        net = gas.network(args.get("network", ""))
        if net is None:
            raise ValueError("Pick a network.")
        token = next((t for t in self.tokens(net.slug)
                      if t["address"].lower() == args.get("token", "").lower()), None)
        if token is None:
            raise ValueError("Pick a token.")
        dest = args.get("to", "").strip()
        if not dest:
            raise ValueError("Enter a destination address.")
        if not gas.is_valid_address(net, dest):
            raise ValueError(f"That is not a valid {net.name} address.")
        return net, token, dest, evm.str_to_wei(args.get("amount", ""))

    @staticmethod
    def _direct(net, token):
        return net.slug == "base" and token["address"].lower() == NATIVE

    def preview(self, args):
        """What the send would deliver, as a dict for the page, or raises
        ValueError / RelayError / EVMError."""
        net, token, dest, wei = self._parse(args)
        if self._direct(net, token):
            return {"direct": True, "symbol": "ETH", "decimals": 18,
                    "receive": wei, "receive_usd": None, "impact_percent": 0.0}
        sender = base_wallet.load_address(self._path())
        summary = relay.check_exit_quote(
            relay.quote_exit(net, token["address"], dest, wei, sender, self._relay_key()),
            net, token["address"], dest, wei, sender)
        return {"direct": False, **summary}

    def send(self, form, passphrase, ctx):
        args = {"network": form.get("base_network", ""), "token": form.get("base_token", ""),
                "to": form.get("base_to", ""), "amount": form.get("base_amount", "").strip()}
        ctx["base_to_value"] = args["to"].strip()
        ctx["base_amount_value"] = args["amount"]
        ctx["base_network_value"] = args["network"]
        ctx["base_token_value"] = args["token"]
        try:
            net, token, dest, wei = self._parse(args)
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
            if self._direct(net, token):
                tx_hash = base_wallet.send_eth(path, kek, self._rpc_url(), dest, wei)
            else:
                tx_hash = base_wallet.send_exit(path, kek, self._rpc_url(), net,
                                                token["address"], dest, wei, self._relay_key())
        except ValueError as e:
            ctx["alert_err"] = str(e) or "That is not this node's passphrase."
            return
        except (evm.EVMError, relay.RelayError) as e:
            ctx["alert_err"] = f"Error: {e}"
            return
        ctx["alert_ok_tx"] = tx_hash
        ctx["alert_ok_verb"] = "Sent from Base."
        for k in ("base_to_value", "base_amount_value"):
            ctx[k] = ""
