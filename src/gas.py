"""Fee requests: what a node will do for someone holding a token with no gas.

A request is an ordinary LapseCoin transaction whose memo names a network and
a target gas balance for an address the sender controls. A live node sends
the shortfall, in that network's own gas coin, through Relay. Nothing here
touches the chain's consensus rules (those live in the fee lock, see
chainstate); this module is the shared vocabulary every node derives the same
answers from: which networks and actions exist, how much a request may ask
for, how a memo reads, who has priority among the nodes that offered.

Amounts are integers in a network's smallest unit (wei, lamports). Dollar
figures only ever appear as ceilings and are floats at the edges.
"""

import base64
import hashlib
import re
from dataclasses import dataclass

import evm
import gaslock

# ---------------------------------------------------------------------------
# Policy constants. Fixed in code on purpose: they are what makes one node's
# answer to "should I help?" the same as every other node's, and a request
# that is fine for some nodes and refused by others is just noise.
# ---------------------------------------------------------------------------

# The most any one request may cost a node, payout and Relay overhead
# together. Disclosed to the requester next to what their action needs.
NODE_CAP_USD = 2.00

# Below this a payout is not worth the fixed overhead of making it.
FLOOR_USD = 0.25

# Head-room over today's gas price: it moves between quoting and spending.
GAS_SAFETY = 1.5

# A destination already holding this share of its target is done.
SATISFIED_SHARE = 0.9

# Blocks, derived from chain data only. The window and the memo tags are
# consensus (the chain settles the lock on them) so they live in gaslock.
CLAIM_WINDOW_BLOCKS = gaslock.CLAIM_WINDOW_BLOCKS
RANK_SLOT_BLOCKS = 2        # each ranked claimer's time to pay before the next may
MAX_RANKS = 3

REQUEST_TAG = gaslock.REQUEST_TAG
CLAIM_TAG = gaslock.CLAIM_TAG
REF_LEN = gaslock.REF_LEN


@dataclass(frozen=True)
class Network:
    slug: str
    name: str
    chain_id: int
    symbol: str
    decimals: int
    vm: str                  # "evm" or "svm"
    currency: str            # Relay's address for the native coin
    rpc: str                 # public default, read-only use
    overhead_usd: float      # what Relay costs on top of the payout, measured
    explorer: str            # tx URL prefix


_ZERO = "0x0000000000000000000000000000000000000000"

NETWORKS = {n.slug: n for n in (
    Network("base", "Base", 8453, "ETH", 18, "evm", _ZERO,
            "https://mainnet.base.org", 0.0, "https://basescan.org/tx/"),
    Network("ethereum", "Ethereum", 1, "ETH", 18, "evm", _ZERO,
            "https://ethereum.publicnode.com", 0.05, "https://etherscan.io/tx/"),
    Network("optimism", "Optimism", 10, "ETH", 18, "evm", _ZERO,
            "https://optimism.publicnode.com", 0.03, "https://optimistic.etherscan.io/tx/"),
    Network("arbitrum", "Arbitrum", 42161, "ETH", 18, "evm", _ZERO,
            "https://arbitrum-one.publicnode.com", 0.03, "https://arbiscan.io/tx/"),
    Network("linea", "Linea", 59144, "ETH", 18, "evm", _ZERO,
            "https://rpc.linea.build", 0.03, "https://lineascan.build/tx/"),
    Network("bsc", "BNB Chain", 56, "BNB", 18, "evm", _ZERO,
            "https://bsc-rpc.publicnode.com", 0.05, "https://bscscan.com/tx/"),
    Network("polygon", "Polygon", 137, "POL", 18, "evm", _ZERO,
            "https://polygon-bor-rpc.publicnode.com", 0.15, "https://polygonscan.com/tx/"),
    Network("avalanche", "Avalanche", 43114, "AVAX", 18, "evm", _ZERO,
            "https://api.avax.network/ext/bc/C/rpc", 0.15, "https://snowtrace.io/tx/"),
    Network("solana", "Solana", 792703809, "SOL", 9, "svm", "11111111111111111111111111111111",
            "https://api.mainnet-beta.solana.com", 0.05, "https://solscan.io/tx/"),
)}


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    evm_gas: int             # gas units on an EVM chain
    svm_lamports: int        # lamports on Solana (fees plus any new token account rent)


