"""Sanctions screening for fee request destinations.

A node sends real money to an address a stranger names, so it asks the
Chainalysis sanctions oracle, a public contract that lists sanctioned
addresses, before it offers to help or pays. Sanctions attach to an address,
not a chain, so one contract on Ethereum answers for every EVM network
(it is not deployed on Base or Linea itself). Relay screens its own routes
too, but a payout on Base does not go through Relay.

Solana addresses are not covered by the oracle; those payouts rely on Relay's
screening.
"""

import evm

ORACLE = "0x40C57923924B5c5c5455c48D93317139ADDaC8fb"
IS_SANCTIONED = "0xdf592f7d"          # isSanctioned(address)
RPC = "https://ethereum.publicnode.com"


def is_sanctioned(addr: str, rpc: str = RPC) -> bool:
    """True when the oracle lists the EVM address. Raises EVMError or
    EVMUnreachable when it cannot be asked: callers treat that as 'do not
    help', never as 'not sanctioned'."""
    data = IS_SANCTIONED + addr[2:].lower().rjust(64, "0")
    result = evm.rpc(rpc, "eth_call", [{"to": ORACLE, "data": data}, "latest"])
    try:
        return int(result, 16) != 0
    except (TypeError, ValueError) as e:
        raise evm.EVMUnreachable(f"sanctions oracle: unexpected reply {result!r}") from e
