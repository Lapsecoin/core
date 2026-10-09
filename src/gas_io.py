"""Reading other networks for fee requests: prices, gas prices, balances and
the sanctions oracle. Read-only and node-free, so the full node's pages, its
worker and the light client's page all ask the world the same way, and tests
swap one object for a fake.
"""

import evm
import gas
import relay
import sanctions


class ChainIO:
    def __init__(self, proxy=None):
        self.proxy = proxy          # callable returning a proxy URL or None (the light client's Tor)

    def _route(self):
        """Send this call the way the light client sends its own: through its
        proxy when it has one, so reading another network does not name the
        user's address outside Tor."""
        url = self.proxy() if self.proxy else None
        proxies = {"http": url, "https": url} if url else {}
        evm._session.proxies = proxies
        relay._session.proxies = proxies

    def _setting(self, setting):
        return self.settings.get(setting).strip() if self.settings is not None else ""

    def base_rpc(self):
        return evm.DEFAULT_BASE_RPC

    def relay_key(self):
        return ""

    def _rpc_for(self, net):
        return self.base_rpc() if net.slug == "base" else net.rpc

    def price(self, net):
        self._route()
        return relay.price_usd(net, self.relay_key())

    def gas_price(self, net):
        """Wei per gas now (0 on Solana, where the need is a fixed amount)."""
        if net.vm == "svm":
            return 0
        self._route()
        return evm.gas_price_wei(self._rpc_for(net))

    def dest_balance(self, net, addr):
        self._route()
        if net.vm == "evm":
            return evm.get_balance_wei(self._rpc_for(net), addr)
        resp = evm.rpc(net.rpc, "getBalance", [addr])
        return int(resp["value"]) if isinstance(resp, dict) else int(resp)

    def sanctioned(self, net, addr):
        """Whether the destination is on a sanctions list (EVM only)."""
        self._route()
        return sanctions.is_sanctioned(addr) if net.vm == "evm" else False
