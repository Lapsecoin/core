"""Reads for the web UI taken straight from this node's own chain, state
and mempool.

LocalReader is one of two implementations of the same small interface the
wallet pages are written against (the other, RemoteReader, asks another
node over HTTP and lives in remote_reader.py). Anything the pages need from
a node goes through these methods, so the full node and the light client
render the same pages from the same code and cannot drift apart.
"""

import hashlib
import threading

import block as block_mod
import board_view
import state as state_mod
import tx as tx_mod
from params import MIN_RELAY_FEE_RATE
from ui_common import _tx_amount

# Rows per page of an address's history.
HISTORY_PER_PAGE = 3


def _get_address_history(addr, node):
    """Every indexed transaction touching addr, newest first.

    Rows are grouped by block and each block is hashed through once, rather
    than re-hashing its whole transaction list per row. An address with
    several transactions in one block used to walk that block once per row,
    recomputing tx_hash (a full canonical serialization plus a digest) for
    every transaction on every pass.
    """
    chain = node.view.chain
    wanted = {}
    for height, tx_h in node.storage.get_tx_heights_for_addr(addr):
        if 0 <= height < len(chain):
            wanted.setdefault(height, []).append(tx_h)

    history = []
    for height, hashes in wanted.items():
        by_hash = {tx_mod.tx_hash(t): t for t in chain[height]["transactions"]}
        for tx_h in hashes:
            t = by_hash.get(tx_h)
            if t is not None:
                direction = "sent" if t.get("from") == addr else "received"
                history.append((height, tx_h, direction, t))
    history.sort(key=lambda row: row[0], reverse=True)
    return history


class _RewardSeries:
    """The mint reward at each height, replayed from genesis once and
    extended as the chain grows.

    Two callers needed this and each replayed the whole emission curve from
    genesis itself, one of them per page view: 45ms at height 100k on a
    public, unauthenticated page, growing with the chain forever. The
    series only ever gets longer at the end, so it is computed once and
    appended to.

    Read from Flask threads, which are many, and extended by whichever gets
    there first, so extension holds a lock. The list is only ever appended
    to under it, never rewritten, so a reader holding an index already
    within range does not need one.
    """

    def __init__(self):
        self._rewards = []       # reward minted by the block at each height
        self._total_minted = 0   # running total after the last entry
        self._lock = threading.Lock()

    def _extend_to(self, height):
        with self._lock:
            while len(self._rewards) <= height:
                reward = state_mod.compute_reward(self._total_minted)
                self._rewards.append(reward)
                if reward >= 1:
                    self._total_minted += reward

    def reward_at(self, height):
        if height < 0:
            return 0
        if height >= len(self._rewards):
            self._extend_to(height)
        return self._rewards[height]

    def prefix(self, count):
        """Rewards for heights [0, count), for a single walk of the chain."""
        if count <= 0:
            return []
        self.reward_at(count - 1)
        return self._rewards[:count]


_reward_series = _RewardSeries()


def _block_reward(chain, height):
    """Mint reward for the block at `height`. 0 for genesis (no builder,
    nothing minted)."""
    return _reward_series.reward_at(height)


def _get_mined_blocks_for_addr(addr, node):
    """Blocks built by addr, with the reward + fees paid to the builder.

    Mining rewards never go through the mempool/AddrIndex (they're credited
    directly in chainstate._apply_builder_reward), so they can't be pulled
    from the same tx index as ordinary transfers. The chain is kept fully
    in memory though, so we can just walk it once, reading each height's
    reward off the shared series rather than re-deriving the emission curve
    on every lookup.
    """
    chain   = node.view.chain
    rewards = _reward_series.prefix(len(chain))
    return [(height, blk["hash"], rewards[height] + block_mod.block_fees(blk))
            for height, blk in enumerate(chain)
            if blk.get("builder") == addr]


def fee_estimate(node):
    """Current mempool fee-per-byte picture for the send UI.

    Reuses block.assemble() itself (rather than reimplementing its
    fee-per-byte packing logic) to find the "next block" clearing rate, so
    this can never quietly drift out of sync with what actually gets a
    transaction included.

    Returns {"pending": int, "min": float, "median": float, "max": float,
    "next_block": float}. next_block never reads below params.MIN_RELAY_FEE_RATE:
    that floor is enforced by the mempool regardless of congestion (see
    mempool.Mempool.add), so an uncongested network never actually suggests 0,
    the same reason no real network's fees are literally 0 when idle.
    """
    pending = node.mempool.all_txs()
    if not pending:
        return {"pending": 0, "min": 0, "median": 0, "max": 0,
                "next_block": MIN_RELAY_FEE_RATE}

    rates = sorted(tx_mod.fee_rate(t) for t in pending)
    n = len(rates)
    median = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2

    v = node.view
    iterations = block_mod.get_vdf_iterations(v.chain)
    board_floor = tx_mod.board_fee_floor(v.state.total_board_posts)
    candidate = block_mod.assemble(v.tip, pending, v.tip.get("builder") or "",
                                   iterations, board_fee_floor=board_floor)
    included = candidate["transactions"]
    # Full block: the going rate is the lowest fee-per-byte that still made
    # it in, whatever that is, congestion sets its own price. Otherwise
    # everything pending fits, so the relay floor is what's actually
    # required to clear the next block, not 0.
    if len(included) < n:
        next_block = min(tx_mod.fee_rate(t) for t in included)
    else:
        next_block = MIN_RELAY_FEE_RATE

    return {"pending": n, "min": rates[0], "median": median, "max": rates[-1],
            "next_block": next_block}



