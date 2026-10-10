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
from params import TICKS_PER_LAPSE

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

# What a node asks as the lock before it will serve a request. A policy, not
# a rule: it follows the price of LAPSE, so it is a constant that gets
# lowered in a release when LAPSE is worth more, and updated nodes follow.
MIN_SERVED_LOCK = 10 * TICKS_PER_LAPSE

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
    Network("scroll", "Scroll", 534352, "ETH", 18, "evm", _ZERO,
            "https://rpc.scroll.io/", 0.05, "https://scrollscan.com/tx/"),
    Network("blast", "Blast", 81457, "ETH", 18, "evm", _ZERO,
            "https://rpc.blast.io/", 0.05, "https://blastscan.io/tx/"),
    Network("mantle", "Mantle", 5000, "MNT", 18, "evm", _ZERO,
            "https://rpc.mantle.xyz", 0.21, "https://mantlescan.xyz/tx/"),
    Network("gnosis", "Gnosis", 100, "xDAI", 18, "evm", _ZERO,
            "https://rpc.gnosischain.com/", 0.13, "https://gnosisscan.io/tx/"),
    Network("sonic", "Sonic", 146, "S", 18, "evm", _ZERO,
            "https://rpc.soniclabs.com", 0.13, "https://sonicscan.org/tx/"),
    Network("celo", "Celo", 42220, "CELO", 18, "evm", _ZERO,
            "https://forno.celo.org", 0.14, "https://celoscan.io/tx/"),
    Network("berachain", "Berachain", 80094, "BERA", 18, "evm", _ZERO,
            "https://rpc.berachain.com/", 0.12, "https://beratrail.io/tx/"),
    Network("hyperevm", "HyperEVM", 999, "HYPE", 18, "evm", _ZERO,
            "https://rpc.hyperliquid.xyz/evm", 0.06, "https://hyperevmscan.io/tx/"),
    Network("unichain", "Unichain", 130, "ETH", 18, "evm", _ZERO,
            "https://mainnet.unichain.org", 0.11, "https://uniscan.xyz/tx/"),
    Network("world-chain", "World Chain", 480, "ETH", 18, "evm", _ZERO,
            "https://worldchain-mainnet.gateway.tenderly.co", 0.04, "https://worldscan.org/tx/"),
    Network("ink", "Ink", 57073, "ETH", 18, "evm", _ZERO,
            "https://ink.drpc.org", 0.05, "https://explorer.inkonchain.com/tx/"),
    Network("ronin", "Ronin", 2020, "RON", 18, "evm", _ZERO,
            "https://api.roninchain.com/rpc", 0.12, "https://explorer.roninchain.com/tx/"),
    Network("cronos", "Cronos", 25, "CRO", 18, "evm", _ZERO,
            "https://cronos.drpc.org", 0.11, "https://cronoscan.com/tx/"),
    Network("apechain", "ApeChain", 33139, "APE", 18, "evm", _ZERO,
            "https://apechain.calderachain.xyz/http", 0.09, "https://apescan.io/tx/"),
    Network("soneium", "Soneium", 1868, "ETH", 18, "evm", _ZERO,
            "https://rpc.soneium.org/", 0.04, "https://soneium.blockscout.com/tx/"),
    Network("katana", "Katana", 747474, "ETH", 18, "evm", _ZERO,
            "https://rpc.katana.network", 0.04, "https://explorer.katanarpc.com/tx/"),
    Network("zora", "Zora", 7777777, "ETH", 18, "evm", _ZERO,
            "https://rpc.zora.energy", 0.11, "https://explorer.zora.energy/tx/"),
    Network("mode", "Mode", 34443, "ETH", 18, "evm", _ZERO,
            "https://mainnet.mode.network/", 0.11, "https://explorer.mode.network/tx/"),
    Network("plasma", "Plasma", 9745, "XPL", 18, "evm", _ZERO,
            "https://rpc.plasma.to", 0.13, "https://plasmascan.to/tx/"),
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
# The claim a helping node signs, and the memos that carry requests and claims
# ---------------------------------------------------------------------------

def claim_message(ref: str, lapse_from: str) -> bytes:
    """What a claimer's Base address signs to show it holds the funds it
    claims to pay with."""
    return gaslock.claim_message(ref, lapse_from)


def _b64(sig_hex_or_bytes) -> str:
    raw = bytes.fromhex(sig_hex_or_bytes[2:]) if isinstance(sig_hex_or_bytes, str) \
        else bytes(sig_hex_or_bytes)
    return base64.b64encode(raw).decode()


def build_request_memo(net_slug: str, target: int, dest: str) -> str:
    return f"{REQUEST_TAG}{net_slug} {target} {dest}"


def parse_request_memo(memo):
    """The request a memo makes, or None if it is not a well-formed one."""
    if not isinstance(memo, str) or not memo.startswith(REQUEST_TAG):
        return None
    parts = memo[len(REQUEST_TAG):].split(" ")
    if len(parts) != 3:
        return None
    slug, target, dest = parts
    net = NETWORKS.get(slug)
    if net is None or not re.fullmatch(r"[1-9][0-9]{0,30}", target):
        return None
    if not is_valid_address(net, dest):
        return None
    return dict(network=slug, target=int(target), dest=dest)


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
