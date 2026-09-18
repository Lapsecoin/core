# P2P swap: what is left

State of the LAPSE/XLM swap feature as of commit `e09abec`. Written after
tracing each flow end to end rather than reading each module on its own,
which is how most of the items below were missed the first time: every
one of them is a module that works in isolation and a flow that does not.

Read the "Broken flows" section first. Three of them mean the feature
cannot complete a trade today, so nothing further down matters until they
are fixed.

---

## 1. Broken flows

### 1.1 A trade can never complete — done

`market_routes.py:455` was the only place a `Trade` row was created,
always with `role="taker"`. Fixed: `swap_engine.discover_trades`, called
from `swap_worker.run_once` before the existing per-trade advance loop,
watches this node's own orders for a settled step-1 payment and creates
the maker-side `Trade` and `Increment` rows.

The memo now carries an order reference, exactly as sketched here:
`swap.session_tag(order_id, session_id, n)` produces
`<order8>:<session8>:<n>` (20 bytes), and `parse_session_tag` validates
both prefixes as hex of the exact expected length.

One gap this write-up did not anticipate: the payment alone only ever
reveals the taker's address on the chain it arrived on, never the other
one the maker needs for its own reciprocating leg, and Stellar's memo has
no room left to carry it (LapseCoin's 200-byte memo would, but Stellar's
28 bytes are already spent). Resolved with a small signed "fill claim"
gossiped the same way an order is (`market.py`'s claim section,
`trade_storage.Claim`, `gossip.KIND_CLAIM`, `node._handle_inbound_claim`):
the taker states which addresses to pay and how much for a given session,
signed with the same key that controls the LapseCoin address it names. It
proves address control and nothing else; the maker still independently
re-derives the whole schedule from the claim's stated fill size and step
count and checks it against its own exposure cap before creating
anything, never trusting a taker's word for what is safe. A cancelled or
expired order with a live claim is still discovered: cancelling withdraws
what is unfilled, not what already has money moving against it
(`market.orders_by_maker_with_claims`).

### 1.2 A seller holding no XLM cannot be paid — done

`xlm.build_sponsored_create_account` and `xlm.build_create_account` were
both fully implemented and tested, and neither was ever called.
`swap_engine.XLMAdapter.build`'s `create_account` flag now gets passed:
`Engine._build_and_send` (via the new `_xlm_send_amount`) checks
`adapter.account_exists(destination)` before building an XLM leg and
switches to a create-account operation when it does not exist,
raising the sent amount to `xlm.ACCOUNT_MIN_BALANCE_STROOPS` if the
step's own agreed amount would not have cleared it (Stellar has no
smaller unit an account can be created with). The counterparty's own
settlement check only ever requires paid >= agreed, so the difference
simply overpays the step it was scheduled for.

**The "decide which" this write-up asked for turned out to have only one
answer.** Sponsored creation cannot be used here at all: CAP-33's
end-sponsoring operation is sourced by the new account itself, which
means the transaction needs a signature from the seller's own key before
it can be submitted. A buyer paying a seller it has never exchanged a
message with has no way to obtain that signature — there is no
handshake, and building one just for this would be a materially bigger
feature than plan.md's phrasing suggested. `xlm.build_sponsored_create_account`
is therefore not merely "gets its caller in 1.2" as the dead-code table
below used to say; it cannot have one under this architecture, and
should be deleted rather than kept as an unusable alternative.

Plain create is used unconditionally, which does cost the buyer up to
one XLM (the gap between the agreed step and the minimum, at most, and
only on the first payment to a given destination) — the tradeoff this
section already named, just resolved rather than left open.

### 1.3 The first-mover rule is documented but not implemented — done

`swap.opening_mover` existed, was tested, and was never called;
`market_routes._start_trade` hardcoded `i_open = True`. Fixed, with one
necessary carve-out this write-up did not account for: step 1 can only
ever be sent by the taker, because nothing else could tell the maker a
session exists in the first place (no handshake; see 1.1). Wiring
`opening_mover` into step 1 as originally described would have let a
well-established taker facing a newer maker compute `i_open = False` and
wait for a maker who has no way to know it should move first — a silent
deadlock, not a fairness improvement.