class LocalReader:

    def __init__(self, node):
        self.node = node
        # One slot is enough: only the latest board state is ever asked for.
        # Stored as a single tuple so a concurrent reader never sees half of
        # an update.
        self._board_cache = {"key": None, "value": None}

    # Fee market

    def fee_estimate(self):
        return fee_estimate(self.node)

    # One account

    def account(self, addr=None, *, nick=None, fees=False, fresh=False):
        """Everything a wallet page needs to know about one address in a
        single answer, so a remote reader costs one request, not four.

        Always carries board_floor. With addr: balance and nonce (the
        highest nonce either confirmed or still pending, so the next
        transaction is nonce + 1). With nick: nick_owner, the address that
        owns it or None. With fees: the fee_estimate dict. `fresh` is for
        readers that keep answers for a while; this one always reads the
        live state.
        """
        node = self.node
        v = node.view
        out = {"board_floor": tx_mod.board_fee_floor(v.state.total_board_posts)}
        if addr:
            out["balance"] = v.state.get_balance(addr)
            out["nonce"] = max(v.state.get_nonce(addr),
                               node.mempool.pending_nonce(addr))
        if nick:
            out["nick_owner"] = board_view._nickname_owned_by(v.state, nick)
        if fees:
            out["fees"] = fee_estimate(node)
        return out

    def submit(self, tx_dict):
        return self.node.submit_tx_from_api(tx_dict)

    # Address page

    def address_page(self, addr, page):
        """One page of an address's history, newest first, with the
        totals the page header shows. Each row is
        (height, hash, direction, amount, kind) where kind is "tx" or
        "block" (a block the address built)."""
        node = self.node
        v = node.view
        tx_rows = [(h, hsh, direction, _tx_amount(t), "tx")
                   for h, hsh, direction, t in _get_address_history(addr, node)]
        mined_rows = [(h, hsh, "mined", amount, "block")
                      for h, hsh, amount in _get_mined_blocks_for_addr(addr, node)]
        newest_first = sorted(tx_rows + mined_rows, key=lambda r: r[0], reverse=True)
        total_pages = max(-(-len(newest_first) // HISTORY_PER_PAGE), 1)
        page = min(max(page, 1), total_pages)
        start = (page - 1) * HISTORY_PER_PAGE
        return {"balance": v.state.get_balance(addr),
                "tx_count": v.state.get_nonce(addr),
                "page": page, "total_pages": total_pages,
                "rows": [list(r) for r in newest_first[start:start + HISTORY_PER_PAGE]]}

    # Board

    def _board_state_key(self):
        node = self.node
        chain = node.view.chain
        mem = tuple(sorted(
            tx_mod.tx_hash(t) for t in node.mempool.all_txs()
            if (t.get("memo") or "").startswith(board_view.BOARD_TAGS)))
        return (id(node), len(chain), chain[-1].get("hash") if chain else None, mem)

    def _board_snapshot(self):
        # The heavy part of the board (a scan of the whole chain plus the
        # vote and profile pass) only changes when a block lands or a
        # board/vote tx enters or leaves the mempool, so it is computed
        # once per such state and every viewer, poll and scroll chunk
        # reuses it.
        key = self._board_state_key()
        cached = self._board_cache["value"]
        if cached is not None and self._board_cache["key"] == key:
            return key, cached
        node = self.node
        value = board_view.build_board_snapshot(
            node.view.chain, node.view.state.nicknames, node.mempool)
        self._board_cache["key"], self._board_cache["value"] = key, value
        return key, value

    def profile(self, addr):
        """addr's current board icon and nickname, or None."""
        return self._board_snapshot()[1][2].get(addr)

    def board_etag(self, chunks):
        key = self._board_state_key()
        return hashlib.sha1(repr((key, max(chunks, 1))).encode()).hexdigest()

    def board_page(self, chunks):
        key, snap = self._board_snapshot()
        data = board_view.board_page_data(snap, chunks)
        data["etag"] = hashlib.sha1(repr((key, max(chunks, 1))).encode()).hexdigest()
        return data


def local_reader_for(node):
    """The one LocalReader for this node. The public and private apps both
    ask, and sharing it means the board is resolved once, not once per app."""
    reader = getattr(node, "_ui_reader", None)
    if reader is None:
        reader = node._ui_reader = LocalReader(node)
    return reader
