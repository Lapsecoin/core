"""Fee requests: networks, amounts, memos, signatures, window and order."""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import evm
import gas
from nacl.signing import SigningKey

KEY = (7).to_bytes(32, "big")
ADDR = evm.address_from_secret(KEY)
BASE = gas.NETWORKS["base"]
ETH = gas.NETWORKS["ethereum"]
SOL = gas.NETWORKS["solana"]
TXID = "ab" * 32


def evm_request(target=10 ** 15, net="ethereum", nonce=4, lapse="aa" * 20, key=KEY):
    dest = evm.address_from_secret(key)
    sig = evm.sign_message(gas.request_message(net, target, dest, lapse, nonce), key)
    return gas.build_request_memo(net, target, dest, sig), dest


class TestTables:
    def test_every_network_is_complete_and_unique(self):
        ids = [n.chain_id for n in gas.NETWORKS.values()]
        assert len(ids) == len(set(ids))
        for slug, n in gas.NETWORKS.items():
            assert n.slug == slug and n.decimals in (9, 18) and n.vm in ("evm", "svm")
            assert n.overhead_usd < gas.NODE_CAP_USD and n.explorer.startswith("https://")

    def test_base_is_free_to_pay_because_it_is_the_funding_chain(self):
        assert BASE.overhead_usd == 0.0 and BASE.chain_id == evm.BASE_CHAIN_ID

    def test_every_action_has_a_need_on_both_vms(self):
        for a in gas.ACTIONS.values():
            assert a.evm_gas > 0 and a.svm_lamports > 0

    def test_unknown_network_is_none(self):
        assert gas.network("dogechain") is None and gas.network("base") is BASE


class TestAddresses:
    def test_evm(self):
        assert gas.is_valid_address(ETH, ADDR) and not gas.is_valid_address(ETH, "0x12")
        assert gas.same_address(ETH, ADDR, ADDR.lower())

    def test_solana(self):
        good = "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK"
        assert gas.is_valid_address(SOL, good)
        assert gas.is_valid_address(SOL, "11111111111111111111111111111111")
        assert not gas.is_valid_address(SOL, ADDR)
        assert not gas.is_valid_address(SOL, good + "0")        # 0 is not base58
        assert not gas.is_valid_address(SOL, good[:-3])
        assert not gas.same_address(SOL, good, good.lower())

    @pytest.mark.parametrize("junk", [None, 3, b"x", ""])
    def test_junk_is_never_valid(self, junk):
        assert not gas.is_valid_address(ETH, junk) and not gas.is_valid_address(SOL, junk)


