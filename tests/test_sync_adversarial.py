"""Can a peer win a sync by lying?

The real Syncer, the real Node (_sync_page_outcome, apply_better_chain,
validation, fork choice) and real VDF proofs, with only two things shrunk:
the per-block iteration count (a few thousand instead of 12.2M, so a proof
takes milliseconds) and the clock (advanced by hand so builders can stamp
honest times). The attacker is a fake UDP endpoint that answers GETINFO and
GETSYNC however it likes.

What these pin down: an INFO claim (height, work, tip) is never trusted; a
chain is adopted only when blocks that verify, each chained on the previous
one's hash, carry strictly more proven work than ours; and an attacker
cannot hold a claim "until it has built the blocks", because a block cannot
be computed before its parent exists.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import vdf as vdf_mod
from params import MIN_BLOCK_SPACING_SECONDS
from syncer import Syncer
from tests.fixtures import address
from tests.test_node import node_env  # noqa: F401  (fixture)

ITERS = 3000
HONEST, ATTACKER, HEAVY = address(1), address(2), address(3)


@pytest.fixture
def clock(monkeypatch):
    """Hand-driven clock and a tiny, constant iteration requirement."""
    t = [block_mod.create_genesis()["timestamp"]]
    monkeypatch.setattr(block_mod._time, "time", lambda: t[0])
    # A chain whose first block was built by HEAVY demands twice the work per
    # block afterwards, standing in for a fork that retargeted harder.
    monkeypatch.setattr(
        block_mod, "get_vdf_iterations",
        lambda chain: 2 * ITERS if len(chain) > 1 and chain[1].get("builder") == HEAVY else ITERS)
    return t


def build_on(chain, builder, clock, iterations=None, ts=None, challenge_parent=None):
    """One real block on top of chain. Overrides let a test forge it."""
    tip = chain[-1]
    if iterations is None:
        iterations = block_mod.get_vdf_iterations(chain)
    clock[0] = ts if ts is not None else tip["timestamp"] + MIN_BLOCK_SPACING_SECONDS + 5
    blk = block_mod.assemble(tip, [], builder, iterations)
    parent = challenge_parent if challenge_parent is not None else tip["hash"]
    out, proof, _ = vdf_mod.evaluate(block_mod.vdf_challenge(parent, builder), iterations)
    blk["vdf_output"], blk["vdf_proof"] = out, proof
    blk["vdf_iterations"] = iterations
    blk["hash"] = block_mod.block_hash(blk)
    return blk


def grow(chain, builder, clock, n):
    chain = list(chain)
    for _ in range(n):
        chain.append(build_on(chain, builder, clock))
    return chain


class Liar:
    """Fake UDP endpoint for one peer. `claim` is what INFO says; `serve`
    is the chain GETSYNC actually answers from (callable so a test can make
    it grow as it is asked)."""

    def __init__(self, serve, claim_height=None, claim_work=None, claim_tip=None):
        self.serve = serve
        self.claim_height, self.claim_work, self.claim_tip = claim_height, claim_work, claim_tip
        self.info_calls = 0
        self.sync_calls = []

    def get_info(self, peer, timeout=8.0):
        self.info_calls += 1
        chain = self.serve()
        return {"height": self.claim_height if self.claim_height is not None else len(chain) - 1,
                "tip_hash": self.claim_tip or chain[-1]["hash"],
                "work": (self.claim_work if self.claim_work is not None
                         else sum(b.get("vdf_iterations", 0) for b in chain)),
                "version": "test"}

    def request_sync(self, peer, from_h, to_h, timeout=None):
        self.sync_calls.append((from_h, to_h))
        chain = self.serve()
        return {"genesis": "test", "chain": chain[from_h:to_h + 1]}


def run_sync(node, liar):
    syncer = Syncer(pool=type("P", (), {"random": lambda s: "9.9.9.9:1",
                                        "update_info": lambda s, *a, **k: None})(),
                    udp=liar)
    adopted = syncer.check_and_sync(
        node.cs.chain, node._sync_page_outcome, peer="9.9.9.9:1",
        local_work=node.cs.cumulative_iterations, max_pages=50, budget=30)
    return adopted, syncer


def adopt(node, chain):
    ok, err = node.apply_better_chain(chain)
    assert ok, err


def test_shorter_chain_with_more_proven_work_wins_and_longer_with_less_loses(node_env, clock):
    """Fork choice is proven work, never height: a 4-block chain whose blocks
    each cost double beats a 6-block one, and the reverse does not happen."""
    node = node_env[0]
    longer = grow([node.cs.chain[0]], HONEST, clock, 6)             # 6 * ITERS
    adopt(node, longer)
    heavy = grow([node.cs.chain[0]], HEAVY, clock, 4)               # ITERS + 3 * 2 * ITERS
    assert sum(b["vdf_iterations"] for b in heavy[1:]) > sum(b["vdf_iterations"] for b in longer[1:])
    adopted, _ = run_sync(node, Liar(lambda: heavy))
    assert adopted and node.cs.height == 4 and node.cs.tip["hash"] == heavy[-1]["hash"]
    adopted, _ = run_sync(node, Liar(lambda: longer, claim_work=10 ** 12))
    assert not adopted and node.cs.tip["hash"] == heavy[-1]["hash"]


def test_honest_longer_chain_is_adopted(node_env, clock):
    node = node_env[0]
    honest = grow([node.cs.chain[0]], HONEST, clock, 6)
    adopted, _ = run_sync(node, Liar(lambda: honest))
    assert adopted and node.cs.height == 6


def test_huge_claim_with_forged_proofs_is_rejected_and_stops_early(node_env, clock):
    node = node_env[0]
    real = grow([node.cs.chain[0]], ATTACKER, clock, 3)
    forged = [dict(b) for b in real]
    for b in forged[1:]:
        b["vdf_proof"] = "00" * (len(b["vdf_proof"]) // 2)
        b["hash"] = block_mod.block_hash(b)
    # re-chain the forged hashes
    for i in range(2, len(forged)):
        forged[i]["previous_hash"] = forged[i - 1]["hash"]
        forged[i]["hash"] = block_mod.block_hash(forged[i])
    liar = Liar(lambda: forged, claim_height=10_000, claim_work=10 ** 12)
    adopted, _ = run_sync(node, liar)
    assert not adopted and node.cs.height == 0
    assert len(liar.sync_calls) <= 3, "must stop at the first bad page, not walk to the claimed tip"


def test_inflated_work_claim_does_not_beat_less_real_work(node_env, clock):
    node = node_env[0]
    ours = grow([node.cs.chain[0]], HONEST, clock, 6)
    adopt(node, ours)
    weaker = grow([node.cs.chain[0]], ATTACKER, clock, 4)       # genuinely less work
    liar = Liar(lambda: weaker, claim_height=50, claim_work=10 ** 12)
    adopted, _ = run_sync(node, liar)
    assert not adopted and node.cs.height == 6 and node.cs.tip["hash"] == ours[-1]["hash"]


def test_claim_is_not_held_until_blocks_exist(node_env, clock):
    """The "claim a longer chain, build the tail by the time they ask" plan:
    the attacker really does hold only 3 blocks when it claims 10, and the
    block after its tip cannot be produced for a parent that does not exist
    yet. When asked past what it has, it has nothing valid to send."""
    node = node_env[0]
    ours = grow([node.cs.chain[0]], HONEST, clock, 6)
    adopt(node, ours)
    held = grow([node.cs.chain[0]], ATTACKER, clock, 3)
    liar = Liar(lambda: held, claim_height=10, claim_work=ITERS * 10)
    adopted, _ = run_sync(node, liar)
    assert not adopted and node.cs.tip["hash"] == ours[-1]["hash"]


def test_attacker_that_really_builds_in_time_is_just_mining(node_env, clock):
    """If the attacker does compute the blocks while we fetch, the chain is
    real and heavier, and adopting it is correct. Nothing was faked."""
    node = node_env[0]
    ours = grow([node.cs.chain[0]], HONEST, clock, 4)
    adopt(node, ours)
    fork = grow([node.cs.chain[0]], ATTACKER, clock, 4)           # equal work, other builder
    state = {"chain": fork}

    def serve():
        if len(state["chain"]) < 8:                                # keeps building as asked
            state["chain"] = grow(state["chain"], ATTACKER, clock, 1)
        return state["chain"]

    liar = Liar(serve, claim_height=7)
    adopted, _ = run_sync(node, liar)
    assert adopted and node.cs.height >= 5
    assert node.cs.cumulative_iterations > ITERS * 4              # more PROVEN work than ours


def test_block_computed_on_the_wrong_parent_is_rejected(node_env, clock):
    """Precomputing: a VDF evaluated for a guessed parent does not verify
    once the real parent hash is known."""
    node = node_env[0]
    chain = grow([node.cs.chain[0]], ATTACKER, clock, 2)
    forged = list(chain) + [build_on(chain, ATTACKER, clock, challenge_parent="ab" * 32)]
    adopted, _ = run_sync(node, Liar(lambda: forged))
    assert not adopted and node.cs.height == 0


def test_lowered_iteration_count_is_rejected(node_env, clock):
    """Claiming less work per block than the chain requires (what padded
    timestamps are meant to buy) fails the exact-iterations check."""
    node = node_env[0]
    chain = grow([node.cs.chain[0]], ATTACKER, clock, 2)
    cheap = list(chain) + [build_on(chain, ATTACKER, clock, iterations=ITERS // 2)]
    adopted, _ = run_sync(node, Liar(lambda: cheap))
    assert not adopted and node.cs.height == 0


def test_future_timestamp_is_rejected(node_env, clock):
    node = node_env[0]
    chain = grow([node.cs.chain[0]], ATTACKER, clock, 2)
    now = chain[-1]["timestamp"]
    early = build_on(chain, ATTACKER, clock, ts=now + 100)         # stamped 100s ahead...
    clock[0] = now + 5                                             # ...of the real clock
    ok, err = block_mod.validate(early, node.cs.state.snapshot(), chain)
    assert not ok and "future" in err
    adopted, _ = run_sync(node, Liar(lambda: list(chain) + [early]))
    assert node.cs.tip["hash"] != early["hash"]


def test_equal_work_claim_costs_a_bounded_fetch_and_changes_nothing_if_not_better(node_env, clock):
    node = node_env[0]
    ours = grow([node.cs.chain[0]], HONEST, clock, 4)
    adopt(node, ours)
    twin = grow([node.cs.chain[0]], ATTACKER, clock, 4)
    liar = Liar(lambda: twin)
    run_sync(node, liar)
    assert node.cs.cumulative_iterations == ITERS * 4
    assert len(liar.sync_calls) < 20


# ---- we are at 5, the peer claims 6, and builds its 6th while we fetch ----

def _lazy_builder(chain, builder, clock):
    """Serves `chain` but computes the next block only once it is first
    asked for, i.e. while the victim is already fetching."""
    state = {"chain": chain, "built": 0}

    def serve():
        return state["chain"]

    def on_ask(from_h, to_h):
        if to_h >= len(state["chain"]) and state["built"] == 0:
            state["chain"] = grow(state["chain"], builder, clock, 1)
            state["built"] += 1
    return state, serve, on_ask


class _LazyLiar(Liar):
    def __init__(self, serve, on_ask, **kw):
        super().__init__(serve, **kw)
        self.on_ask = on_ask

    def request_sync(self, peer, from_h, to_h, timeout=None):
        self.on_ask(from_h, to_h)
        return super().request_sync(peer, from_h, to_h, timeout)


def test_claims_6_on_our_own_5_and_builds_the_6th_while_we_fetch(node_env, clock):
    """Same history as ours, so the 6th is simply the next block, computed
    for real after the claim. Adopting it is correct: it is a valid block
    on our tip."""
    node = node_env[0]
    five = grow([node.cs.chain[0]], HONEST, clock, 5)
    adopt(node, five)
    state, serve, on_ask = _lazy_builder(list(five), ATTACKER, clock)
    liar = _LazyLiar(serve, on_ask, claim_height=6, claim_work=ITERS * 6)
    adopted, _ = run_sync(node, liar)
    assert adopted and node.cs.height == 6
    assert node.cs.tip["builder"] == ATTACKER and node.cs.chain[:6] == five


def test_claims_6_but_cannot_deliver_it_changes_nothing(node_env, clock):
    node = node_env[0]
    five = grow([node.cs.chain[0]], HONEST, clock, 5)
    adopt(node, five)
    liar = Liar(lambda: five, claim_height=6, claim_work=ITERS * 6)   # never builds it
    adopted, _ = run_sync(node, liar)
    assert not adopted and node.cs.chain == five


def test_claims_6_on_a_different_5_then_builds_6_is_a_real_heavier_chain(node_env, clock):
    """The peer's first five blocks are NOT ours (a rival fork), and it
    builds block 6 during our fetch. At that point it genuinely holds more
    proven work than we do, so taking it is right: it paid for 6 blocks to
    our 5. Nothing was faked; it only had to be real by the time we looked."""
    node = node_env[0]
    ours = grow([node.cs.chain[0]], HONEST, clock, 5)
    adopt(node, ours)
    rival5 = grow([node.cs.chain[0]], ATTACKER, clock, 5)
    state, serve, on_ask = _lazy_builder(list(rival5), ATTACKER, clock)
    liar = _LazyLiar(serve, on_ask, claim_height=6, claim_work=ITERS * 6)
    adopted, _ = run_sync(node, liar)
    assert adopted and node.cs.height == 6
    assert node.cs.cumulative_iterations == ITERS * 6
    assert node.cs.chain[1]["hash"] == rival5[1]["hash"]


def _rival_tip(base, ours_tip, clock):
    """Another valid block at the same height as ours_tip, from some other
    builder, whose vdf_output sorts LOWER (it would win the draw)."""
    for i in range(10, 400):
        cand = build_on(base, address(i), clock)
        if block_mod.tie_break_key(cand) < block_mod.tie_break_key(ours_tip):
            return cand
    raise AssertionError("no lower-output rival found")


def test_late_sibling_via_sync_is_refused_once_the_draw_window_has_closed(node_env, clock):
    """A same-height rival with a lower output, delivered by sync instead of
    gossip, is treated like the gossiped block it is: only while that
    height's draw is open."""
    node = node_env[0]
    four = grow([node.cs.chain[0]], HONEST, clock, 4)
    ours5 = build_on(four, HONEST, clock)
    adopt(node, four + [ours5])
    rival5 = _rival_tip(four, ours5, clock)
    node._draw_height, node._draw_closes = 5, 0.0                  # window long closed
    adopted, _ = run_sync(node, Liar(lambda: four + [rival5]))
    assert not adopted and node.cs.tip["hash"] == ours5["hash"]


