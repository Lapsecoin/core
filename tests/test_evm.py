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


def rlp_decode(b):
    """A small RLP decoder, just to read a signed transaction back."""
    def one(i):
        t = b[i]
        if t < 0x80:
            return b[i:i + 1], i + 1
        if t < 0xB8:
            n = t - 0x80
            return b[i + 1:i + 1 + n], i + 1 + n
        if t < 0xC0:
            ln = t - 0xB7
            n = int.from_bytes(b[i + 1:i + 1 + ln], "big")
            return b[i + 1 + ln:i + 1 + ln + n], i + 1 + ln + n
        if t < 0xF8:
            n, start = t - 0xC0, i + 1
        else:
            ln = t - 0xF7
            n, start = int.from_bytes(b[i + 1:i + 1 + ln], "big"), i + 1 + ln
        items, j = [], start
        while j < start + n:
            item, j = one(j)
            items.append(item)
        return items, start + n
    return one(0)[0]


def sender_of(raw):
    """Who signed a type-2 transaction, from nothing but its bytes."""
    f = rlp_decode(raw[1:])
    unsigned = evm.rlp_encode([int.from_bytes(x, "big") if not isinstance(x, list) and i not in (5, 7, 8) else x
                               for i, x in enumerate(f[:9])])
    y, r, s = (int.from_bytes(x, "big") for x in f[9:12])
    xy = evm._recover(evm.keccak256(b"\x02" + unsigned), r, s, y)
    return evm._address_of_xy(xy)


class TestKeccak:
    VECTORS = {
        b"": "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        b"abc": "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
        b"a" * 3: "b9a5dc0048db9a7d13548781df3cd4b2334606391f75f40c14225a92f4cb3537",
        b"a" * 135: "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446",
        b"a" * 136: "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e",
        b"a" * 137: "d869f639c7046b4929fc92a4d988a8b22c55fbadb802c0c66ebcd484f1915f39",
        b"a" * 272: "cf7fcd4f705ee749930d19ca84561a9bf62516bd90a471545fa2f49fdc7e63c8",
    }

    def test_known_digests_including_the_padding_boundaries(self):
        for data, digest in self.VECTORS.items():
            assert evm.keccak256(data).hex() == digest, len(data)

    def test_it_is_not_sha3(self):
        import hashlib
        assert evm.keccak256(b"abc") != hashlib.sha3_256(b"abc").digest()


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
    # Signed by eth-account for key 1 over b"hello lapse": a signature made by
    # someone else's implementation that ours has to read correctly.
    EXTERNAL_SIG = ("0x1ad2787a46d63be8e945b1696543152a3afd92338b7f593093ccbe4ff001e084"
                    "34cf05a12a1cbe9720cccd77351bb8c29f73788c03a062ba9e05e931aa739c161b")

    def test_a_signature_made_elsewhere_recovers_to_its_signer(self):
        assert evm.recover_message_signer(b"hello lapse", self.EXTERNAL_SIG) == KEY_ONE_ADDR

    def test_a_signature_of_ours_recovers_to_us(self):
        sig = evm.sign_message(b"hello lapse", KEY_ONE)
        assert len(sig) == 132 and sig[-2:] in ("1b", "1c")
        assert evm.recover_message_signer(b"hello lapse", sig) == KEY_ONE_ADDR

    def test_signatures_are_low_s(self):
        for _ in range(20):
            sig = bytes.fromhex(evm.sign_message(b"x", evm.generate_keypair()[0])[2:])
            assert int.from_bytes(sig[32:64], "big") <= evm._N // 2

    def test_another_message_does_not_recover_the_signer(self):
        sig = evm.sign_message(b"one", KEY_ONE)
        assert evm.recover_message_signer(b"two", sig) != KEY_ONE_ADDR

    def test_a_signature_from_a_random_key_recovers_to_that_keys_address(self):
        for _ in range(5):
            sk, addr = evm.generate_keypair()
            assert evm.recover_message_signer(b"m", evm.sign_message(b"m", sk)) == addr

    @pytest.mark.parametrize("junk", ["", "0x", "0x12", "zz", "0x" + "00" * 65, "0x" + "11" * 64 + "05"])
    def test_malformed_signatures_recover_nothing(self, junk):
        assert evm.recover_message_signer(b"x", junk) is None

    def test_out_of_range_values_recover_nothing(self):
        d = evm.keccak256(b"x")
        assert evm._recover(d, 0, 1, 0) is None
        assert evm._recover(d, 1, 0, 0) is None
        assert evm._recover(d, evm._N, 1, 0) is None
        assert evm._recover(d, 1, evm._N, 0) is None
        assert evm._recover(d, 1, 1, 2) is None

    def test_a_transaction_made_elsewhere_reads_back_to_its_sender(self):
        assert sender_of(bytes.fromhex(REFERENCE_TX)) == KEY_ONE_ADDR

    def test_our_transaction_has_the_right_fields_and_reads_back_to_us(self):
        raw = evm.sign_transaction(
            KEY_ONE, chain_id=8453, nonce=7, to=DEAD, value=10 ** 15,
            data=bytes.fromhex("49290c1c00ff"), gas=60000,
            max_fee_per_gas=3_000_000, max_priority_fee_per_gas=1_000_000)
        assert raw[:1] == b"\x02"
        f = rlp_decode(raw[1:])
        assert [int.from_bytes(x, "big") for x in f[:5]] == [8453, 7, 1_000_000, 3_000_000, 60000]
        assert f[5].hex() == DEAD[2:].lower() and int.from_bytes(f[6], "big") == 10 ** 15
        assert f[7].hex() == "49290c1c00ff" and f[8] == []
        assert sender_of(raw) == KEY_ONE_ADDR
        assert evm.transaction_hash(raw) == "0x" + evm.keccak256(raw).hex()

    def test_the_external_transaction_has_the_same_shape_as_ours(self):
        ours = rlp_decode(evm.sign_transaction(
            KEY_ONE, chain_id=8453, nonce=7, to=DEAD, value=10 ** 15,
            data=bytes.fromhex("49290c1c00ff"), gas=60000,
            max_fee_per_gas=3_000_000, max_priority_fee_per_gas=1_000_000)[1:])
        theirs = rlp_decode(bytes.fromhex(REFERENCE_TX)[1:])
        assert ours[:9] == theirs[:9] and len(ours) == len(theirs) == 12

    def test_a_bad_destination_is_refused_before_signing(self):
        with pytest.raises(ValueError):
            evm.sign_transaction(KEY_ONE, chain_id=1, nonce=0, to="0x12", value=1,
                                 gas=21000, max_fee_per_gas=1, max_priority_fee_per_gas=1)

    def test_an_invalid_secret_is_refused(self):
        with pytest.raises(ValueError):
            evm.address_from_secret(b"\x00" * 32)


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