`opening_mover` is now called by both sides (`trust.mutual_scores`
derives both halves from data neither side can lie about) and genuinely
decides who moves first from step 2 onward, where both sides already know
the trade exists and either could safely be the one waiting; step 1
stays forced to the taker regardless of trust, in both
`market_routes._start_trade` and `swap_engine.discover_trades`. A test
(`test_a_tie_in_trust_never_makes_both_sides_believe_they_open`) locks in
the one way this could go wrong: the maker's step-2 decision has to be
derived as the *negation* of what the taker computed, never as an
independent parallel `opening_mover` call, or a tied trust score would
have both sides believe they open step 2 and neither would send.

---

## 2. Dead code

Everything here is written, and some of it is tested, and nothing calls
it. Each entry is either a symptom of a missing flow above or something
to delete.

| Symbol | Verdict |
|---|---|
| `swap.parse_session_tag` | done: called by `swap_engine.discover_trades` |
| `swap.opening_mover` | done: called from both sides (see 1.3) |
| `xlm.build_sponsored_create_account` | cannot be called here at all (see 1.2); delete along with its tests |
| `xlm.build_create_account` | done: called from `Engine._build_and_send` (see 1.2) |
| `swap_engine.LapseAdapter.height` | delete |
| `swap_engine.LapseAdapter.balance` | wire into the solvency check (4.2) or delete |
| `swap_engine.XLMAdapter.balance` | same |
| `trust.get_score` | superseded by `get_detail`; delete |
| `trust.all_scores`, `trust.stake_lookup_for` | delete unless the ticker (5.2) wants them |
| `market.prune_expired` | never called, so orders accumulate forever; call it (4.4) |
| `swap_worker.status()` | never surfaced; see 3.3 |

---

## 3. Structural problems

### 3.1 Four Horizon round-trips per page render

`market_routes._account_exists`, `_spendable` and `_locked` each fetch
`/accounts/<id>` separately, and `_locked` fetches it twice. That is four
calls for one page, against a rate-limited public endpoint, on every
refresh.

**Fix.** One fetch, one parse, one small struct. `xlm.py` should expose a
single `account_summary(addr)` and the three helpers collapse into it.

### 3.2 Routes reach through a module into the ORM — done

`market_routes._my_orders` built a peewee query against `market_mod.Order`
directly. Fixed: `market.orders_by_maker(addr, height)` now holds that
filter logic (deliberately not reusing `open_orders`'s remaining>0 filter,
since a maker managing their own orders still wants to see one that
finished), and `_my_orders` calls it.

### 3.3 A stalled worker is invisible — done

`swap_worker.status()` reports whether the worker is running, unlocked,
paused after an outage, and what the last error was. Surfaced on the
Trades page now (`market_routes._worker_view`, `templates_html/trades.html`):
a banner when locked, stopped, or paused after a Horizon outage, and a
quiet one-liner when everything is fine and a trade is active.

---

## 4. Security and correctness

### 4.1 Swap traffic can degrade block propagation — done

`gossip.py` kept one 50k LRU shared by every item kind. Fixed: `Gossip`
now holds one cache per kind (`_seen` is a dict keyed by `KIND_*`), block
and tx each keep the original 50k allowance, and orders get their own
20k (`ORDER_SEEN_CACHE_SIZE`) that cannot touch the others.
`mark_seen(h, kind)` now requires the kind explicitly rather than
defaulting, since a silently-wrong default is the same class of bug as
sharing one cache.

**Measured, not asserted.** `tests/test_gossip.py` now drives real
`Gossip` objects (no mocks on the propagation path) under the real,
cryptographically-random stem/fluff coin flip, not just the
always-fluff extreme the older topology tests used: 30 trials at 3 nodes
and 30 at 100 nodes (plus 30 more on a sparser 100-node graph), all
asserting full delivery. Then `TestOrderFloodDoesNotDegradeConsensusDelivery`
floods a node's order cache 2,000 entries past its ceiling and confirms a
block (and separately a tx) still reaches every node afterward.