ACTIONS = {a.key: a for a in (
    Action("send", "Send a token", 65_000, 2_050_000),
    Action("swap", "Swap tokens (already approved)", 250_000, 4_200_000),
    Action("approve_swap", "Approve and swap", 300_000, 4_200_000),
)}


def network(slug):
    return NETWORKS.get(slug)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_address(net: Network, addr) -> bool:
    if not isinstance(addr, str):
        return False
    if net.vm == "evm":
        return evm.is_valid_address(addr)
    if not 32 <= len(addr) <= 44 or any(c not in _B58_INDEX for c in addr):
        return False
    return len(b58decode(addr)) == 32


def same_address(net: Network, a: str, b: str) -> bool:
    return a.lower() == b.lower() if net.vm == "evm" else a == b


# ---------------------------------------------------------------------------
# How much
# ---------------------------------------------------------------------------

def units_per_coin(net: Network) -> int:
    return 10 ** net.decimals


def usd_to_units(net: Network, usd: float, price_usd: float) -> int:
    return int(usd / price_usd * units_per_coin(net))


def units_to_usd(net: Network, units: int, price_usd: float) -> float:
    return units / units_per_coin(net) * price_usd


def payout_cap_usd(net: Network) -> float:
    """The most of the payout itself a node will send: its ceiling minus what
    Relay costs on this route."""
    return max(NODE_CAP_USD - net.overhead_usd, 0.0)


def needed_units(net: Network, action: Action, gas_price: int = 0) -> int:
    """What the action needs in the network's smallest unit, with head-room.
    gas_price is wei per gas on an EVM chain and ignored on Solana."""
    if net.vm == "svm":
        return action.svm_lamports
    return int(action.evm_gas * gas_price * GAS_SAFETY)


def effective_target(net: Network, target: int, price_usd: float) -> int:
    """The balance a destination can actually be brought to: what it asked
    for, but never more than one node's ceiling covers. Capping it here is
    what stops a later-ranked node paying again for a need no single payout
    could ever meet: once the destination holds this much, every node
    agrees it is done."""
    return min(target, usd_to_units(net, payout_cap_usd(net), price_usd))


def payout_units(net: Network, target: int, balance: int, price_usd: float) -> int:
    """What a node sends right now, or 0 when the destination is done.

    Recomputed from the destination's live balance at the node's turn, which
    is what makes a second-ranked node a no-op when the first one succeeded:
    the balance already sits at the target.
    """
    eff = effective_target(net, target, price_usd)
    if balance >= eff * SATISFIED_SHARE:
        return 0
    floor_units = usd_to_units(net, FLOOR_USD, price_usd)
    cap_units = usd_to_units(net, payout_cap_usd(net), price_usd)
    return min(max(eff - balance, floor_units), cap_units)


def plan(net: Network, action: Action, *, gas_price: int, price_usd: float, balance: int):
    """What the requester should see and ask for.

    target is the balance the action needs; deliver is what a node will
    actually send towards it, never more than a node's ceiling allows. When
    that falls short of the need the page says how much it covers instead of
    promising it.
    """
    target = needed_units(net, action, gas_price)
    deliver = payout_units(net, target, balance, price_usd)
    covered = min((balance + deliver) / target, 1.0) if target else 1.0
    return dict(target=target, shortfall=max(target - balance, 0), deliver=deliver,
                needs_help=deliver > 0, covers_share=covered,
                target_usd=units_to_usd(net, target, price_usd),
                deliver_usd=units_to_usd(net, deliver, price_usd),
                cap_usd=payout_cap_usd(net))


# ---------------------------------------------------------------------------
# Messages the two parties sign, and the memos that carry them
# ---------------------------------------------------------------------------

def request_message(net_slug: str, target: int, dest: str, lapse_from: str, nonce: int) -> bytes:
    """What the destination address signs to show it belongs to the sender.
    Bound to the sender and its nonce so a signature cannot be lifted onto
    someone else's request."""
    return (f"LapseCoin fee request\n{net_slug}\n{target}\n{dest}\n"
            f"{lapse_from}\n{nonce}").encode()


def claim_message(ref: str, lapse_from: str) -> bytes:
    """What a claimer's Base address signs to show it holds the funds it
    claims to pay with."""
    return f"LapseCoin fee claim\n{ref}\n{lapse_from}".encode()


def _b64(sig_hex_or_bytes) -> str:
    raw = bytes.fromhex(sig_hex_or_bytes[2:]) if isinstance(sig_hex_or_bytes, str) \
        else bytes(sig_hex_or_bytes)
    return base64.b64encode(raw).decode()


