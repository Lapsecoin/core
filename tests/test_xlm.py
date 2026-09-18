"""Tests for the Stellar side of the swap.

Everything here runs offline. The network-dependent reads are covered by
injecting a fake Horizon response rather than hitting mainnet, so the
suite stays deterministic and runnable without connectivity.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import xlm


class TestAmounts:
    def test_stroops_to_str_never_scientific(self):
        # Decimal renders small values as "1E-7", which Stellar rejects.
        for stroops in (1, 10, 100, 1000):
            assert "E" not in xlm.stroops_to_str(stroops)
            assert "e" not in xlm.stroops_to_str(stroops)

    def test_stroops_to_str_seven_places(self):
        assert xlm.stroops_to_str(1) == "0.0000001"
        assert xlm.stroops_to_str(15_000_000) == "1.5000000"
        assert xlm.stroops_to_str(0) == "0.0000000"

    def test_roundtrip_exact(self):
        for amount in ("0.0000001", "1.5000000", "12.3456789", "0.0000000"):
            assert xlm.stroops_to_str(xlm.str_to_stroops(amount)) == amount

    def test_no_float_drift(self):
        # 0.1 + 0.2 in floats is 0.30000000000000004. In stroops it is exact.
        assert xlm.str_to_stroops("0.1") + xlm.str_to_stroops("0.2") == \
               xlm.str_to_stroops("0.3")


class TestKeys:
    def test_generate_keypair_is_real_ed25519(self):
        seed, pub = xlm.generate_keypair()
        assert pub.startswith("G")
        assert seed.startswith("S")
        assert len(pub) == 56
        assert xlm.is_valid_address(pub)

    def test_keypairs_are_distinct(self):
        _, a = xlm.generate_keypair()
        _, b = xlm.generate_keypair()
        assert a != b

    def test_rejects_invalid_address(self):
        for bad in ("GNOTREAL", "", "not-an-address", None, 12345):
            assert not xlm.is_valid_address(bad)

    def test_save_and_load_roundtrip(self, tmp_path):
        seed, pub = xlm.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm.save_key(path, seed, pub, "correct horse battery staple")
        assert xlm.load_public_key(path) == pub
        assert xlm.decrypt_seed(path, "correct horse battery staple") == seed

    def test_wrong_passphrase_rejected(self, tmp_path):
        seed, pub = xlm.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm.save_key(path, seed, pub, "right")
        with pytest.raises(ValueError):
            xlm.decrypt_seed(path, "wrong")

    def test_seed_not_stored_in_clear(self, tmp_path):
        seed, pub = xlm.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm.save_key(path, seed, pub, "pw")
        raw = open(path).read()
        assert seed not in raw

    def test_key_file_is_owner_only(self, tmp_path):
        seed, pub = xlm.generate_keypair()
        path = str(tmp_path / "xlm.key")
        xlm.save_key(path, seed, pub, "pw")
        assert oct(os.stat(path).st_mode)[-3:] == "600"

    def test_passphrase_is_mandatory(self, tmp_path):
        seed, pub = xlm.generate_keypair()
        with pytest.raises(ValueError):
            xlm.save_key(str(tmp_path / "k"), seed, pub, "")

    def test_missing_key_file_returns_none(self, tmp_path):
        assert xlm.load_public_key(str(tmp_path / "absent.key")) is None


class TestTransactionBuilding:
    def test_payment_hash_is_deterministic(self):
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr_a, hash_a = xlm.build_payment(seed, dest, 15_000_000, "s:1", 99)
        xdr_b, hash_b = xlm.build_payment(seed, dest, 15_000_000, "s:1", 99)
        assert hash_a == hash_b

    def test_sequence_changes_the_hash(self):
        """The property crash recovery depends on: one signed envelope
        carries one sequence number, so it can apply at most once."""
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        _, hash_99 = xlm.build_payment(seed, dest, 15_000_000, "s:1", 99)
        _, hash_100 = xlm.build_payment(seed, dest, 15_000_000, "s:1", 100)
        assert hash_99 != hash_100

    def test_envelope_hash_matches_build(self):
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr, tx_hash = xlm.build_payment(seed, dest, 1_000_000, "s:2", 5)
        assert xlm.envelope_hash(xdr) == tx_hash

    def test_memo_is_carried(self):
        from stellar_sdk import TransactionEnvelope
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr, _h = xlm.build_payment(seed, dest, 1_000_000, "abc123:7", 5)
        env = TransactionEnvelope.from_xdr(xdr, xlm.NETWORK_PASSPHRASE)
        assert env.transaction.memo.memo_text == b"abc123:7"

    def test_amount_survives_into_envelope(self):
        from stellar_sdk import TransactionEnvelope
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr, _h = xlm.build_payment(seed, dest, 12_345_678, "s:1", 5)
        env = TransactionEnvelope.from_xdr(xdr, xlm.NETWORK_PASSPHRASE)
        assert env.transaction.operations[0].amount == "1.2345678"

    def test_create_account_rejects_below_minimum(self):
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        with pytest.raises(xlm.XLMError):
            xlm.build_create_account(seed, dest,
                                     xlm.ACCOUNT_MIN_BALANCE_STROOPS - 1,
                                     "s:1", 5)

    def test_create_account_accepts_minimum(self):
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr, tx_hash = xlm.build_create_account(
            seed, dest, xlm.ACCOUNT_MIN_BALANCE_STROOPS, "s:1", 5)
        assert xlm.envelope_hash(xdr) == tx_hash


class TestSponsoredCreation:
    """A seller holding only LAPSE must be able to receive XLM without
    first owning any."""

    def _build(self):
        sponsor_seed, _ = xlm.generate_keypair()
        new_seed, new_pub = xlm.generate_keypair()
        xdr, tx_hash = xlm.build_sponsored_create_account(
            sponsor_seed, new_seed, "s:1", 42)
        return xdr, tx_hash, new_pub

    def test_has_all_three_sponsorship_operations(self):
        from stellar_sdk import TransactionEnvelope
        xdr, _h, _p = self._build()
        env = TransactionEnvelope.from_xdr(xdr, xlm.NETWORK_PASSPHRASE)
        names = [type(op).__name__ for op in env.transaction.operations]
        assert names == ["BeginSponsoringFutureReserves",
                         "CreateAccount",
                         "EndSponsoringFutureReserves"]

    def test_new_account_starts_at_zero(self):
        from stellar_sdk import TransactionEnvelope
        xdr, _h, _p = self._build()
        env = TransactionEnvelope.from_xdr(xdr, xlm.NETWORK_PASSPHRASE)
        assert env.transaction.operations[1].starting_balance == "0"

    def test_requires_both_signatures(self):
        """Neither side can sponsor the other unilaterally."""
        from stellar_sdk import TransactionEnvelope
        xdr, _h, _p = self._build()
        env = TransactionEnvelope.from_xdr(xdr, xlm.NETWORK_PASSPHRASE)
        assert len(env.signatures) == 2


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestHorizonReads:
    """Horizon behaviour, faked so the suite stays offline and deterministic."""

    def test_missing_account_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(404))
        assert xlm.account_exists("GABC") is False
        assert xlm.get_balance_stroops("GABC") == 0

    def test_unknown_tx_is_none(self, monkeypatch):
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(404))
        assert xlm.get_transaction("0" * 64) is None
        assert xlm.transaction_succeeded("0" * 64) is False

    def test_rate_limit_raises_unreachable_not_false(self, monkeypatch):
        """A rate limit means the answer is unknown, not no. Collapsing the
        two is how a live trade gets misread as abandoned."""
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(429))
        with pytest.raises(xlm.XLMUnreachable):
            xlm.account_exists("GABC")

    def test_server_error_raises_unreachable(self, monkeypatch):
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(503))
        with pytest.raises(xlm.XLMUnreachable):
            xlm.get_balance_stroops("GABC")

    def test_connection_failure_raises_unreachable(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("network down")

        monkeypatch.setattr(xlm._session, "get", boom)
        with pytest.raises(xlm.XLMUnreachable):
            xlm.account_exists("GABC")

    def test_balance_parsed_from_native_only(self, monkeypatch):
        payload = {"balances": [
            {"asset_type": "credit_alphanum4", "balance": "999.0000000"},
            {"asset_type": "native", "balance": "12.5000000"},
        ], "subentry_count": 0, "num_sponsored": 0}
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(200, payload))
        assert xlm.get_balance_stroops("GABC") == 125_000_000

    def test_spendable_excludes_locked_reserve(self, monkeypatch):
        payload = {"balances": [{"asset_type": "native", "balance": "5.0000000"}],
                   "subentry_count": 0, "num_sponsored": 0}
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(200, payload))
        # 5 XLM held, 1 XLM locked as the two base reserves.
        assert xlm.get_spendable_stroops("GABC") == 40_000_000

    def test_spendable_never_negative(self, monkeypatch):
        payload = {"balances": [{"asset_type": "native", "balance": "0.5000000"}],
                   "subentry_count": 0, "num_sponsored": 0}
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(200, payload))
        assert xlm.get_spendable_stroops("GABC") == 0

    def test_failed_tx_on_ledger_is_not_success(self, monkeypatch):
        """Stellar records failed transactions too; presence is not settlement."""
        payload = {"successful": False, "hash": "ab" * 32}
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(200, payload))
        assert xlm.transaction_succeeded("ab" * 32) is False


class _RoutedResponses:
    """Routes distinct Horizon endpoints to distinct canned payloads.

    find_payment and recent_incoming_payments each make one call for the
    payments list and then one more per candidate to check its memo, so a
    single static response (the pattern above) cannot exercise them; each
    path needs its own answer.
    """

    def __init__(self, routes):
        self.routes = routes    # {path: payload}, missing path -> 404
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        path = url[len(xlm.HORIZON_URL):]
        self.calls.append((path, params))
        if path not in self.routes:
            return _FakeResponse(404)
        return _FakeResponse(200, self.routes[path])


def _payments_page(records):
    return {"_embedded": {"records": records}}


def _payment_record(to, frm, amount, tx_hash, successful=True, asset="native"):
    return {"type": "payment", "asset_type": asset, "to": to, "from": frm,
            "amount": amount, "transaction_successful": successful,
            "transaction_hash": tx_hash}


def _create_account_record(account, funder, starting_balance, tx_hash,
                           successful=True):
    return {"type": "create_account", "account": account, "funder": funder,
            "starting_balance": starting_balance,
            "transaction_successful": successful, "transaction_hash": tx_hash}


class TestFindPayment:
    """The check that actually decides an increment: every term has to
    match, not just the amount, or an unrelated transfer would settle a
    step nobody agreed to."""

    def test_finds_a_matching_settled_payment(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "tag1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1 * xlm.STROOPS_PER_XLM) == "tx1"

    def test_no_account_at_all_returns_none(self, monkeypatch):
        monkeypatch.setattr(xlm._session, "get", lambda *a, **k: _FakeResponse(404))
        assert xlm.find_payment("GTO", "tag1", 1) is None

    def test_under_payment_does_not_match(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "0.5000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "tag1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1 * xlm.STROOPS_PER_XLM) is None

    def test_over_payment_still_matches(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "9.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "tag1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1 * xlm.STROOPS_PER_XLM) == "tx1"

    def test_wrong_memo_does_not_match(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "different-tag"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1) is None

    def test_wrong_sender_does_not_match_when_specified(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GNOTIT", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "tag1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1, from_address="GFROM") is None

    def test_failed_transaction_is_ignored(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1",
                                successful=False)]),
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1) is None

    def test_non_native_asset_is_ignored(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1",
                                asset="credit_alphanum4")]),
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1) is None

    def test_a_payment_to_someone_else_is_ignored(self, monkeypatch):
        """Horizon's payments feed for an account also lists that
        account's own outgoing payments; only inbound ones count."""
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GELSEWHERE", "GTO", "5.0000000", "tx1")]),
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1) is None

    def test_create_account_record_counts_as_a_payment(self, monkeypatch):
        """The first delivery to a brand-new counterparty is a
        create_account operation, not a payment; it has to be findable
        the same way, or a step to a fresh address could never settle."""
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _create_account_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "tag1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.find_payment("GTO", "tag1", 1 * xlm.STROOPS_PER_XLM) == "tx1"

    def test_only_the_matching_candidates_pay_for_a_memo_lookup(self, monkeypatch):
        """The memo check is the one that costs an extra Horizon call, so
        it must run only after amount and sender have already matched."""
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "0.1000000", "tx-under"),
                _payment_record("GTO", "GFROM", "5.0000000", "tx-ok")]),
            "/transactions/tx-ok": {"memo_type": "text", "memo": "tag1"},
        }
        routed = _RoutedResponses(routes)
        monkeypatch.setattr(xlm._session, "get", routed)
        assert xlm.find_payment("GTO", "tag1", 1 * xlm.STROOPS_PER_XLM) == "tx-ok"
        assert ("/transactions/tx-under", None) not in routed.calls