**Bonus find while building the harness.** `node._handle_inbound_order`
had a real, live bug, not merely a risk: it called `gossip.mark_seen`
directly to skip re-verifying a duplicate order, using the exact cache
`gossip._fluff` also consults to decide whether it has already flooded
the item. That pre-mark meant the very first time any node relayed an
order onward, `_fluff` found the hash already "seen" and silently sent
nothing — orders propagated at most one hop from wherever they entered
the network, full stop, unless the origin itself happened to fluff
immediately. Invisible to the existing test suite because node.py's tests
mock gossip (no real dedup memory) and gossip.py's tests never drove
node.py's real order-handling sequence. Fixed by adding
`market.already_known(item)`, a database-backed pre-check that answers
"have we verified and stored this" without touching gossip's own
seen-cache, and by making `_handle_inbound_order` relay unconditionally
afterward — exactly the "relayed either way" pattern `_handle_inbound_tx`
and block handling already use, and for the identical reason: gating a
relay on local novelty is what strands every peer reachable only through
whoever originated the item, this node's own orders included. Covered in
`tests/test_node.py::TestHandleInboundOrder` against a real `Gossip`
instance, and in `tests/test_market.py::TestAlreadyKnown`.

### 4.2 Unbounded order intake — done

`MAX_ORDERS_PER_MAKER = 20` caps per maker, but makers are free: generate
keypairs, twenty orders each, forever. Every distinct order costs a
FALCON verification and a row. Same shape on the claim book.

**Fix.** `market.MAX_ORDERS_TOTAL` / `MAX_CLAIMS_TOTAL` cap the books
themselves regardless of how many addresses an attacker mints;
`_check_admission` / `_check_claim_admission` run before the signature
check (dedup already ran first, which was right) so a flood of garbage
never buys a FALCON verification per entry. `store_order`/`store_claim`
also recheck the global cap directly, as defense in depth against any
future caller that skips `verify_order`/`verify_claim`. Covered in
`tests/test_market.py::TestAdmissionRunsBeforeTheSignatureCheck` and
`TestClaimAdmissionRunsBeforeTheSignatureCheck`.

### 4.3 XLM sequence collisions across concurrent trades — done

`XLMAdapter.build` read the sequence fresh from Horizon per build. Two
trades both sending XLM would collide: A submits, B builds before Horizon
reflects it, B gets a stale sequence and dies. `_resend` then reads that
as "sequence consumed" and rebuilds, possibly in a loop.

**Fix.** `swap_engine.SequenceAllocator`: one allocator per account,
seeded from Horizon once and advanced in memory per envelope built, so a
same-process build for a second trade on the same wallet never re-reads
Horizon before it has caught up with the first submission. `reset()` is
called from `Engine._resend`'s dead-envelope path, so a sequence spent
by something outside this allocator (a manual withdrawal, another
process on the same wallet) still forces a fresh read next time. Covered
in `tests/test_swap_engine.py::TestSequenceAllocator` and
`TestXLMAdapterSequencing`.

### 4.4 Solvency and expiry are not checked where it matters — done

- The taker's own balance was never checked before a trade starts, so a
  trade could be opened that cannot be funded. It then stalls, and a
  stall is what blame is measured from. Fixed in
  `market_routes._start_trade`: checks XLM (spendable) or LAPSE balance,
  whichever side this node's leg is, before the claim is ever built.
- A buy order never checked the maker's XLM balance; only the sell side
  checked LAPSE. Fixed symmetrically in `market_routes._place_order`.
- `market_take` fetches an order by id directly, bypassing the expiry
  filter `open_orders()` normally applies, so a trade could still be
  opened against an order whose `expiry_block` had already passed.
  Fixed with an explicit check in `_start_trade`.
- Beyond the plan's own list: `swap_engine._discover_one` had the same
  gap on the maker's side — a taker's payment landing was enough to
  commit this node to a trade with no check that it could pay its own
  leg back. Fixed with the same balance check, run right before the
  `Trade` row is created.
- `market.prune_expired` not being called was already fixed in section 2
  above (`swap_worker.run_once` calls it after each discovery pass).

Covered in `tests/test_market_routes.py::TestStartTrade` (the new
expiry/solvency tests) and `TestPlaceOrder`, and
`tests/test_swap_engine.py::TestDiscoverTrades` (the new maker-solvency
tests).

### 4.5 `find_payment` rescans an address's entire history — done

`swap_engine.LapseAdapter.find_payment` walked every transaction the
address had ever made, for every increment, on every pass. Cost grew
without bound with account age.

