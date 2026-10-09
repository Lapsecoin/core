"""The EVM primitives, checked against fixed vectors. The address, personal_sign
signature and EIP-1559 raw transaction below were produced by eth-account for
the same inputs and must match byte for byte."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import evm

KEY_ONE = (1).to_bytes(32, "big")
KEY_ONE_ADDR = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
DEAD = "0x000000000000000000000000000000000000dEaD"

# From eth-account, same inputs as test_transaction_matches_the_reference_encoding.
REFERENCE_TX = "02f87782210507830f4240832dc6c082ea6094000000000000000000000000000000000000dead87038d7ea4c680008649290c1c00ffc080a0aeb9ceb92b7c8bfb2770a1c2a4ac95f9a5fbd2f736b2e4417c645aec52ced60aa03fcec98c2dd7a92cdbef3f238f5bc9aa900d68f78aba70ac6593e88aaf1327f9"
REFERENCE_TX_HASH = "0xce18df3b8b7ae102dc3eadbd4d7a08631e93a9f963e5700004aa89cdb1b0687b"


class TestAddresses:
    def test_known_address_for_key_one(self):
        assert evm.address_from_secret(KEY_ONE) == KEY_ONE_ADDR

    def test_keccak_of_nothing(self):
        assert evm.keccak256(b"").hex() == (
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_generated_keys_are_distinct_and_valid(self):
        (s1, a1), (s2, a2) = evm.generate_keypair(), evm.generate_keypair()
        assert s1 != s2 and a1 != a2
        assert evm.is_valid_address(a1) and evm.address_from_secret(s1) == a1

    def test_checksum_is_enforced_only_on_mixed_case(self):
        assert evm.is_valid_address(KEY_ONE_ADDR)
        assert evm.is_valid_address(KEY_ONE_ADDR.lower())
        assert evm.is_valid_address("0x" + KEY_ONE_ADDR[2:].upper())
        bad = KEY_ONE_ADDR[:-1] + ("a" if KEY_ONE_ADDR[-1] != "a" else "b")
        assert not evm.is_valid_address(bad)

    @pytest.mark.parametrize("junk", [None, 5, "", "0x", "0x12", "7E5F4552091A69125d5DfCb7b8C2659029395Bdf",
                                      "0x" + "g" * 40, "0x" + "a" * 41])
    def test_junk_is_not_an_address(self, junk):
        assert not evm.is_valid_address(junk)

    def test_to_checksum_address(self):
        assert evm.to_checksum_address(KEY_ONE_ADDR.lower()) == KEY_ONE_ADDR
        with pytest.raises(ValueError):
            evm.to_checksum_address("nope")


class TestAmounts:
    def test_round_trip(self):
        assert evm.str_to_wei("0.01") == 10 ** 16
        assert evm.str_to_wei("1") == 10 ** 18
        assert evm.str_to_wei("0.000000000000000001") == 1
        assert evm.wei_to_str(10 ** 16) == "0.010000"
        assert evm.wei_to_str(1, 18) == "0.000000000000000001"

    def test_display_truncates_never_rounds_up(self):
        assert evm.wei_to_str(1999999999999999999, 6) == "1.999999"

    @pytest.mark.parametrize("junk", ["", "abc", "-1", "1e3", "0.1234567890123456789", "1,5"])
    def test_junk_amounts_are_rejected(self, junk):
        with pytest.raises(ValueError):
            evm.str_to_wei(junk)


class TestRlp:
    def test_spec_vectors(self):
        assert evm.rlp_encode(b"dog").hex() == "83646f67"
        assert evm.rlp_encode([b"cat", b"dog"]).hex() == "c88363617483646f67"
        assert evm.rlp_encode(1024).hex() == "820400"
        assert evm.rlp_encode(0).hex() == "80"
        assert evm.rlp_encode([]).hex() == "c0"
        assert evm.rlp_encode(b"\x7f").hex() == "7f"

    def test_long_strings_use_a_length_of_length(self):
        out = evm.rlp_encode(b"a" * 60)
        assert out[:2].hex() == "b83c" and out[2:] == b"a" * 60

    def test_unsupported_types_raise(self):
        with pytest.raises(TypeError):
            evm.rlp_encode("a str")


class TestSigning:
    def test_message_signature_recovers_the_signer(self):
        sig = evm.sign_message(b"hello lapse", KEY_ONE)
        assert len(sig) == 132 and sig[-2:] in ("1b", "1c")
        assert evm.recover_message_signer(b"hello lapse", sig) == KEY_ONE_ADDR

    def test_signature_is_deterministic(self):
        assert evm.sign_message(b"x", KEY_ONE) == evm.sign_message(b"x", KEY_ONE)

    def test_another_message_does_not_recover_the_signer(self):
        sig = evm.sign_message(b"one", KEY_ONE)
        assert evm.recover_message_signer(b"two", sig) != KEY_ONE_ADDR

    @pytest.mark.parametrize("junk", ["", "0x", "0x12", "zz", "0x" + "00" * 65, "0x" + "11" * 64 + "05"])
    def test_malformed_signatures_recover_nothing(self, junk):
        assert evm.recover_message_signer(b"x", junk) is None

    def test_transaction_matches_the_reference_encoding(self):
        raw = evm.sign_transaction(
            KEY_ONE, chain_id=8453, nonce=7, to=DEAD, value=10 ** 15,
            data=bytes.fromhex("49290c1c00ff"), gas=60000,
            max_fee_per_gas=3_000_000, max_priority_fee_per_gas=1_000_000)
        assert raw[:1] == b"\x02"
        assert raw.hex() == REFERENCE_TX
        assert evm.transaction_hash(raw) == REFERENCE_TX_HASH

    def test_a_bad_destination_is_refused_before_signing(self):
        with pytest.raises(ValueError):
            evm.sign_transaction(KEY_ONE, chain_id=1, nonce=0, to="0x12", value=1,
                                 gas=21000, max_fee_per_gas=1, max_priority_fee_per_gas=1)


class TestRpc:
    def test_hex_results_become_ints(self, monkeypatch):
        monkeypatch.setattr(evm, "rpc", lambda url, m, p=None: "0x10")
        assert evm.get_balance_wei("u", KEY_ONE_ADDR) == 16
        assert evm.get_nonce("u", KEY_ONE_ADDR) == 16

    def test_fee_params_leave_headroom_over_the_base_fee(self, monkeypatch):
        def fake(url, m, p=None):
            return {"baseFeePerGas": hex(100)} if m == "eth_getBlockByNumber" else hex(7)
        monkeypatch.setattr(evm, "rpc", fake)
        assert evm.get_fee_params("u") == (207, 7)
        assert evm.gas_price_wei("u") == 107

    def test_unreachable_endpoint_is_distinct_from_a_refusal(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("down")
        monkeypatch.setattr(evm._session, "post", boom)
        with pytest.raises(evm.EVMUnreachable):
            evm.rpc("http://x", "eth_chainId")

    def test_an_error_reply_is_a_refusal(self, monkeypatch):
        class R:
            def json(self):
                return {"error": {"message": "nonce too low"}}
        monkeypatch.setattr(evm._session, "post", lambda *a, **k: R())
        with pytest.raises(evm.EVMError, match="nonce too low"):
            evm.rpc("http://x", "eth_sendRawTransaction")

    def test_garbage_in_a_number_is_unreachable_not_a_crash(self):
        with pytest.raises(evm.EVMUnreachable):
            evm._hex_int(None)
