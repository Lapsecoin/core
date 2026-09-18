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

### 1.1 A trade can never complete

`market_routes.py:455` is the only place a `Trade` row is created, always
with `role="taker"`. Nothing tells the maker a trade exists, so the
maker's worker never reciprocates. The taker pays step one and waits
forever.

**Fix.** The maker discovers the trade from the chain. Their node already
watches its own address; it should recognise an incoming payment whose
memo names one of its own open orders, create the maker-side `Trade`, and
reciprocate.

This needs no new message type and no handshake. It also produces the
acceptance proof that `swap_engine._peer_ever_reciprocated` already
depends on.

The memo must carry an order reference. Stellar's text memo is 28 bytes,
which is the binding constraint:

```
<order8>:<session8>:<n>     20 bytes, fits
```

`swap.session_tag` and `swap.parse_session_tag` both need to change
shape, and `parse_session_tag` gets its first caller (see 2.1).

### 1.2 A seller holding no XLM cannot be paid

`xlm.build_sponsored_create_account` and `xlm.build_create_account` are
both fully implemented and tested, and **neither is ever called**.
`swap_engine.XLMAdapter.build` takes a `create_account` flag that nothing
passes, so every XLM leg is built as a plain payment.

A plain payment to an address with no account behind it fails with
`op_no_destination`. So the advertised property — that somebody holding
only LAPSE can sell it without owning XLM first — does not hold. The UI
states it does (`market.html`, "you can sell LAPSE without funding it
first"), which makes this worse than a missing feature.

**Fix.** `ensure_sent` checks `account_exists(destination)` before
building an XLM leg, and chooses create-account or sponsored-create when
it does not. Decide which: plain create costs the buyer 1 XLM that the
seller then holds as reserve, sponsored create costs the buyer nothing
permanent but raises their own reserve while it stands.

### 1.3 The first-mover rule is documented but not implemented

`swap.opening_mover` exists, is tested, and is **never called**.
`market_routes._start_trade` hardcodes `i_open = True`, so the taker
always opens.

The documented rule is that the less established side opens, so the party
asking to be trusted is the one who demonstrates it. What actually
happens is that takers always carry the opening risk regardless of
standing.

**Fix.** Call `opening_mover` with both sides' scores and fall back to
role on a tie, which is what its `None` return is for.

---

## 2. Dead code

Everything here is written, and some of it is tested, and nothing calls
it. Each entry is either a symptom of a missing flow above or something
to delete.

| Symbol | Verdict |
|---|---|
| `swap.parse_session_tag` | gets its caller in 1.1 |
| `swap.opening_mover` | gets its caller in 1.3 |
| `xlm.build_sponsored_create_account` | gets its caller in 1.2 |
| `xlm.build_create_account` | reachable only via a flag nobody sets; see 1.2 |
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

### 3.2 Routes reach through a module into the ORM

`market_routes._my_orders` builds a peewee query against
`market_mod.Order` directly (`market_routes.py:516`). Every other
database access in that file goes through a `market.py` function. This is
the one place the boundary leaks, and it duplicates filter logic that
`market.open_orders` already has.

**Fix.** `market.orders_by_maker(addr, height)` and call that.

### 3.3 A stalled worker is invisible

`swap_worker.status()` reports whether the worker is running, unlocked,
paused after an outage, and what the last error was. Nothing displays it.
A trade that is not progressing looks identical to one that is waiting,
and the user has no way to tell a locked wallet from an unreachable
Horizon from a genuine wait.

**Fix.** Surface it on the Trades page. This is small and worth doing
early, because it makes every later problem visible instead of silent.

---

## 4. Security and correctness

### 4.1 Swap traffic can degrade block propagation

`gossip.py:101` keeps one 50k LRU shared by every item kind. Flooding
distinct orders evicts block and transaction hashes, and an evicted block
hash means that block gets re-flooded. A swap feature must not be able to
slow consensus down.

**Fix.** Per-kind dedup namespaces with their own budgets. Consensus
kinds keep the full allowance; swap kinds get a separate one and cannot
touch it.

**Must be measured, not asserted.** The Dandelion stem/fluff rule is
built to deliver to every node from three peers up to a hundred, and
there is currently no test of that property at all. Build the harness,
confirm 100% delivery at 3 and at 100 nodes, then confirm it still holds
with an order flood running.

### 4.2 Unbounded order intake

`MAX_ORDERS_PER_MAKER = 20` caps per maker, but makers are free: generate
keypairs, twenty orders each, forever. Every distinct order costs a
FALCON verification and a row.

**Fix.** A global cap, a per-peer intake rate, and cheap structural
checks before the signature check. Dedup already runs first, which is
right; the expensive check should come last.

### 4.3 XLM sequence collisions across concurrent trades

`XLMAdapter.build` reads the sequence fresh from Horizon per build. Two
trades both sending XLM will collide: A submits, B builds before Horizon
reflects it, B gets a stale sequence and dies. `_resend` then reads that
as "sequence consumed" and rebuilds, possibly in a loop.

**Fix.** One allocator per account that hands out sequences, seeded from
Horizon and advanced locally per envelope built.

### 4.4 Solvency and expiry are not checked where it matters

- The taker's own balance is never checked before a trade starts, so a
  trade can be opened that cannot be funded. It then stalls, and a stall
  is what blame is measured from.
- A buy order never checks the maker's XLM balance. Only the sell side
  checks LAPSE (`market_routes.py:372`).
- The engine never reads `expiry_block`, so a trade outlives its order
  silently.
- `market.prune_expired` is never called (see 2), so expired orders stay
  in the book and in the database indefinitely.

### 4.5 `find_payment` rescans an address's entire history

`swap_engine.LapseAdapter.find_payment` walks every transaction the
address has ever made, for every increment, on every pass. Cost grows
without bound with account age.

**Fix.** Index confirmed payments by session tag as blocks are applied,
and look up by tag.

---

## 5. Missing

### 5.1 No way to withdraw XLM

There is no send path for the trading wallet. XLM can arrive and cannot
leave except by trading LAPSE back, which means a user can never reach
USD. This is the most user-visible gap in the feature.

**Fix.** An asset dropdown on the existing send page (LAPSE / XLM) rather
than a second page, reusing the form and the passphrase handling. The XLM
option needs spendable-minus-reserve, and an account-merge option to
close the wallet and reclaim the reserve.

### 5.2 No price derived from trade history

Only top-of-book is shown. Both chains carry matching session memos for
every completed trade, so the executed rate is derivable by anyone, which
is the ticker the original design called for.

Wash trading is cheap and unpreventable here, as in any permissionless
market. Mitigate with a median rather than a mean, requiring a signed
order behind each counted trade, and weighting by standing. Label it for
what it is.

### 5.3 No mainnet trade has ever run

Every component is verified against live Horizon, and the crash behaviour
holds across 3,000 injected-failure trades, but two nodes have never
traded real LAPSE for real XLM. Do this with a trivial amount before
calling any of it finished.

---

## 6. Order of work

1. **Worker status on the Trades page** (3.3). Small, and makes
   everything after it visible instead of silent.
2. **Per-kind dedup plus the 3-and-100-node delivery harness** (4.1).
   Protects consensus before swap traffic grows, and the harness is
   reusable.
3. **Maker-side discovery** (1.1), which brings 1.3 and the memo format
   with it. This is what makes a trade complete.
4. **Account creation on the XLM leg** (1.2). Needed before any trade
   with a counterparty who holds no XLM, which is most new users.
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