def test_sibling_via_sync_is_taken_while_the_draw_window_is_open(node_env, clock):
    import time as _time
    node = node_env[0]
    four = grow([node.cs.chain[0]], HONEST, clock, 4)
    ours5 = build_on(four, HONEST, clock)
    adopt(node, four + [ours5])
    rival5 = _rival_tip(four, ours5, clock)
    node.open_draw(5)
    assert node._draw_is_open(5)
    adopted, _ = run_sync(node, Liar(lambda: four + [rival5]))
    assert adopted and node.cs.tip["hash"] == rival5["hash"]


def test_sibling_via_sync_with_a_higher_output_never_wins_even_in_the_window(node_env, clock):
    node = node_env[0]
    four = grow([node.cs.chain[0]], HONEST, clock, 4)
    ours5 = build_on(four, HONEST, clock)
    adopt(node, four + [ours5])
    worse = None
    for i in range(10, 400):
        cand = build_on(four, address(i), clock)
        if block_mod.tie_break_key(cand) > block_mod.tie_break_key(ours5):
            worse = cand
            break
    node.open_draw(5)
    adopted, _ = run_sync(node, Liar(lambda: four + [worse]))
    assert not adopted and node.cs.tip["hash"] == ours5["hash"]


def test_that_late_sibling_loses_once_anyone_has_built_on_ours(node_env, clock):
    node = node_env[0]
    four = grow([node.cs.chain[0]], HONEST, clock, 4)
    ours5 = build_on(four, HONEST, clock)
    ours6 = build_on(four + [ours5], HONEST, clock)
    adopt(node, four + [ours5, ours6])
    rival5 = _rival_tip(four, ours5, clock)
    adopted, _ = run_sync(node, Liar(lambda: four + [rival5], claim_work=ITERS * 5))
    assert not adopted and node.cs.tip["hash"] == ours6["hash"]