def build_request_memo(net_slug: str, target: int, dest: str, signature) -> str:
    """`signature` is the wallet's: a 0x-hex personal_sign string for an EVM
    address, raw 64 bytes for a Solana one."""
    return f"{REQUEST_TAG}{net_slug} {target} {dest} {_b64(signature)}"


def parse_request_memo(memo):
    """The request a memo makes, or None if it is not a well-formed one."""
    if not isinstance(memo, str) or not memo.startswith(REQUEST_TAG):
        return None
    parts = memo[len(REQUEST_TAG):].split(" ")
    if len(parts) != 4:
        return None
    slug, target, dest, sig = parts
    net = NETWORKS.get(slug)
    if net is None or not re.fullmatch(r"[1-9][0-9]{0,30}", target):
        return None
    if not is_valid_address(net, dest):
        return None
    try:
        raw = base64.b64decode(sig, validate=True)
    except ValueError:
        return None
    if len(raw) != (65 if net.vm == "evm" else 64):
        return None
    return dict(network=slug, target=int(target), dest=dest, signature=raw)


def verify_request_signature(req: dict, lapse_from: str, nonce: int) -> bool:
    net = NETWORKS[req["network"]]
    msg = request_message(req["network"], req["target"], req["dest"], lapse_from, nonce)
    if net.vm == "evm":
        signer = evm.recover_message_signer(msg, "0x" + req["signature"].hex())
        return signer is not None and signer.lower() == req["dest"].lower()
    import nacl.exceptions
    import nacl.signing
    try:
        nacl.signing.VerifyKey(b58decode(req["dest"])).verify(msg, req["signature"])
        return True
    except (nacl.exceptions.BadSignatureError, ValueError):
        return False


def request_ref(request_txid: str) -> str:
    return request_txid[:REF_LEN]


def build_claim_memo(request_txid: str, base_addr: str, signature) -> str:
    return f"{CLAIM_TAG}{request_ref(request_txid)} {base_addr} {_b64(signature)}"


def parse_claim_memo(memo):
    if not isinstance(memo, str) or not memo.startswith(CLAIM_TAG):
        return None
    parts = memo[len(CLAIM_TAG):].split(" ")
    if len(parts) != 3:
        return None
    ref, base_addr, sig = parts
    if not re.fullmatch(r"[0-9a-f]{%d}" % REF_LEN, ref) or not evm.is_valid_address(base_addr):
        return None
    try:
        raw = base64.b64decode(sig, validate=True)
    except ValueError:
        return None
    if len(raw) != 65:
        return None
    return dict(ref=ref, base_addr=base_addr, signature=raw)


def verify_claim_signature(claim: dict, lapse_from: str) -> bool:
    msg = claim_message(claim["ref"], lapse_from)
    signer = evm.recover_message_signer(msg, "0x" + claim["signature"].hex())
    return signer is not None and signer.lower() == claim["base_addr"].lower()


# ---------------------------------------------------------------------------
# Timing and order, derived from chain data every node shares
# ---------------------------------------------------------------------------

def window_close(request_height: int, first_claim_height=None) -> int:
    """The last block a claim may land in. The first claim, at x, closes the
    window at x+1 (one block for everyone else to show they saw the request),
    never later than the request's block plus CLAIM_WINDOW_BLOCKS."""
    return gaslock.close_height({"height": request_height, "claim": first_claim_height})


def rank_key(request_txid: str, window_close_block_hash: str, claimer_addr: str) -> str:
    return hashlib.sha256(
        (request_txid + window_close_block_hash + claimer_addr).encode()).hexdigest()


def order_claimers(request_txid: str, window_close_block_hash: str, claimers) -> list:
    """Claimer addresses, first to pay first. Stable and identical on every
    node given the same inputs; at most MAX_RANKS of them are ever used."""
    ranked = sorted(set(claimers),
                    key=lambda a: rank_key(request_txid, window_close_block_hash, a))
    return ranked[:MAX_RANKS]


def turn_start(close_height: int, rank_index: int) -> int:
    """The first block at which the claimer at rank_index (0 based) may pay.
    The order is fixed by the hash of the block that closes the window, so
    rank 0 starts on the block after it and each next one RANK_SLOT_BLOCKS
    later."""
    return close_height + 1 + rank_index * RANK_SLOT_BLOCKS