**Fix.** `storage.AddrIndex` gets a `memo` column, populated as blocks
are indexed; `Storage.get_tx_by_addr_and_memo` looks a payment up by its
exact session tag directly, and `find_payment` now uses it instead of
`get_tx_heights_for_addr`. `recent_incoming` (discovery) is untouched:
it genuinely doesn't know the memo ahead of time. Schema v3, with a
one-shot migration (`_migrate_addrindex_memo`) backfilling existing
databases. Caught while testing the migration itself: declaring the
`(addr, memo)` index on the model let `create_tables(safe=True)` build
it against the existing table before the migration added the column,
corrupting the index outright; fixed by creating that index by hand
with `CREATE INDEX IF NOT EXISTS` after `_migrate()` runs. Covered in
`tests/test_storage.py::TestTxAndAddrIndex` and
`TestAddrIndexMemoMigration`, and
`tests/test_swap_engine.py::TestLapseAdapterFindPayment`.

---

## 5. Missing

### 5.1 No way to withdraw XLM — done

There was no send path for the trading wallet. XLM could arrive and could
not leave except by trading LAPSE back, which meant a user could never
reach USD. This was the most user-visible gap in the feature.

**Fix.** An asset toggle on the existing send page (LAPSE / XLM), reusing
the form and the passphrase handling rather than a second page. The XLM
side offers a plain payment, capped at spendable so the reserve stays
locked, or account-merge (new: `xlm.build_account_merge`) to close the
wallet and reclaim the reserve along with everything else. Both reuse
the same wallet and passphrase-derived key market_routes already trades
against. Covered in `tests/test_api.py::TestXlmView` /
`TestSubmitXlmAndAlert` and `tests/test_xlm.py::TestAccountMerge`.

### 5.2 No price derived from trade history — done

Only top-of-book was shown. Both chains carry matching session memos for
every completed trade, so the executed rate a trade settled at is public
and cheap to compute.

Wash trading is cheap and unpreventable here, as in any permissionless
market. Mitigated with a median rather than a mean (a handful of wash
trades pull a mean arbitrarily far but only outnumber a median), and
weighted by this node's own trust score for the counterparty (so trades
between two fresh, unstaked addresses count for as little as
trust.score already makes a fresh identity worth). A signed order and
claim already stand behind every Trade row by construction, so nothing
further was needed there.

**What shipped is narrower than "derivable by anyone" implied**: a node
only ever sees trades it was itself a party to (there is no gossip of
completed trades and no efficient way to scan the whole chain for
memo-shaped payments between arbitrary strangers), so `market.ticker_price`
reports this node's own trade history only, never a network-wide rate.
The Market page's label says so explicitly, and names wash trading as a
real possibility rather than presenting the number as a market price.
Covered in `tests/test_market.py::TestTicker`.

### 5.3 No mainnet trade has ever run

Every component is verified against live Horizon, and the crash behaviour
holds across 3,000 injected-failure trades, but two nodes have never
traded real LAPSE for real XLM. Do this with a trivial amount before
calling any of it finished.

---

## 6. Order of work

1. **Worker status on the Trades page** (3.3) — done.
2. **Per-kind dedup plus the 3-and-100-node delivery harness** (4.1) —
   done, and turned up a real order-propagation bug beyond what 4.1
   originally described (see above).
3. **Maker-side discovery** (1.1), which brought 1.3 and the memo format
   with it — done. This is what makes a trade complete. Also introduced
   the fill-claim message and `market.orders_by_maker_with_claims`,
   neither of which this write-up anticipated (see 1.1's notes above).
4. **Account creation on the XLM leg** (1.2) — done. Needed before any
   trade with a counterparty who holds no XLM, which is most new users.
5. **Admission control, sequence allocator, solvency, expiry** (4.2, 4.3,
   4.4).
6. **XLM withdrawal** (5.1).
7. **Payment index** (4.5), once there is enough history for the rescan
   to matter.
8. **Price ticker** (5.2), once completed trades exist to derive it from.
9. **Dead code sweep** (2), after 3 and 4 have claimed what they need.
10. **A real mainnet trade** (5.3).

Steps 1 and 2 touch nothing the rest depends on and can go in any order.
Steps 3 and 4 are the ones that turn this from a UI over a non-functional
engine into a working feature.
