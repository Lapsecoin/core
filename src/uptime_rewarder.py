"""In-process uptime-reward payout for peers that can't mine yet.

Runs directly inside the node process: no HTTP round-trips, no CSRF
dance, no re-supplying the passphrase every cycle, signs with the kek
already resident in memory while the node is running
(Node.build_and_sign_tx_internal).

No separate on/off switch: a budget of 0 (the default for a fresh node)
already means nothing happens, so that's what "disabled" is. Set a real
budget from the private dashboard's /rewards page to turn it on.
"""

import json
import logging
import os
import threading
import time

import crypto as crypto_mod
from params import TICKS_PER_LAPSE

log = logging.getLogger("ec.rewarder")

DEFAULT_BUDGET_LAPSE   = 0
DEFAULT_HALFLIFE_HOURS = 24 * 30
RECENT_BLOCKS_WINDOW   = 30
ACTIVE_WINDOW_S        = 3600      # "active peer" = seen within the last hour
CHECK_INTERVAL_S       = 3600      # hourly

STATE_FILE = "lapsecoin_rewards.json"


class UptimeRewarder:
    """One instance per node. .start() spawns its background thread; the
    loop itself always runs, but does nothing on a tick while the budget
    is 0 (the default)."""

    def __init__(self, node, state_file=STATE_FILE,
                 budget_lapse=DEFAULT_BUDGET_LAPSE,
                 halflife_hours=DEFAULT_HALFLIFE_HOURS,
                 recent_blocks_window=RECENT_BLOCKS_WINDOW,
                 check_interval=CHECK_INTERVAL_S):
        self.node = node
        self.state_file = state_file
        self.halflife_hours = halflife_hours
        self.recent_blocks_window = recent_blocks_window
        self.check_interval = check_interval
        self._lock = threading.Lock()
        self._state = self._load_state(budget_lapse)

    # ------------------------------------------------------------------
    # Persisted state: {"remaining_ticks": int,
    # "pending": {"tx_hash": str, "amount": int} | None}
    # ------------------------------------------------------------------

    def _load_state(self, default_budget_lapse):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                state.pop("enabled", None)  # pre-existing state files, if any
                state.setdefault("pending", None)
                if "remaining_ticks" in state:
                    return state
            except Exception:
                log.warning("[rewarder] state file unreadable, reinitializing", exc_info=True)
        state = {"remaining_ticks": int(default_budget_lapse * TICKS_PER_LAPSE),
                 "pending": None}
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        state = state if state is not None else self._state
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_file)

    # ------------------------------------------------------------------
    # Read/write surface for the web UI
    # ------------------------------------------------------------------

    def status(self):
        with self._lock:
            return dict(self._state)

    def adjust_budget(self, delta_lapse):
        """Change remaining_ticks by delta_lapse LAPSE (+ or -), floored at
        0. This is what an edited budget number on the settings page
        turns into: the field IS the remaining budget, editable in place."""
        with self._lock:
            delta_ticks = int(delta_lapse * TICKS_PER_LAPSE)
            self._state["remaining_ticks"] = max(0, self._state["remaining_ticks"] + delta_ticks)
            self._save_state()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="uptime-rewarder").start()

    def _loop(self):
        while True:
            time.sleep(self.check_interval)
            try:
                self.run_once()
            except Exception:
                log.exception("[rewarder] cycle failed")

    def run_once(self):
        """One full cycle: say we are here, resolve last cycle's pending tx,
        then (if budget remains) pay eligible peers. Safe to call directly,
        e.g. for testing. Not gated on the thread being alive."""
        # Announced on every cycle regardless of budget. Being payable and
        # choosing to pay are separate: a node with no budget still wants
        # other operators to be able to reach it.
        try:
            self.node.announce_alive()
        except Exception:
            log.debug("[rewarder] could not announce liveness", exc_info=True)

        self._resolve_pending()

        with self._lock:
            remaining_ticks = self._state["remaining_ticks"]
        if remaining_ticks <= 0:
            log.debug("[rewarder] budget exhausted, nothing to pay out")
            return

        reward_addr = self.node.addr
        wallet_balance = self.node.view.state.get_balance(reward_addr)

        win_counts = self._recent_block_win_counts()
        self_wins = win_counts.get(reward_addr, 0)

        eligible = []
        for addr in self._active_addresses() - {reward_addr}:
            if win_counts.get(addr, 0) > self_wins:
                continue  # won more than us over the window: can mine fine on its own
            if not crypto_mod.is_valid_address(addr):
                log.debug("[rewarder] skipping a malformed announced address: %r", addr)
                continue
            balance = self.node.view.state.get_balance(addr)
            if balance < remaining_ticks:
                eligible.append(addr)
        if not eligible:
            log.debug("[rewarder] no eligible peers this cycle")
            return

        pool_amount = int(remaining_ticks * (1 - 0.5 ** (1 / self.halflife_hours)))
        pool_amount = min(pool_amount, wallet_balance)
        if pool_amount <= 0:
            log.debug("[rewarder] pool amount rounded to zero")
            return
        share = pool_amount // len(eligible)
        if share <= 0:
            log.debug("[rewarder] per-peer share rounded to zero")
            return

        outputs = [{"to": addr, "amount": share} for addr in eligible]
        try:
            tx_dict, _fee = self.node.build_and_sign_tx_internal(outputs, fee=0)
        except Exception:
            log.exception("[rewarder] failed to build/sign payout tx")
            return
        ok, result = self.node.submit_tx_from_api(tx_dict)
        if not ok:
            log.warning("[rewarder] payout tx rejected: %s", result)
            return

        tx_hash = result
        sent = share * len(eligible)
        with self._lock:
            self._state["remaining_ticks"] = remaining_ticks - sent
            self._state["pending"] = {"tx_hash": tx_hash, "amount": sent}
            self._save_state()
        log.info("[rewarder] sent %.4f LAPSE to %d peer(s) (tx %s), budget remaining %.4f LAPSE",
                 share / TICKS_PER_LAPSE, len(eligible), tx_hash,
                 self._state["remaining_ticks"] / TICKS_PER_LAPSE)

    def _resolve_pending(self):
        """By the time this runs again (>= check_interval later, well past
        the mempool's 30-minute TTL), a tx from last cycle is either
        confirmed or gone. 404-equivalent (never confirmed) refunds its
        amount back into the budget instead of silently losing it."""
        with self._lock:
            pending = self._state.get("pending")
        if not pending:
            return
        # Confirmed means "in a block", and only the chain index says that.
        # Sitting in the mempool used to count too, which is the definition
        # of not confirmed: a payout still pending was recorded as having
        # gone through and its budget never refunded.
        confirmed = self.node.storage.get_tx_height(pending["tx_hash"]) is not None
        with self._lock:
            if confirmed:
                log.info("[rewarder] previous payout %s confirmed, debit of %.4f LAPSE stands",
                         pending["tx_hash"], pending["amount"] / TICKS_PER_LAPSE)
            else:
                self._state["remaining_ticks"] += pending["amount"]
                log.info("[rewarder] previous payout %s never confirmed, refunded %.4f LAPSE",
                         pending["tx_hash"], pending["amount"] / TICKS_PER_LAPSE)
            self._state["pending"] = None
            self._save_state()

    def _active_addresses(self):
        """Addresses that announced themselves active within the window.

        Read from the liveness notes that travel the network (see
        Node._handle_inbound_alive), not from what each peer told us about
        itself over GETINFO. The old source tied every address to the IP
        that reported it and published the pairing, which is a directory of
        who is who; a note that has been relayed says nothing about where
        it came from.

        It also means this no longer only reaches direct peers. A note
        propagates, so a node several hops away is just as payable as one
        we happen to be connected to.
        """
        return self.node.active_addresses(ACTIVE_WINDOW_S)

    def _recent_block_win_counts(self):
        chain = self.node.view.chain
        tip_height = self.node.view.height
        start = max(0, tip_height - self.recent_blocks_window + 1)
        counts = {}
        for h in range(start, tip_height + 1):
            builder = chain[h].get("builder")
            if builder:
                counts[builder] = counts.get(builder, 0) + 1
        return counts