class TestRecentIncomingPayments:
    """Discovery's raw material: every settled inbound payment with its
    memo, not a search for one already-known memo."""

    def test_returns_sender_memo_amount_and_hash(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "text", "memo": "a1b2c3d4:e5f6a1b2:1"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        rows = xlm.recent_incoming_payments("GTO")
        assert rows == [("GFROM", "a1b2c3d4:e5f6a1b2:1", 5 * xlm.STROOPS_PER_XLM, "tx1")]

    def test_memo_is_none_when_absent(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.recent_incoming_payments("GTO")[0][1] is None

    def test_memo_is_none_when_not_text(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "5.0000000", "tx1")]),
            "/transactions/tx1": {"memo_type": "id", "memo": "12345"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        assert xlm.recent_incoming_payments("GTO")[0][1] is None

    def test_failed_and_outgoing_and_non_native_records_are_excluded(self, monkeypatch):
        routes = {
            "/accounts/GTO/payments": _payments_page([
                _payment_record("GTO", "GFROM", "1.0000000", "tx-failed",
                                successful=False),
                _payment_record("GELSEWHERE", "GTO", "1.0000000", "tx-outgoing"),
                _payment_record("GTO", "GFROM", "1.0000000", "tx-other-asset",
                                asset="credit_alphanum4"),
                _payment_record("GTO", "GFROM", "2.0000000", "tx-good"),
            ]),
            "/transactions/tx-good": {"memo_type": "text", "memo": "tag"},
        }
        monkeypatch.setattr(xlm._session, "get", _RoutedResponses(routes))
        rows = xlm.recent_incoming_payments("GTO")
        assert [r[3] for r in rows] == ["tx-good"]

    def test_no_account_returns_empty(self, monkeypatch):
        monkeypatch.setattr(xlm._session, "get", lambda *a, **k: _FakeResponse(404))
        assert xlm.recent_incoming_payments("GTO") == []


class TestSubmitIdempotency:
    """Re-submitting a persisted envelope is the recovery path, so the ways
    a repeat comes back must read as success, not failure."""

    def _xdr(self):
        seed, _ = xlm.generate_keypair()
        _, dest = xlm.generate_keypair()
        xdr, tx_hash = xlm.build_payment(seed, dest, 1_000_000, "s:1", 7)
        return xdr, tx_hash

    def test_success(self, monkeypatch):
        xdr, tx_hash = self._xdr()
        monkeypatch.setattr(xlm._session, "post",
                            lambda *a, **k: _FakeResponse(200, {"successful": True}))
        ok, got_hash, detail = xlm.submit_envelope(xdr)
        assert ok is True
        assert got_hash == tx_hash

    def test_duplicate_that_already_applied_reads_as_success(self, monkeypatch):
        xdr, tx_hash = self._xdr()
        monkeypatch.setattr(
            xlm._session, "post",
            lambda *a, **k: _FakeResponse(
                400, {"extras": {"result_codes": {"transaction": "tx_bad_seq"}}}))
        # Horizon then confirms the transaction did land.
        monkeypatch.setattr(
            xlm._session, "get",
            lambda *a, **k: _FakeResponse(200, {"successful": True}))
        ok, _h, detail = xlm.submit_envelope(xdr)
        assert ok is True
        assert detail == "already applied"

    def test_sequence_consumed_by_other_tx_is_a_failure(self, monkeypatch):
        """Bad sequence with no matching transaction on the ledger means
        this envelope can never apply. The caller must rebuild, not retry."""
        xdr, _tx_hash = self._xdr()
        monkeypatch.setattr(
            xlm._session, "post",
            lambda *a, **k: _FakeResponse(
                400, {"extras": {"result_codes": {"transaction": "tx_bad_seq"}}}))
        monkeypatch.setattr(xlm._session, "get",
                            lambda *a, **k: _FakeResponse(404))
        ok, _h, detail = xlm.submit_envelope(xdr)
        assert ok is False
        assert "sequence consumed" in detail

    def test_transport_failure_raises_rather_than_reporting_failure(self, monkeypatch):
        """Fate unknown must not be recorded as failed, or a payment that is
        about to settle gets stranded and possibly re-sent."""
        import requests
        xdr, _tx_hash = self._xdr()

        def boom(*a, **k):
            raise requests.ConnectionError("dropped")

        monkeypatch.setattr(xlm._session, "post", boom)
        with pytest.raises(xlm.XLMUnreachable):
            xlm.submit_envelope(xdr)

    def test_rate_limited_submit_raises_unreachable(self, monkeypatch):
        xdr, _tx_hash = self._xdr()
        monkeypatch.setattr(xlm._session, "post",
                            lambda *a, **k: _FakeResponse(429))
        with pytest.raises(xlm.XLMUnreachable):
            xlm.submit_envelope(xdr)


class TestPriceFeed:
    def test_failure_returns_zero_never_raises(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(xlm._session, "get", boom)
        xlm._price_cache.update(usd=0.0, at=0.0)
        assert xlm.get_xlm_usd() == 0.0
