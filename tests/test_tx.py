"""
Unit tests for tx.py: the plaintext transaction format.

Covers: create, tx_hash, tx_size, tx_size_in_block, validate (fields/
outputs, signature, nonce, balance checks), and board_fee_floor, the one
protocol-enforced fee formula (ordinary sends are otherwise sender-bid).

All tests are pure and local. No network, no chain, no disk.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto
import tx as tx_mod
import state as state_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import keypair, address, pubkey_hex, make_tx, seed_balance


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. Transaction creation
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_returns_dict_with_required_fields(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        for field in ["from", "pubkey", "outputs", "nonce", "fee", "signature"]:
            assert field in t

    def test_signature_is_hex_string(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert isinstance(t["signature"], str)
        bytes.fromhex(t["signature"])  # must not raise

    def test_pubkey_is_hex_string(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert isinstance(t["pubkey"], str)
        bytes.fromhex(t["pubkey"])

    def test_from_address_matches_pubkey(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        pk_bytes = bytes.fromhex(t["pubkey"])
        expected_addr = crypto.public_key_to_address(pk_bytes)
        assert t["from"] == expected_addr

    def test_nonce_increments(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert t2["nonce"] == t1["nonce"] + 1


# ---------------------------------------------------------------------------
# 2. tx_hash
# ---------------------------------------------------------------------------

class TestTxHash:
    def test_hash_returns_64_char_hex(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h = tx_mod.tx_hash(t)
        assert isinstance(h, str) and len(h) == 64

    def test_hash_is_deterministic(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_hash(t) == tx_mod.tx_hash(t)

    def test_different_txs_have_different_hashes(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_hash(t1) != tx_mod.tx_hash(t2)

    def test_hash_excludes_signature(self):
        """tx_hash covers the signed content only, not the signature itself:
        Falcon signing draws fresh randomness each time, so a re-signed
        identical tx must still hash identically (same logical tx, same
        id) even though its signature bytes differ."""
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h1 = tx_mod.tx_hash(t)
        t2 = dict(t)
        t2["signature"] = "00" * 100
        h2 = tx_mod.tx_hash(t2)
        assert h1 == h2


# ---------------------------------------------------------------------------
# 3. tx_size and tx_size_in_block
# ---------------------------------------------------------------------------

class TestTxSize:
    def test_tx_size_excludes_signature(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        size = tx_mod.tx_size(t)
        assert isinstance(size, int) and size > 0
        fields_no_sig = {k: v for k, v in t.items() if k != "signature"}
        raw_size = len(crypto.canonical_json(fields_no_sig))
        assert size == raw_size

    def test_tx_size_in_block_first_position_no_comma(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s0 = tx_mod.tx_size_in_block(t, position=0)
        s1 = tx_mod.tx_size_in_block(t, position=1)
        assert s1 == s0 + 1  # comma added for non-first

    def test_tx_size_positive(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_size(t) > 0


# ---------------------------------------------------------------------------
# 4. validate, field / output checks
# ---------------------------------------------------------------------------

class TestValidateFields:
    def test_valid_tx_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_missing_from_field_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        del t["from"]
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "missing field" in err

    def test_empty_outputs_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["outputs"] = []
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_output_with_zero_amount_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, 0, s, outputs_override=[{"to": address(1), "amount": 0}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_output_with_negative_amount_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, -1, s, outputs_override=[{"to": address(1), "amount": -1}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_invalid_recipient_address_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s,
                    outputs_override=[{"to": "not_an_address", "amount": TICKS_PER_LAPSE}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "invalid address" in err

    def test_negative_fee_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["fee"] = -1
        ok, err = tx_mod.validate(t, s)
        assert ok is False


# ---------------------------------------------------------------------------
# 4b. validate, field whitelist and memo
#
# Nothing before this ever checked for an *unexpected* field, only that
# the required ones were present, so a sender could attach an arbitrarily
# large field under any name and it would validate fine. That made the
# memo cap below pointless on its own: capping one named field is no
# defense while any other name is still wide open.
# ---------------------------------------------------------------------------

class TestValidateFieldWhitelist:
    def test_unexpected_field_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["junk"] = "A" * 5_000_000
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "unexpected field" in err

    def test_memo_field_alone_is_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="thanks for lunch")
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_no_memo_key_when_blank(self):
        """create() omits the key entirely rather than storing "", so a
        transaction built without a memo is byte-for-byte what it always
        was, unaffected by this feature existing at all."""
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert "memo" not in t

    def test_memo_over_cap_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="x" * (tx_mod.MAX_MEMO_BYTES + 1))
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo exceeds" in err

    def test_memo_exactly_at_cap_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="x" * tx_mod.MAX_MEMO_BYTES)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_memo_cap_counts_utf8_bytes_not_characters(self):
        """A multi-byte character can cost several bytes, so the cap has to
        be checked after UTF-8 encoding, not against len(memo)."""
        s = fresh_state()
        seed_balance(s, 0)
        # 4 bytes each in UTF-8 (outside the BMP), so this is one
        # character over the cap in character count but well over it in
        # the bytes actually being checked.
        char_count = tx_mod.MAX_MEMO_BYTES // 4 + 1
        memo = "\U0001F600" * char_count
        assert char_count < tx_mod.MAX_MEMO_BYTES  # sanity: not over by length alone
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo=memo)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo exceeds" in err

    def test_memo_null_byte_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="hi\x00there")
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "null byte" in err

    def test_non_string_memo_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["memo"] = 12345
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo must be a string" in err

    def test_outputs_within_cap_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        outputs = [{"to": address(1), "amount": 1} for _ in range(tx_mod.MAX_OUTPUTS)]
        t = make_tx(0, 1, 0, s, outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_too_many_outputs_rejected(self):
        """Count, not just content, has to be bounded: each output only
        needs a valid address and a positive amount, so a wall of 1-tick
        outputs costs almost nothing in real balance while still bloating
        the transaction, unlike the whitelist above this doesn't defend
        against."""
        s = fresh_state()
        seed_balance(s, 0)
        outputs = [{"to": address(1), "amount": 1} for _ in range(tx_mod.MAX_OUTPUTS + 1)]
        t = make_tx(0, 1, 0, s, outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "too many outputs" in err


# ---------------------------------------------------------------------------
# 5. validate, signature check
# ---------------------------------------------------------------------------

class TestValidateSignature:
    def test_wrong_pubkey_for_address_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        _, pk2 = keypair(2)
        t["pubkey"] = pk2.hex()
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "pubkey" in err or "address" in err

    def test_tampered_signature_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["signature"] = "00" * 752
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_non_hex_signature_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["signature"] = 12345
        ok, err = tx_mod.validate(t, s)
        assert ok is False


# ---------------------------------------------------------------------------
# 6. validate, nonce check (sequential, per sender)
# ---------------------------------------------------------------------------

class TestValidateNonce:
    def test_correct_nonce_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_nonce_too_high_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, nonce_override=5)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "nonce" in err

    def test_nonce_already_used_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t)
        # Replay the same tx (same nonce)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "nonce" in err


# ---------------------------------------------------------------------------
# 7. validate, balance check (outputs + fee <= balance)
# ---------------------------------------------------------------------------

class TestValidateBalance:
    def test_insufficient_balance_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 0.001)  # nearly nothing
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "insufficient" in err

    def test_exact_balance_passes(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from_addr = address(0)
        bal = s.get_balance(from_addr)
        outputs = [{"to": address(1), "amount": bal}]
        sk, _ = keypair(0)
        t = tx_mod.create(from_addr, pubkey_hex(0), outputs, 1, 0, sk)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 9. validate: the burn address can never be a sender (see
#    _check_not_from_burn_address). Not left to rest on "nobody could ever
#    forge a signature for it": that's still true (burn_address() isn't
#    derived from any key), but this check makes it unconditional, so it
#    holds even if a signature somehow verified anyway.
# ---------------------------------------------------------------------------

class TestBurnAddressCannotSpend:
    def test_rejected_before_signature_is_even_checked(self):
        """A garbage signature must still fail with the burn-address
        message specifically, proving this check runs first and doesn't
        depend on the signature verifying at all."""
        s = fresh_state()
        outputs = [{"to": address(1), "amount": 1}]
        t = {"from": crypto.burn_address(), "pubkey": pubkey_hex(0),
             "outputs": outputs, "nonce": 1, "fee": 0, "signature": "00" * 10}
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "burn address" in err

    def test_rejected_even_with_a_real_valid_signature(self):
        """The stronger claim: even a tx some keypair genuinely signed
        (from field set to the burn address regardless of whose key it
        really is) is still rejected. Nothing about signature validity
        can ever make this pass."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        # Funding the burn address itself so this can't also be read as
        # merely an insufficient-balance failure.
        s.credit(crypto.burn_address(), TICKS_PER_LAPSE)
        outputs = [{"to": address(1), "amount": 1}]
        sk, _ = keypair(0)
        t = tx_mod.create(crypto.burn_address(), pubkey_hex(0), outputs, 1, 0, sk)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "burn address" in err

    def test_ordinary_addresses_are_unaffected(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, 1, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_fee_included_in_required_balance(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from_addr = address(0)
        bal = s.get_balance(from_addr)
        outputs = [{"to": address(1), "amount": bal}]
        sk, _ = keypair(0)
        t = tx_mod.create(from_addr, pubkey_hex(0), outputs, 1, 1, sk)  # fee=1 tips it over
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "insufficient" in err


# ---------------------------------------------------------------------------
# 8. board_fee_floor: the geometric, uncapped staircase (see tx.py's own
#    comment on BOARD_BASE_FEE/BOARD_STEP_SIZE/BOARD_FEE_RATIO for why
#    floor(0) is pinned at exactly 1 -- any post already on chain was
#    validated at some total_board_posts <= what it is now, and the very
#    first board post ever made was validated at total_board_posts == 0,
#    so this is the one value a future formula change can never move
#    without retroactively invalidating that post on replay from genesis.
# ---------------------------------------------------------------------------

class TestBoardFeeFloor:
    def test_floor_at_zero_posts_is_exactly_one(self):
        """Pinned, not incidental: see the class docstring above. A change
        that breaks this breaks replaying the existing chain."""
        assert tx_mod.board_fee_floor(0) == 1

    def test_floor_stays_flat_within_a_step(self):
        assert (tx_mod.board_fee_floor(0)
                == tx_mod.board_fee_floor(tx_mod.BOARD_STEP_SIZE - 1))

    def test_floor_multiplies_by_the_ratio_at_each_step_boundary(self):
        base = tx_mod.board_fee_floor(0)
        step_size = tx_mod.BOARD_STEP_SIZE
        for step in range(1, 5):
            assert (tx_mod.board_fee_floor(step * step_size)
                    == base * tx_mod.BOARD_FEE_RATIO ** step)

    def test_floor_is_never_negative_or_zero(self):
        for n in (0, 1, tx_mod.BOARD_STEP_SIZE, 10 ** 6):
            assert tx_mod.board_fee_floor(n) >= 1

    def test_floor_is_monotonically_non_decreasing(self):
        prev = tx_mod.board_fee_floor(0)
        for n in range(0, tx_mod.BOARD_STEP_SIZE * 6, tx_mod.BOARD_STEP_SIZE // 3):
            cur = tx_mod.board_fee_floor(n)
            assert cur >= prev
            prev = cur

    def test_no_ceiling_arbitrarily_far_out(self):
        """Deliberately uncapped: the board is rationed by price, not by a
        hard post-count cutoff, so there must be no plateau at any point,
        however far out."""
        far = tx_mod.board_fee_floor(tx_mod.BOARD_STEP_SIZE * 50)
        farther = tx_mod.board_fee_floor(tx_mod.BOARD_STEP_SIZE * 51)
        assert farther > far

    def test_validate_enforces_the_floor_for_a_board_post(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        s.total_board_posts = tx_mod.BOARD_STEP_SIZE * 4  # floor > 1 here
        floor = tx_mod.board_fee_floor(s.total_board_posts)
        outputs = [{"to": address(1), "amount": 1}]
        t = make_tx(0, 1, 1, s, fee=floor - 1, memo=tx_mod.BOARD_MEMO_TAG + "hi",
                    outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "board post fee below current floor" in err

    def test_validate_accepts_a_board_post_at_exactly_the_floor(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        s.total_board_posts = tx_mod.BOARD_STEP_SIZE * 4
        floor = tx_mod.board_fee_floor(s.total_board_posts)
        outputs = [{"to": address(1), "amount": 1}]
        t = make_tx(0, 1, 1, s, fee=floor, memo=tx_mod.BOARD_MEMO_TAG + "hi",
                    outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_the_original_pre_formula_post_still_validates_unchanged(self):
        """The concrete compatibility case: a board post made when
        total_board_posts was 0 (the very first one, already on chain)
        must still pass validate() exactly as it did before this formula
        existed, with no activation height and no special-casing."""
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        assert s.total_board_posts == 0
        outputs = [{"to": address(1), "amount": 1}]
        # Whatever a real client actually paid back then: the old
        # BOARD_BASE_FEE (1) plus a byte-rate component. Any fee >= 1
        # must still clear board_fee_floor(0).
        t = make_tx(0, 1, 1, s, fee=250, memo=tx_mod.BOARD_MEMO_TAG + "first post ever",
                    outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err