class TestAmounts:
    PRICE = 2500.0

    def test_need_is_gas_units_times_price_with_headroom(self):
        a = gas.ACTIONS["send"]
        assert gas.needed_units(ETH, a, 10 ** 9) == int(65_000 * 10 ** 9 * gas.GAS_SAFETY)
        assert gas.needed_units(SOL, a, gas_price=999) == a.svm_lamports

    def test_ceiling_leaves_room_for_the_route(self):
        assert gas.payout_cap_usd(BASE) == gas.NODE_CAP_USD
        assert gas.payout_cap_usd(gas.NETWORKS["polygon"]) == pytest.approx(
            gas.NODE_CAP_USD - gas.NETWORKS["polygon"].overhead_usd)

    def test_a_cheap_chain_is_covered_in_full(self):
        # about 0.001 gwei x 65k units: well under a cent
        p = gas.plan(gas.NETWORKS["arbitrum"], gas.ACTIONS["send"],
                     gas_price=10 ** 7, price_usd=self.PRICE, balance=0)
        assert p["needs_help"] and p["covers_share"] == 1.0
        assert p["deliver_usd"] == pytest.approx(gas.FLOOR_USD, rel=0.01)   # raised to the floor

    def test_an_expensive_chain_is_capped_and_says_how_much_it_covers(self):
        p = gas.plan(ETH, gas.ACTIONS["approve_swap"], gas_price=40 * 10 ** 9,
                     price_usd=self.PRICE, balance=0)
        assert p["target_usd"] > gas.NODE_CAP_USD
        assert p["deliver_usd"] == pytest.approx(gas.payout_cap_usd(ETH), rel=1e-6)
        assert 0 < p["covers_share"] < 1

    def test_what_the_address_already_holds_counts(self):
        a = gas.ACTIONS["send"]
        full = gas.plan(ETH, a, gas_price=10 ** 10, price_usd=self.PRICE, balance=0)
        held = full["target"] // 2
        half = gas.plan(ETH, a, gas_price=10 ** 10, price_usd=self.PRICE, balance=held)
        cap = gas.usd_to_units(ETH, gas.payout_cap_usd(ETH), self.PRICE)
        assert full["deliver"] == cap
        assert half["shortfall"] == full["target"] - held
        assert half["deliver"] == cap - held < full["deliver"]

    def test_an_address_with_enough_is_not_helped(self):
        a = gas.ACTIONS["send"]
        t = gas.needed_units(ETH, a, 10 ** 9)
        p = gas.plan(ETH, a, gas_price=10 ** 9, price_usd=self.PRICE, balance=t)
        assert not p["needs_help"] and p["deliver"] == 0 and p["covers_share"] == 1.0

    def test_payout_is_the_shortfall_between_the_floor_and_the_ceiling(self):
        floor = gas.usd_to_units(ETH, gas.FLOOR_USD, self.PRICE)             # 1e14
        cap = gas.usd_to_units(ETH, gas.payout_cap_usd(ETH), self.PRICE)     # 7.8e14
        assert (floor, cap) == (10 ** 14, 780_000_000_000_000)
        assert gas.payout_units(ETH, 5 * 10 ** 14, 0, self.PRICE) == 5 * 10 ** 14
        assert gas.payout_units(ETH, 10 ** 16, 0, self.PRICE) == cap
        assert gas.payout_units(ETH, 5 * 10 ** 14, 3 * 10 ** 14, self.PRICE) == 2 * 10 ** 14
        assert gas.payout_units(ETH, 5 * 10 ** 14, 44 * 10 ** 13, self.PRICE) == floor

    def test_a_destination_close_enough_is_left_alone(self):
        assert gas.payout_units(ETH, 5 * 10 ** 14, 45 * 10 ** 13, self.PRICE) == 0
        assert gas.payout_units(ETH, 5 * 10 ** 14, 5 * 10 ** 14, self.PRICE) == 0

    def test_a_second_node_sees_the_first_nodes_payment_and_stands_down(self):
        for target in (3 * 10 ** 14, 5 * 10 ** 14, 10 ** 16, 10 ** 20):
            first = gas.payout_units(ETH, target, 0, self.PRICE)
            assert first > 0
            assert gas.payout_units(ETH, target, first, self.PRICE) == 0, target

    def test_a_need_beyond_the_ceiling_is_paid_once_not_once_per_rank(self):
        cap = gas.usd_to_units(ETH, gas.payout_cap_usd(ETH), self.PRICE)
        balance = 0
        for _ in range(gas.MAX_RANKS):
            balance += gas.payout_units(ETH, 10 ** 20, balance, self.PRICE)
        assert balance == cap

    def test_an_address_already_holding_a_ceiling_is_not_helped_however_much_it_asks(self):
        cap = gas.usd_to_units(ETH, gas.payout_cap_usd(ETH), self.PRICE)
        assert gas.payout_units(ETH, 10 ** 20, cap, self.PRICE) == 0

    def test_usd_round_trip(self):
        u = gas.usd_to_units(SOL, 1.0, 100.0)
        assert u == 10 ** 7 and gas.units_to_usd(SOL, u, 100.0) == pytest.approx(1.0)


class TestRequestMemo:
    def test_round_trip_and_signature(self):
        memo, dest = evm_request()
        req = gas.parse_request_memo(memo)
        assert req["network"] == "ethereum" and req["dest"] == dest and req["target"] == 10 ** 15
        assert gas.verify_request_signature(req, "aa" * 20, 4)

    def test_fits_in_a_memo(self):
        import tx
        memo, _ = evm_request(target=10 ** 30)
        assert len(memo.encode()) <= tx.MAX_MEMO_BYTES

    def test_signature_is_bound_to_sender_and_nonce_and_fields(self):
        memo, _ = evm_request()
        req = gas.parse_request_memo(memo)
        assert not gas.verify_request_signature(req, "bb" * 20, 4)      # another sender
        assert not gas.verify_request_signature(req, "aa" * 20, 5)      # another nonce
        assert not gas.verify_request_signature({**req, "target": req["target"] + 1}, "aa" * 20, 4)
        assert not gas.verify_request_signature({**req, "network": "base"}, "aa" * 20, 4)

    def test_someone_elses_signature_does_not_prove_the_address(self):
        other = (9).to_bytes(32, "big")
        dest = evm.address_from_secret(KEY)
        sig = evm.sign_message(gas.request_message("ethereum", 5, dest, "aa" * 20, 1), other)
        req = gas.parse_request_memo(gas.build_request_memo("ethereum", 5, dest, sig))
        assert not gas.verify_request_signature(req, "aa" * 20, 1)

    def test_solana_round_trip(self):
        sk = SigningKey.generate()
        dest = _b58encode(bytes(sk.verify_key))
        msg = gas.request_message("solana", 2_050_000, dest, "aa" * 20, 2)
        memo = gas.build_request_memo("solana", 2_050_000, dest, sk.sign(msg).signature)
        req = gas.parse_request_memo(memo)
        assert req and gas.verify_request_signature(req, "aa" * 20, 2)
        assert not gas.verify_request_signature(req, "aa" * 20, 3)
        assert len(memo.encode()) <= 200

    @pytest.mark.parametrize("memo", [
        None, "", "[gas]", "[gas] ", "[gas] ethereum", "hello",
        "[gas] dogechain 5 " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
        "[gas] ethereum 0 " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
        "[gas] ethereum -5 " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
        "[gas] ethereum 5 0x12 " + base64.b64encode(b"x" * 65).decode(),
        "[gas] ethereum 5 " + ADDR + " not-base64!!",
        "[gas] ethereum 5 " + ADDR + " " + base64.b64encode(b"x" * 64).decode(),
        "[gas] ethereum 5 " + ADDR + " " + base64.b64encode(b"x" * 65).decode() + " extra",
        "[gas] ethereum " + "9" * 40 + " " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
    ])
    def test_malformed_memos_are_not_requests(self, memo):
        assert gas.parse_request_memo(memo) is None


class TestClaimMemo:
    def test_round_trip_and_signature(self):
        msg = gas.claim_message(gas.request_ref(TXID), "cc" * 20)
        memo = gas.build_claim_memo(TXID, ADDR, evm.sign_message(msg, KEY))
        claim = gas.parse_claim_memo(memo)
        assert claim["ref"] == TXID[:gas.REF_LEN] and claim["base_addr"] == ADDR
        assert gas.verify_claim_signature(claim, "cc" * 20)
        assert not gas.verify_claim_signature(claim, "dd" * 20)
        assert len(memo.encode()) <= 200

    def test_a_claim_for_a_different_request_does_not_verify(self):
        msg = gas.claim_message(gas.request_ref("cd" * 32), "cc" * 20)
        memo = gas.build_claim_memo(TXID, ADDR, evm.sign_message(msg, KEY))
        assert not gas.verify_claim_signature(gas.parse_claim_memo(memo), "cc" * 20)

    @pytest.mark.parametrize("memo", [
        None, "", "[gas-claim] ", "[gas-claim] zz " + ADDR + " AAAA",
        "[gas-claim] " + TXID[:23] + " " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
        "[gas-claim] " + TXID[:24] + " 0x12 " + base64.b64encode(b"x" * 65).decode(),
        "[gas-claim] " + TXID[:24] + " " + ADDR + " " + base64.b64encode(b"x" * 10).decode(),
        "[gas-claim] " + TXID[:24].upper() + " " + ADDR + " " + base64.b64encode(b"x" * 65).decode(),
    ])
    def test_malformed_claims_are_ignored(self, memo):
        assert gas.parse_claim_memo(memo) is None


class TestWindowAndOrder:
    def test_no_claim_waits_the_whole_window(self):
        assert gas.window_close(100) == 100 + gas.CLAIM_WINDOW_BLOCKS

    def test_a_claim_closes_the_window_one_block_later(self):
        assert gas.window_close(100, 101) == 102
        assert gas.window_close(100, 103) == 104

    def test_a_late_claim_never_extends_the_window(self):
        assert gas.window_close(100, 105) == 105
        assert gas.window_close(100, 110) == 105

    def test_order_is_the_same_whatever_order_claims_arrive_in(self):
        claimers = [f"node{i}" for i in range(10)]
        a = gas.order_claimers(TXID, "h" * 64, claimers)
        b = gas.order_claimers(TXID, "h" * 64, list(reversed(claimers)))
        assert a == b and len(a) == gas.MAX_RANKS and len(set(a)) == gas.MAX_RANKS

    def test_order_depends_on_the_request_and_the_closing_block(self):
        claimers = [f"node{i}" for i in range(10)]
        base = gas.order_claimers(TXID, "h" * 64, claimers)
        assert gas.order_claimers("cd" * 32, "h" * 64, claimers) != base
        assert gas.order_claimers(TXID, "k" * 64, claimers) != base

    def test_a_node_claiming_twice_counts_once(self):
        assert gas.order_claimers(TXID, "h", ["a", "a", "a"]) == ["a"]

    def test_nobody_claimed(self):
        assert gas.order_claimers(TXID, "h", []) == []

    def test_turns_follow_the_closing_block_in_slots(self):
        assert [gas.turn_start(200, i) for i in range(3)] == [201, 203, 205]

    def test_ranks_are_spread_fairly(self):
        wins = {}
        for i in range(600):
            first = gas.order_claimers(f"{i:064x}", "h" * 64, ["a", "b", "c"])[0]
            wins[first] = wins.get(first, 0) + 1
        assert all(120 < n < 280 for n in wins.values()) and len(wins) == 3


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = gas._B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out
