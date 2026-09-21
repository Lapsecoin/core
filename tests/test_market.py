"""Order book: signing, verification, storage and depth.

The book is an unauthenticated surface that anyone on the network can
push rows into, so most of what is tested here is refusal: a forged
order, a tampered one, a malformed one, or one that would let a single
maker fill the book.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import crypto
import market
import storage as storage_mod
import swap
import swap_engine
import trade_storage
import xlm as xlm_mod
from trade_storage import Increment, LEG_SETTLED, Order, Trade


LAPSE = 100_000_000
XLM = 10_000_000


@pytest.fixture(autouse=True)
def fresh_db():
    storage_mod.db.init(":memory:")
    storage_mod.db.connect(reuse_if_open=True)
    storage_mod.db.drop_tables(trade_storage.TRADE_TABLES, safe=True)
    storage_mod.db.create_tables(trade_storage.TRADE_TABLES, safe=True)
    trade_storage._initialised = True
    yield
    storage_mod.db.close()


@pytest.fixture(scope="module")
def maker(tmp_path_factory):
    """A real FALCON keypair with its key file, as a node would have."""
    sk, pk = crypto.generate_keypair()
    path = str(tmp_path_factory.mktemp("keys") / "maker.key")
    crypto.save_key(path, sk, pk, "pw")
    kek = crypto.derive_kek(path, "pw")
    _seed, xlm_pub = xlm_mod.generate_keypair()
    return {"addr": crypto.public_key_to_address(pk), "pubkey": pk.hex(),
            "keyfile": path, "kek": kek, "xlm": xlm_pub}


def signed_order(maker, **overrides):
    order = market.build_order(
        maker_lapse_addr=maker["addr"], maker_xlm_addr=maker["xlm"],
        direction=overrides.pop("direction", "sell"),
        lapse_total=overrides.pop("lapse_total", 10 * LAPSE),
        price_stroops_per_lapse=overrides.pop("price", 1000),
        expiry_block=overrides.pop("expiry_block", 50_000),
        pubkey_hex=maker["pubkey"],
        min_fill=overrides.pop("min_fill", 0),
        max_fill=overrides.pop("max_fill", 0))
    order.update(overrides)
    return market.sign_order(order, maker["keyfile"], maker["kek"])


@pytest.fixture(scope="module")
def taker(tmp_path_factory):
    """A second, independent FALCON keypair, standing in for whoever
    fills an order."""
    sk, pk = crypto.generate_keypair()
    path = str(tmp_path_factory.mktemp("keys") / "taker.key")
    crypto.save_key(path, sk, pk, "pw")
    kek = crypto.derive_kek(path, "pw")
    _seed, xlm_pub = xlm_mod.generate_keypair()
    return {"addr": crypto.public_key_to_address(pk), "pubkey": pk.hex(),
            "keyfile": path, "kek": kek, "xlm": xlm_pub}


def signed_fill_request(taker, order_id="order-1", session_id="s" * 16, **overrides):
    req = market.build_fill_request(
        order_id=order_id, session_id=session_id,
        taker_lapse_addr=taker["addr"], taker_xlm_addr=taker["xlm"],
        lapse_total=overrides.pop("lapse_total", 1 * LAPSE),
        pubkey_hex=taker["pubkey"])
    req.update(overrides)
    return market.sign_fill_request(req, taker["keyfile"], taker["kek"])


def signed_fill_response(maker, request_id="r" * 16, order_id="order-1",
                         session_id="s" * 16, accepted=True, **overrides):
    resp = market.build_fill_response(
        request_id=request_id, order_id=order_id, session_id=session_id,
        lapse_total=overrides.pop("lapse_total", 1 * LAPSE), accepted=accepted,
        maker_pubkey_hex=maker["pubkey"],
        accepted_height=overrides.pop("accepted_height", 1000),
        confirm_depth=overrides.pop("confirm_depth", 2),
        xlm_total=overrides.pop("xlm_total", 1000 * XLM),
        direction=overrides.pop("direction", "sell"),
        maker_xlm_addr=overrides.pop("maker_xlm_addr", maker["xlm"]),
        maker_opens=overrides.pop("maker_opens", True),
        increment_count=overrides.pop("increment_count", 3 if accepted else None),
        reason=overrides.pop("reason", "" if accepted else "no room"))
    resp.update(overrides)
    return market.sign_fill_response(resp, maker["keyfile"], maker["kek"])


class TestSigning:
    def test_signed_order_verifies(self, maker):
        assert market.verify_order(signed_order(maker), current_height=100)

    def test_unsigned_order_is_refused(self, maker):
        order = market.build_order(
            maker["addr"], maker["xlm"], "sell", 10 * LAPSE, 1000,
            50_000, maker["pubkey"])
        with pytest.raises(market.OrderRejected, match="signature"):
            market.verify_order(order, current_height=100)

    @pytest.mark.parametrize("field,value", [
        ("lapse_total", 999 * LAPSE),
        ("price_stroops_per_lapse", 1),
        ("direction", "buy"),
        ("expiry_block", 90_000),
        ("min_fill", 5),
    ])
    def test_tampering_breaks_the_signature(self, maker, field, value):
        order = signed_order(maker)
        order[field] = value
        with pytest.raises(market.OrderRejected, match="signature"):
            market.verify_order(order, current_height=100)

    def test_swapping_the_payout_address_breaks_it(self, maker):
        """The attack that matters: redirect an order's proceeds."""
        order = signed_order(maker)
        _seed, other = xlm_mod.generate_keypair()
        order["maker_xlm_addr"] = other
        with pytest.raises(market.OrderRejected, match="signature"):
            market.verify_order(order, current_height=100)

    def test_pubkey_must_match_the_claimed_address(self, maker):
        order = signed_order(maker)
        order["maker_lapse_addr"] = "a.b.c.d.e.f.g.h.i.j.k.l"
        with pytest.raises(market.OrderRejected):
            market.verify_order(order, current_height=100)

    def test_extra_field_is_refused_not_ignored(self, maker):
        """An unsigned field would be content the maker never agreed to."""
        order = signed_order(maker)
        order["surprise"] = "x"
        with pytest.raises(market.OrderRejected, match="unexpected"):
            market.verify_order(order, current_height=100)


class TestAdmissionRunsBeforeTheSignatureCheck:
    """Dedup already runs first (node._handle_inbound_order, via
    market.already_known), which is right; the expensive check should
    come last. These bounds are the other half of that: a maker already
    at capacity, or a book already at its ceiling, must be refused
    before this node ever pays for a FALCON verification, not after."""

    def test_a_maker_already_at_its_cap_is_refused_pre_signature(self, maker):
        for i in range(market.MAX_ORDERS_PER_MAKER):
            market.store_order(signed_order(maker, order_id=f"order-{i}"))
        # A freshly, correctly signed order, from the same maker, who is
        # simply out of room.
        with pytest.raises(market.OrderRejected, match="limit"):
            market.verify_order(signed_order(maker, order_id="one-more"),
                                current_height=100)

    def test_a_full_book_is_refused_pre_signature(self, maker, monkeypatch):
        monkeypatch.setattr(market, "MAX_ORDERS_TOTAL", 1)
        market.store_order(signed_order(maker, order_id="order-0"))
        with pytest.raises(market.OrderRejected, match="full"):
            market.verify_order(signed_order(maker, order_id="order-1"),
                                current_height=100)

    def test_admission_does_not_let_a_forged_order_through(self, maker):
        """Passing the cheap check changes nothing about the signature
        requirement: refusing early can only reject work that would have
        failed anyway, never admit something that would not have passed."""
        order = signed_order(maker)
        order["lapse_total"] = 999 * LAPSE   # breaks the signature
        with pytest.raises(market.OrderRejected, match="signature"):
            market.verify_order(order, current_height=100)


class TestValidation:
    def test_missing_fields_refused(self, maker):
        order = signed_order(maker)
        del order["price_stroops_per_lapse"]
        with pytest.raises(market.OrderRejected, match="missing"):
            market.verify_order(order, current_height=100)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_amount_refused(self, maker, bad):
        with pytest.raises(market.OrderRejected):
            market.verify_order(signed_order(maker, lapse_total=bad),
                                current_height=100)

    @pytest.mark.parametrize("bad", [0, -5])
    def test_non_positive_price_refused(self, maker, bad):
        with pytest.raises(market.OrderRejected):
            market.verify_order(signed_order(maker, price=bad),
                                current_height=100)

    def test_bool_is_not_an_integer(self, maker):
        """True == 1 in Python, so this needs an explicit check.

        Injected after signing, which is where it would actually arrive
        from: a hostile peer sends the JSON directly and never goes
        through build_order, whose int() would have coerced it.
        """
        order = signed_order(maker)
        order["lapse_total"] = True
        with pytest.raises(market.OrderRejected, match="integer"):
            market.verify_order(order, current_height=100)

    def test_expired_order_refused(self, maker):
        with pytest.raises(market.OrderRejected, match="expired"):
            market.verify_order(signed_order(maker, expiry_block=50),
                                current_height=100)

    def test_unbounded_expiry_refused(self, maker):
        """A never-expiring order is a standing advertisement, which this
        design deliberately avoids, and it would sit in every book."""
        with pytest.raises(market.OrderRejected, match="too far"):
            market.verify_order(
                signed_order(maker, expiry_block=10_000_000),
                current_height=100)

    def test_min_fill_above_size_refused(self, maker):
        with pytest.raises(market.OrderRejected, match="min_fill"):
            market.verify_order(signed_order(maker, min_fill=99 * LAPSE),
                                current_height=100)

    def test_bad_stellar_address_refused(self, maker):
        with pytest.raises(market.OrderRejected, match="Stellar"):
            market.verify_order(signed_order(maker, maker_xlm_addr="GNOPE"),
                                current_height=100)

    def test_non_dict_refused(self):
        with pytest.raises(market.OrderRejected):
            market.verify_order("not an order", current_height=100)


class TestCancellation:
    def test_maker_can_cancel_their_own(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        cancel = market.cancellation_for(order["order_id"], maker["pubkey"],
                                         maker["keyfile"], maker["kek"])
        canceller = market.verify_cancellation(cancel)
        assert canceller == maker["addr"]
        assert market.apply_cancellation(order["order_id"], canceller) is True
        assert Order.get(Order.order_id == order["order_id"]).cancelled is True

    def test_someone_else_cannot_cancel(self, maker, tmp_path):
        order = signed_order(maker)
        market.store_order(order)
        sk, pk = crypto.generate_keypair()
        path = str(tmp_path / "other.key")
        crypto.save_key(path, sk, pk, "pw")
        other_kek = crypto.derive_kek(path, "pw")
        cancel = market.cancellation_for(order["order_id"], pk.hex(), path,
                                         other_kek)
        canceller = market.verify_cancellation(cancel)
        with pytest.raises(market.OrderRejected, match="only the maker"):
            market.apply_cancellation(order["order_id"], canceller)

    def test_forged_cancellation_refused(self, maker):
        cancel = market.cancellation_for("some-id", maker["pubkey"],
                                         maker["keyfile"], maker["kek"])
        cancel["cancel"] = "a-different-order"
        with pytest.raises(market.OrderRejected, match="signature"):
            market.verify_cancellation(cancel)

    def test_cancelled_order_leaves_the_book(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        market.apply_cancellation(order["order_id"], maker["addr"])
        assert market.open_orders(current_height=100) == []


class TestStorage:
    def test_store_and_read_back(self, maker):
        order = signed_order(maker)
        assert market.store_order(order) is True
        assert market.get_order(order["order_id"]).lapse_total == 10 * LAPSE

    def test_duplicate_is_not_stored_twice(self, maker):
        order = signed_order(maker)
        assert market.store_order(order) is True
        assert market.store_order(order) is False

    def test_auto_match_margin_defaults_to_zero(self, maker):
        """A gossiped order never carries a margin (see
        market.store_order's own docstring): only this node's own
        posting flow ever passes one."""
        order = signed_order(maker)
        market.store_order(order)
        assert market.get_order(order["order_id"]).auto_match_margin_stroops == 0

    def test_auto_match_margin_is_local_only_and_never_on_the_wire(self, maker):
        order = signed_order(maker)
        market.store_order(order, auto_match_margin_stroops=50)
        row = market.get_order(order["order_id"])
        assert row.auto_match_margin_stroops == 50
        # order_to_wire is what a backfill response and gossip both send;
        # it must never carry this node's private margin to anyone.
        wire = market.order_to_wire(row)
        assert "auto_match_margin_stroops" not in wire
        assert "auto_match_margin_stroops" not in market.SIGNED_FIELDS

    def test_one_maker_cannot_fill_the_book(self, maker):
        for _ in range(market.MAX_ORDERS_PER_MAKER):
            market.store_order(signed_order(maker))
        with pytest.raises(market.OrderRejected, match="limit"):
            market.store_order(signed_order(maker))

    def test_the_book_itself_has_a_ceiling(self, maker, monkeypatch):
        """Independent of the per-maker cap: many addresses cost nothing
        to mint, so the book needs its own bound too. store_order does
        not itself check signatures, so distinct makers can be simulated
        here just by varying the claimed address."""
        monkeypatch.setattr(market, "MAX_ORDERS_TOTAL", 3)
        for i in range(3):
            order = signed_order(maker, order_id=f"order-{i}")
            order["maker_lapse_addr"] = f"maker-{i}.addr"
            market.store_order(order)
        with pytest.raises(market.OrderRejected, match="full"):
            market.store_order(signed_order(maker, order_id="one-too-many"))

    def test_expired_orders_are_excluded(self, maker):
        market.store_order(signed_order(maker, expiry_block=200))
        assert market.open_orders(current_height=100)
        assert market.open_orders(current_height=300) == []

    def test_prune_removes_expired(self, maker):
        market.store_order(signed_order(maker, expiry_block=200))
        assert market.prune_expired(current_height=300) == 1
        assert Order.select().count() == 0

    def test_remaining_is_full_before_any_trade(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        assert market.remaining_ticks(row, 100) == 10 * LAPSE


class TestUnfillableRemainderIsHiddenFromTakers:
    """Once an order's remaining drops below its own min_fill, no request
    against it could ever be honoured (validate_fill would reject any
    amount smaller than min_fill, and nothing larger is left). Listing
    it anyway would only cost a taker a fill request guaranteed to come
    back declined."""

    def test_a_remainder_below_min_fill_is_excluded(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, min_fill=5 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 7 * LAPSE)
        assert market.remaining_ticks(row, 100) == 3 * LAPSE   # nonzero, but < min_fill
        assert market.open_orders(current_height=100) == []

    def test_a_remainder_at_or_above_min_fill_still_shows(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, min_fill=5 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 5 * LAPSE)
        assert market.remaining_ticks(row, 100) == 5 * LAPSE   # exactly min_fill
        assert len(market.open_orders(current_height=100)) == 1

    def test_no_min_fill_set_shows_any_nonzero_remainder(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, min_fill=0)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 9 * LAPSE)
        assert market.remaining_ticks(row, 100) == 1 * LAPSE
        assert len(market.open_orders(current_height=100)) == 1

    def test_fully_delivered_is_excluded_same_as_before(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, min_fill=5 * LAPSE)
        market.store_order(order)
        _accepted_response(maker, order["order_id"], "s" * 16, 10 * LAPSE)
        assert market.open_orders(current_height=100) == []


def _accepted_response(maker, order_id, session_id, lapse_total, request_id=None,
                       **overrides):
    """A network-observed accepted response, signed by the order's real
    maker: reserved_ticks now checks that a response's maker_pubkey
    actually resolves to the order's own maker_lapse_addr (see
    TestForgedResponsesDoNotReserve below for why), so a helper standing
    in for a genuine one has to be signed by that same key rather than a
    placeholder hex string."""
    resp = signed_fill_response(
        maker, request_id=request_id or session_id, order_id=order_id,
        session_id=session_id, lapse_total=lapse_total, accepted=True, **overrides)
    trade_storage.FillResponse.create(
        request_id=resp["request_id"], order_id=resp["order_id"],
        session_id=resp["session_id"], lapse_total=resp["lapse_total"],
        accepted=resp["accepted"], increment_count=resp["increment_count"],
        reason=resp["reason"], maker_pubkey=resp["maker_pubkey"],
        accepted_height=resp["accepted_height"], confirm_depth=resp["confirm_depth"],
        signature=resp["signature"], received_at=time.time())


class TestRemainingReflectsNetworkKnownResponses:
    """A node that is neither an order's maker nor any of its takers has
    no local Trade row for it at all, however much of it has actually
    been filled by strangers. An accepted fill response is gossiped to
    the whole network exactly like an order is, so a node that has
    merely relayed one (never traded on it) still has to see it here, or
    a taker relying on this node's view of "remaining" could sign and
    pay for a fill the order's real maker will simply refuse."""

    def test_a_response_this_node_never_traded_on_still_reduces_remaining(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        assert market.remaining_ticks(row, 100) == 10 * LAPSE

        # A response this node only ever saw over gossip: no Trade row
        # here for it, on either side, the way a genuine third party's
        # node would have none either.
        _accepted_response(maker, order["order_id"], "s" * 16, 4 * LAPSE)
        assert market.remaining_ticks(row, 100) == 6 * LAPSE

    def test_multiple_unrelated_responses_all_reduce_it(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s1" * 8, 3 * LAPSE)
        _accepted_response(maker, order["order_id"], "s2" * 8, 2 * LAPSE)
        assert market.remaining_ticks(row, 100) == 5 * LAPSE

    def test_an_unaccepted_response_does_not_count(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        trade_storage.FillResponse.create(
            request_id="r1", order_id=order["order_id"], session_id="s" * 16,
            lapse_total=4 * LAPSE, accepted=False, increment_count=None,
            reason="not enough remaining", maker_pubkey="ab" * 10,
            signature="cd" * 10, received_at=time.time())
        assert market.remaining_ticks(row, 100) == 10 * LAPSE

    def test_a_response_against_a_different_order_does_not_count(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, "some-other-order", "s" * 16, 4 * LAPSE)
        assert market.remaining_ticks(row, 100) == 10 * LAPSE

    def test_a_locally_tracked_trades_own_response_is_not_double_counted(self, maker, taker):
        """The maker's own accurate delivered_ticks must not also have
        that same response's full amount subtracted a second time as
        'reserved', which would make an order's own maker undercount
        its remaining size for no reason."""
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        session_id = "s" * 16
        _accepted_response(maker, order["order_id"], session_id, 4 * LAPSE)
        Trade.create(
            session_id=session_id, order_id=order["order_id"], role="maker",
            my_lapse_addr=maker["addr"], my_xlm_addr=maker["xlm"],
            peer_lapse_addr=taker["addr"], peer_xlm_addr=taker["xlm"],
            i_send="lapse", lapse_total=4 * LAPSE, xlm_total=4000 * XLM,
            increment_count=1, confirm_depth=1,
            status=trade_storage.TRADE_ACTIVE,
            created_at=time.time(), updated_at=time.time())
        # Nothing settled yet, so delivered_ticks is 0 for this trade,
        # but it must not ALSO be treated as an unrelated reserved
        # response once this node recognizes it as its own trade's.
        assert market.remaining_ticks(row, 100) == 10 * LAPSE


class TestForgedResponsesDoNotReserve:
    """reserved_ticks used to sum every accepted FillResponse naming an
    order, regardless of whose key signed it. verify_fill_response only
    proves a response is genuinely signed by *somebody* (a plain relay
    cannot always tell who the right signer is, see
    node._handle_inbound_fill_response), so a stranger could mint a
    free keypair, sign an 'accepted' response naming somebody else's
    real order, and shrink that order's advertised remaining size to
    zero for the whole network, including the real maker's own node,
    without ever sending a FillRequest or holding a stroop. Fixed by
    checking the response's maker_pubkey actually resolves to the
    order's own maker_lapse_addr before counting it."""

    def test_a_response_signed_by_a_stranger_does_not_reserve_anything(self, maker, taker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])

        # 'taker' stands in for an attacker: a real, independent keypair
        # with nothing to do with this order, signing a well-formed,
        # genuinely-verifying 'accepted' response that simply names
        # someone else's order_id.
        forged = signed_fill_response(
            taker, order_id=order["order_id"], session_id="f" * 16,
            lapse_total=10 * LAPSE, accepted=True)
        assert market.verify_fill_response(forged)  # genuinely signed...
        assert market.store_fill_response(forged)   # ...and admitted, same as any relay would.

        # ...but it must not shrink what the order still has to offer.
        assert market.remaining_ticks(row, 100) == 10 * LAPSE
        assert len(market.open_orders(current_height=100)) == 1

    def test_a_response_from_the_real_maker_still_reserves(self, maker, taker):
        """The fix must not overcorrect into distrusting every response:
        one genuinely signed by the order's own maker still counts,
        exactly as before."""
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "g" * 16, 4 * LAPSE)
        assert market.remaining_ticks(row, 100) == 6 * LAPSE

    def test_reserved_ticks_is_zero_for_an_order_this_node_does_not_have(self):
        # Nothing to check the signer against, so nothing is trusted.
        assert market.reserved_ticks("no-such-order", 100) == 0


class TestExpiredReservationsFreeCapacity:
    """A trade that has stalled well past swap_engine.trade_expired_by's
    own margin must stop holding its order's capacity hostage: the
    order's own maker (or any bystander) can offer that capacity to
    someone else, exactly as if the stalled trade had never been
    accepted, without cancelling anything or waiting on it further."""

    # accepted_height=1000 (signed_fill_response's own default),
    # confirm_depth=2 -> step_timeout_blocks=12, increment_count=3 (also
    # defaults): worst_case_done = 1000 + 3*12 = 1036, + ABANDON_AFTER_
    # BLOCKS (30) = 1066. Below that height the reservation still counts;
    # at or past it, it does not.
    EXPIRY_HEIGHT = 1000 + 3 * 12 + 30

    def test_still_counted_before_the_expiry_margin(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 4 * LAPSE)
        assert market.remaining_ticks(row, self.EXPIRY_HEIGHT - 1) == 6 * LAPSE

    def test_freed_once_the_expiry_margin_passes(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 4 * LAPSE)
        assert market.remaining_ticks(row, self.EXPIRY_HEIGHT) == 10 * LAPSE

    def test_expired_reservation_can_be_re_offered_to_a_new_taker(self, maker):
        """The concrete payoff: validate_fill actually admits a fresh
        fill request for the freed capacity once the stalled one has
        expired, where it would have refused it before."""
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 10 * LAPSE)
        with pytest.raises(market.OrderRejected, match="left"):
            market.validate_fill(row, 5 * LAPSE, self.EXPIRY_HEIGHT - 1)
        market.validate_fill(row, 5 * LAPSE, self.EXPIRY_HEIGHT)   # must not raise

    def test_a_response_with_more_time_budget_is_not_expired_early(self, maker):
        """A trade with more steps, or a deeper confirm_depth, genuinely
        needs more time; the same height must not expire it just because
        a smaller trade would have been expired by then."""
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        _accepted_response(maker, order["order_id"], "s" * 16, 4 * LAPSE,
                           increment_count=20, confirm_depth=5)
        # worst_case_done = 1000 + 20*30 = 1600, +30 = 1630 - well past
        # this class's own EXPIRY_HEIGHT for the 3-step/depth-2 default.
        assert market.remaining_ticks(row, self.EXPIRY_HEIGHT) == 6 * LAPSE


class TestFillResponseAdmissionCapsPerSigner:
    """MAX_FILL_RESPONSES_TOTAL alone bounds the whole network's shared
    budget, not any one signer's share of it: without a per-signer cap,
    one attacker minting free, well-formed 'accepted' responses (see
    TestForgedResponsesDoNotReserve for why those are cheap to produce)
    could exhaust the entire global table, after which store_fill_response
    refuses every genuine maker's real accept, network-wide, until
    pruning catches up an hour later."""

    def test_one_signer_is_capped_independent_of_the_global_limit(self, maker, monkeypatch):
        monkeypatch.setattr(market, "MAX_FILL_RESPONSES_PER_MAKER", 3)
        for i in range(3):
            resp = signed_fill_response(
                maker, request_id=f"r{i}" * 8, order_id=f"o{i}",
                session_id=f"s{i}" * 4, accepted=False, reason="no room")
            assert market.store_fill_response(resp)
        one_more = signed_fill_response(
            maker, request_id="r-over" * 3, order_id="o-over",
            session_id="s-over" * 3, accepted=False, reason="no room")
        with pytest.raises(market.FillResponseRejected, match="already has"):
            market.store_fill_response(one_more)
        with pytest.raises(market.FillResponseRejected, match="already has"):
            market.verify_fill_response(one_more)


class TestOrderHash:
    def test_same_order_same_hash(self, maker):
        order = signed_order(maker)
        assert market.order_hash(order) == market.order_hash(dict(order))

    def test_padding_cannot_mint_a_new_identity(self, maker):
        """Otherwise every padded copy re-floods the network."""
        order = signed_order(maker)
        padded = dict(order, junk="x" * 1000)
        assert market.order_hash(padded) == market.order_hash(order)

    def test_different_orders_differ(self, maker):
        assert market.order_hash(signed_order(maker)) != \
               market.order_hash(signed_order(maker))


class TestValidateFill:
    """Shared by the taker's own request and the maker's independent
    re-check of a claim (see swap_engine.discover_trades): the maker
    must never take a taker's word that a fill fits, any more than a
    taker's own request is trusted without this."""

    def test_a_fill_within_bounds_is_accepted(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        market.validate_fill(row, 5 * LAPSE, 100)   # must not raise

    def test_zero_or_negative_is_refused(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        for bad in (0, -1):
            with pytest.raises(market.OrderRejected, match="positive"):
                market.validate_fill(row, bad, 100)

    def test_more_than_remaining_is_refused(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        with pytest.raises(market.OrderRejected, match="left"):
            market.validate_fill(row, 11 * LAPSE, 100)

    def test_below_min_fill_is_refused(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, min_fill=5 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        with pytest.raises(market.OrderRejected, match="min_fill|will not go"):
            market.validate_fill(row, 1 * LAPSE, 100)

    def test_above_max_fill_is_refused(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE, max_fill=3 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        with pytest.raises(market.OrderRejected, match="max fill"):
            market.validate_fill(row, 4 * LAPSE, 100)

    def test_accounts_for_what_is_already_delivered(self, maker):
        order = signed_order(maker, lapse_total=10 * LAPSE)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        # Nothing delivered yet, so the full remaining size still fits...
        market.validate_fill(row, 10 * LAPSE, 100)
        # ...but not more than the order ever had.
        with pytest.raises(market.OrderRejected):
            market.validate_fill(row, 10 * LAPSE + 1, 100)


class TestAlreadyKnown:
    """Backing market_routes/node's pre-verification dedup. Must answer
    from stored state, not from gossip's own seen-cache: the two track
    different things, and conflating them is what silently drops an order
    past its first hop (see node._handle_inbound_order)."""

    def test_unknown_order_is_not_known(self, maker):
        assert market.already_known(signed_order(maker)) is False

    def test_stored_order_is_known(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        assert market.already_known(order) is True

    def test_unrelated_order_is_still_unknown(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        assert market.already_known(signed_order(maker)) is False

    def test_uncancelled_order_cancellation_is_not_known(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        cancel = market.cancellation_for(order["order_id"], maker["pubkey"],
                                         maker["keyfile"], maker["kek"])
        assert market.already_known(cancel) is False

    def test_applied_cancellation_is_known(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        cancel = market.cancellation_for(order["order_id"], maker["pubkey"],
                                         maker["keyfile"], maker["kek"])
        market.apply_cancellation(order["order_id"], maker["addr"])
        assert market.already_known(cancel) is True

    def test_forged_cancellation_for_an_uncancelled_order_is_not_known(self, maker):
        """A fake cancellation naming a real order_id must still go through
        full verification: 'known' can only mean this node itself already
        applied it, never that the order_id merely exists."""
        order = signed_order(maker)
        market.store_order(order)
        forged = {"cancel": order["order_id"], "pubkey": maker["pubkey"],
                  "signature": "00" * 32}
        assert market.already_known(forged) is False

    def test_non_dict_is_not_known(self):
        assert market.already_known("not an order") is False
        assert market.already_known(None) is False


class TestOrdersByMaker:
    """The maker's own management view, distinct from open_orders (a
    taker's view of what is available): it must still show an order that
    is fully delivered, since the maker still wants to see it."""

    def test_shows_only_this_makers_orders(self, maker, tmp_path):
        market.store_order(signed_order(maker))
        other_sk, other_pk = crypto.generate_keypair()
        other_path = str(tmp_path / "other.key")
        crypto.save_key(other_path, other_sk, other_pk, "pw")
        other_kek = crypto.derive_kek(other_path, "pw")
        _seed, other_xlm = xlm_mod.generate_keypair()
        other = {"addr": crypto.public_key_to_address(other_pk),
                 "pubkey": other_pk.hex(), "keyfile": other_path,
                 "kek": other_kek, "xlm": other_xlm}
        market.store_order(signed_order(other))

        rows = market.orders_by_maker(maker["addr"], current_height=100)
        assert [r.maker_lapse_addr for r in rows] == [maker["addr"]]

    def test_cancelled_orders_are_excluded(self, maker):
        order = signed_order(maker)
        market.store_order(order)
        market.apply_cancellation(order["order_id"], maker["addr"])
        assert market.orders_by_maker(maker["addr"], current_height=100) == []

    def test_expired_orders_are_excluded(self, maker):
        market.store_order(signed_order(maker, expiry_block=200))
        assert market.orders_by_maker(maker["addr"], current_height=300) == []

    def test_fully_delivered_order_still_shows(self, maker):
        """Unlike open_orders, which drops it once nothing remains."""
        order = signed_order(maker, lapse_total=1 * LAPSE)
        market.store_order(order)
        Trade.create(
            session_id="s1", order_id=order["order_id"], role="maker",
            my_lapse_addr=maker["addr"], my_xlm_addr=maker["xlm"],
            peer_lapse_addr="taker.addr", peer_xlm_addr="GTAKER",
            i_send="lapse", lapse_total=1 * LAPSE, xlm_total=1 * XLM,
            increment_count=1, confirm_depth=2,
            status="completed", created_at=0, updated_at=0)
        Increment.create(
            id="s1:1", session_id="s1", n=1,
            lapse_amount=1 * LAPSE, xlm_amount=1 * XLM, i_move_first=False,
            out_state=LEG_SETTLED, in_state=LEG_SETTLED, created_at=0)

        assert market.open_orders(current_height=100) == []
        rows = market.orders_by_maker(maker["addr"], current_height=100)
        assert len(rows) == 1
        assert rows[0].order_id == order["order_id"]


class TestMakerCommitted:
    """A maker's own combined exposure across every live order, which no
    single order's own post-time check ever sees (market_routes
    ._place_order only checks the one order being posted against the
    balance at that moment)."""

    def test_no_orders_is_nothing_committed(self, maker):
        assert market.maker_committed(maker["addr"], current_height=100) == (0, 0)

    def test_a_sell_order_commits_lapse_only(self, maker):
        market.store_order(signed_order(maker, direction="sell", lapse_total=5 * LAPSE))
        lapse, xlm = market.maker_committed(maker["addr"], current_height=100)
        assert lapse == 5 * LAPSE
        assert xlm == 0

    def test_a_buy_order_commits_xlm_only(self, maker):
        market.store_order(signed_order(maker, direction="buy",
                                        lapse_total=5 * LAPSE, price=1000))
        lapse, xlm = market.maker_committed(maker["addr"], current_height=100)
        assert lapse == 0
        assert xlm == market.swap_mod.xlm_for_lapse(5 * LAPSE, 1000)

    def test_two_sell_orders_sum(self, maker):
        market.store_order(signed_order(maker, order_id="o1", direction="sell",
                                        lapse_total=5 * LAPSE))
        market.store_order(signed_order(maker, order_id="o2", direction="sell",
                                        lapse_total=7 * LAPSE))
        lapse, _xlm = market.maker_committed(maker["addr"], current_height=100)
        assert lapse == 12 * LAPSE

    def test_delivered_or_reserved_amounts_reduce_what_is_still_committed(self, maker, taker):
        """Committed means still outstanding, not the order's original
        size: what has already settled or been accepted no longer
        threatens a future balance check."""
        order = signed_order(maker, direction="sell", lapse_total=10 * LAPSE)
        market.store_order(order)
        market.store_fill_response(
            signed_fill_response(maker, order_id=order["order_id"],
                                 session_id="s" * 16, lapse_total=4 * LAPSE))
        lapse, _xlm = market.maker_committed(maker["addr"], current_height=100)
        assert lapse == 6 * LAPSE

    def test_exclude_order_id_leaves_that_order_out(self, maker):
        order = signed_order(maker, direction="sell", lapse_total=5 * LAPSE)
        market.store_order(order)
        lapse, _xlm = market.maker_committed(
            maker["addr"], current_height=100, exclude_order_id=order["order_id"])
        assert lapse == 0

    def test_a_cancelled_order_does_not_count(self, maker):
        order = signed_order(maker, direction="sell", lapse_total=5 * LAPSE)
        market.store_order(order)
        market.apply_cancellation(order["order_id"], maker["addr"])
        lapse, _xlm = market.maker_committed(maker["addr"], current_height=100)
        assert lapse == 0

    def test_someone_elses_orders_are_not_counted(self, maker, taker):
        market.store_order(signed_order(maker, direction="sell", lapse_total=5 * LAPSE))
        lapse, _xlm = market.maker_committed(taker["addr"], current_height=100)
        assert lapse == 0


class TestOrdersByMakerWithClaims:
    """Discovery's own view: unlike orders_by_maker, a cancelled or
    expired order must still surface here if a fill request (and
    therefore possibly an agreed fill) is already waiting against it."""

    def test_an_order_with_no_claims_is_absent(self, maker):
        market.store_order(signed_order(maker))
        assert market.orders_by_maker_with_claims(maker["addr"]) == []

    def test_an_open_order_with_a_claim_shows(self, maker, taker):
        order = signed_order(maker)
        market.store_order(order)
        market.store_fill_request(
            signed_fill_request(taker, order_id=order["order_id"]))
        rows = market.orders_by_maker_with_claims(maker["addr"])
        assert [r.order_id for r in rows] == [order["order_id"]]

    def test_a_cancelled_order_with_a_claim_still_shows(self, maker, taker):
        """A cancellation withdraws what is unfilled, not what already
        has an agreed fill against it (see market.py's own docstring)."""
        order = signed_order(maker)
        market.store_order(order)
        market.store_fill_request(
            signed_fill_request(taker, order_id=order["order_id"]))
        market.apply_cancellation(order["order_id"], maker["addr"])
        rows = market.orders_by_maker_with_claims(maker["addr"])
        assert [r.order_id for r in rows] == [order["order_id"]]

    def test_someone_elses_order_is_never_returned(self, maker, taker, tmp_path):
        other_sk, other_pk = crypto.generate_keypair()
        other_path = str(tmp_path / "unrelated.key")
        crypto.save_key(other_path, other_sk, other_pk, "pw")
        other_kek = crypto.derive_kek(other_path, "pw")
        _seed, other_xlm = xlm_mod.generate_keypair()
        other = {"addr": crypto.public_key_to_address(other_pk),
                 "pubkey": other_pk.hex(), "keyfile": other_path,
                 "kek": other_kek, "xlm": other_xlm}
        order = signed_order(other)
        market.store_order(order)
        market.store_fill_request(
            signed_fill_request(taker, order_id=order["order_id"]))
        assert market.orders_by_maker_with_claims(maker["addr"]) == []


class TestFillRequests:
    """A fill request only ever proves control of the LapseCoin address
    it names. Whether it makes sense against a specific order (remaining
    size, this node's own exposure cap for this taker) is the maker's
    job when it actually decides how to answer, not checked here."""

    def test_signed_request_verifies(self, taker):
        assert market.verify_fill_request(signed_fill_request(taker)) is True

    def test_unsigned_request_is_refused(self, taker):
        req = market.build_fill_request(
            "order-1", "s" * 16, taker["addr"], taker["xlm"], 1 * LAPSE, taker["pubkey"])
        with pytest.raises(market.FillRequestRejected, match="signature"):
            market.verify_fill_request(req)

    @pytest.mark.parametrize("field,value", [
        ("lapse_total", 5 * LAPSE),
        ("taker_xlm_addr", None),
        ("session_id", "different-session"),
    ])
    def test_tampering_breaks_the_signature(self, taker, field, value):
        req = signed_fill_request(taker)
        if value is None:
            _seed, value = xlm_mod.generate_keypair()
        req[field] = value
        with pytest.raises(market.FillRequestRejected, match="signature"):
            market.verify_fill_request(req)

    def test_requesting_from_someone_elses_lapse_address_is_refused(self, taker, maker):
        req = signed_fill_request(taker)
        req["taker_lapse_addr"] = maker["addr"]
        with pytest.raises(market.FillRequestRejected, match="pubkey does not match"):
            market.verify_fill_request(req)

    def test_extra_field_is_refused_not_ignored(self, taker):
        req = signed_fill_request(taker)
        req["surprise"] = "x"
        with pytest.raises(market.FillRequestRejected, match="unexpected"):
            market.verify_fill_request(req)

    def test_missing_field_refused(self, taker):
        req = signed_fill_request(taker)
        del req["lapse_total"]
        with pytest.raises(market.FillRequestRejected, match="missing"):
            market.verify_fill_request(req)

    def test_non_positive_lapse_total_refused(self, taker):
        with pytest.raises(market.FillRequestRejected):
            market.verify_fill_request(signed_fill_request(taker, lapse_total=0))

    def test_bool_is_not_an_integer(self, taker):
        req = signed_fill_request(taker)
        req["lapse_total"] = True
        with pytest.raises(market.FillRequestRejected, match="integer"):
            market.verify_fill_request(req)

    def test_bad_lapse_address_refused(self, taker):
        with pytest.raises(market.FillRequestRejected):
            market.verify_fill_request(signed_fill_request(taker, taker_lapse_addr="not.an.address"))

    def test_bad_stellar_address_refused(self, taker):
        with pytest.raises(market.FillRequestRejected, match="Stellar"):
            market.verify_fill_request(signed_fill_request(taker, taker_xlm_addr="GNOPE"))

    def test_non_dict_refused(self):
        with pytest.raises(market.FillRequestRejected):
            market.verify_fill_request("not a request")


class TestFillRequestAdmissionRunsBeforeTheSignatureCheck:
    def test_a_taker_already_at_its_cap_is_refused_pre_signature(self, taker):
        for i in range(market.MAX_FILL_REQUESTS_PER_TAKER):
            market.store_fill_request(signed_fill_request(taker, session_id=f"s{i}" * 4))
        with pytest.raises(market.FillRequestRejected, match="limit"):
            market.verify_fill_request(signed_fill_request(taker, session_id="overflow" * 2))

    def test_a_full_request_book_is_refused_pre_signature(self, taker, monkeypatch):
        monkeypatch.setattr(market, "MAX_FILL_REQUESTS_TOTAL", 1)
        market.store_fill_request(signed_fill_request(taker, session_id="a" * 16))
        with pytest.raises(market.FillRequestRejected, match="full"):
            market.verify_fill_request(signed_fill_request(taker, session_id="b" * 16))

    def test_admission_does_not_let_a_forged_request_through(self, taker):
        req = signed_fill_request(taker)
        req["lapse_total"] = 999 * LAPSE
        with pytest.raises(market.FillRequestRejected, match="signature"):
            market.verify_fill_request(req)


class TestFillRequestStorage:
    def test_store_and_read_back(self, taker):
        req = signed_fill_request(taker)
        assert market.store_fill_request(req) is True
        assert market.get_fill_request(req["request_id"]).lapse_total == 1 * LAPSE

    def test_duplicate_request_id_is_not_stored_twice(self, taker):
        req = signed_fill_request(taker)
        assert market.store_fill_request(req) is True
        assert market.store_fill_request(req) is False

    def test_one_taker_cannot_fill_the_request_book(self, taker):
        for i in range(market.MAX_FILL_REQUESTS_PER_TAKER):
            market.store_fill_request(signed_fill_request(taker, session_id=f"s{i}" * 4))
        with pytest.raises(market.FillRequestRejected, match="limit"):
            market.store_fill_request(signed_fill_request(taker, session_id="overflow" * 2))

    def test_the_request_book_itself_has_a_ceiling(self, taker, monkeypatch):
        monkeypatch.setattr(market, "MAX_FILL_REQUESTS_TOTAL", 2)
        market.store_fill_request(signed_fill_request(taker, session_id="a" * 16))
        market.store_fill_request(signed_fill_request(taker, session_id="b" * 16))
        with pytest.raises(market.FillRequestRejected, match="full"):
            market.store_fill_request(signed_fill_request(taker, session_id="c" * 16))

    def test_requests_for_order_scopes_by_order(self, taker):
        market.store_fill_request(signed_fill_request(taker, order_id="order-a", session_id="a" * 16))
        market.store_fill_request(signed_fill_request(taker, order_id="order-b", session_id="b" * 16))
        rows = market.requests_for_order("order-a")
        assert [r.session_id for r in rows] == ["a" * 16]

    def test_prune_removes_old_requests(self, taker):
        req = signed_fill_request(taker)
        market.store_fill_request(req)
        removed = market.prune_fill_requests(now=time.time() + market.FILL_REQUEST_MAX_AGE_SECONDS + 1)
        assert removed == 1
        assert market.get_fill_request(req["request_id"]) is None

    def test_prune_leaves_recent_requests(self, taker):
        req = signed_fill_request(taker)
        market.store_fill_request(req)
        assert market.prune_fill_requests(now=time.time()) == 0
        assert market.get_fill_request(req["request_id"]) is not None

    def test_a_request_with_an_accepted_response_is_never_pruned(self, taker, maker):
        """Paired with its own accepted FillResponse, a request is now
        part of the durable trade record itself (see
        market.accepted_fills_for_addr) and must survive regardless of
        age, the same way the accepted response beside it does."""
        req = signed_fill_request(taker, request_id="req1" * 4)
        market.store_fill_request(req)
        resp = signed_fill_response(maker, request_id="req1" * 4, accepted=True)
        market.store_fill_response(resp)
        removed = market.prune_fill_requests(
            now=time.time() + market.FILL_REQUEST_MAX_AGE_SECONDS + 100_000)
        assert removed == 0
        assert market.get_fill_request("req1" * 4) is not None


class TestAlreadyKnownFillRequest:
    def test_unknown_request_is_not_known(self, taker):
        assert market.already_known_fill_request(signed_fill_request(taker)) is False

    def test_stored_request_is_known(self, taker):
        req = signed_fill_request(taker)
        market.store_fill_request(req)
        assert market.already_known_fill_request(req) is True

    def test_non_dict_is_not_known(self):
        assert market.already_known_fill_request("nope") is False
        assert market.already_known_fill_request(None) is False


class TestFillResponses:
    def test_signed_accept_verifies(self, maker):
        assert market.verify_fill_response(signed_fill_response(maker, accepted=True)) is True

    def test_signed_reject_verifies(self, maker):
        assert market.verify_fill_response(signed_fill_response(maker, accepted=False)) is True

    def test_expected_maker_addr_matching_passes(self, maker):
        resp = signed_fill_response(maker)
        assert market.verify_fill_response(resp, expected_maker_addr=maker["addr"]) is True

    def test_signed_by_the_wrong_party_is_refused(self, maker, taker):
        """A stranger cannot sign their own 'acceptance' of someone
        else's order and have it mistaken for that order's real maker
        agreeing: the caller acting on a response must always check the
        signer against the order's own known maker address."""
        resp = signed_fill_response(taker)   # signed by the wrong key entirely
        with pytest.raises(market.FillResponseRejected, match="not signed by the maker"):
            market.verify_fill_response(resp, expected_maker_addr=maker["addr"])

    def test_unsigned_response_is_refused(self, maker):
        resp = market.build_fill_response(
            "r" * 16, "order-1", "s" * 16, 1 * LAPSE, True, maker["pubkey"],
            accepted_height=1000, confirm_depth=2,
            xlm_total=1000 * XLM, direction="sell", maker_xlm_addr=maker["xlm"],
            maker_opens=True, increment_count=3)
        with pytest.raises(market.FillResponseRejected, match="signature"):
            market.verify_fill_response(resp)

    def test_tampering_breaks_the_signature(self, maker):
        resp = signed_fill_response(maker)
        resp["lapse_total"] = 5 * LAPSE
        with pytest.raises(market.FillResponseRejected, match="signature"):
            market.verify_fill_response(resp)

    def test_accepted_without_increment_count_is_refused(self, maker):
        resp = signed_fill_response(maker, accepted=True)
        resp["increment_count"] = None
        with pytest.raises(market.FillResponseRejected, match="integer"):
            market.verify_fill_response(resp)

    def test_rejected_with_an_increment_count_is_refused(self, maker):
        resp = signed_fill_response(maker, accepted=False)
        resp["increment_count"] = 3
        with pytest.raises(market.FillResponseRejected, match="null"):
            market.verify_fill_response(resp)

    @pytest.mark.parametrize("bad", [1, 21, 0, -1])
    def test_increment_count_out_of_range_refused(self, maker, bad):
        with pytest.raises(market.FillResponseRejected, match="increment_count"):
            market.verify_fill_response(signed_fill_response(maker, accepted=True, increment_count=bad))

    def test_extra_field_is_refused_not_ignored(self, maker):
        resp = signed_fill_response(maker)
        resp["surprise"] = "x"
        with pytest.raises(market.FillResponseRejected, match="unexpected"):
            market.verify_fill_response(resp)

    def test_missing_field_refused(self, maker):
        resp = signed_fill_response(maker)
        del resp["reason"]
        with pytest.raises(market.FillResponseRejected, match="missing"):
            market.verify_fill_response(resp)

    def test_non_dict_refused(self):
        with pytest.raises(market.FillResponseRejected):
            market.verify_fill_response("not a response")

    def test_bad_direction_refused(self, maker):
        resp = signed_fill_response(maker, direction="sideways")
        with pytest.raises(market.FillResponseRejected, match="direction"):
            market.verify_fill_response(resp)

    def test_invalid_maker_xlm_addr_refused(self, maker):
        resp = signed_fill_response(maker, maker_xlm_addr="not-a-stellar-address")
        with pytest.raises(market.FillResponseRejected, match="Stellar"):
            market.verify_fill_response(resp)

    def test_non_positive_xlm_total_refused(self, maker):
        resp = signed_fill_response(maker, xlm_total=0)
        with pytest.raises(market.FillResponseRejected, match="xlm_total"):
            market.verify_fill_response(resp)

    def test_non_bool_maker_opens_refused(self, maker):
        resp = signed_fill_response(maker)
        resp["maker_opens"] = "yes"
        with pytest.raises(market.FillResponseRejected, match="maker_opens"):
            market.verify_fill_response(resp)


class TestFillResponseOrderTermsCheck:
    """order_row, when passed to verify_fill_response, catches a maker
    signing an accepted response whose own terms diverge from the order
    it claims to answer - a gap every other check here misses, since
    each field is only ever validated in isolation. Without this, a
    bystander reconstructing this trade later (see swap_engine.
    verify_trade_against_chain) would trust whatever the response signs
    and look for a payment the real trade, built from the order itself,
    was never going to make (see _open_taker_trade's own docstring)."""

    def _matching_response(self, maker, order, **overrides):
        lapse_total = overrides.pop("lapse_total", 2 * LAPSE)
        xlm_total = swap.xlm_for_lapse(lapse_total, order["price_stroops_per_lapse"])
        return signed_fill_response(
            maker, order_id=order["order_id"], lapse_total=lapse_total,
            direction=overrides.pop("direction", order["direction"]),
            maker_xlm_addr=overrides.pop("maker_xlm_addr", order["maker_xlm_addr"]),
            xlm_total=overrides.pop("xlm_total", xlm_total), **overrides)

    def test_matching_terms_pass(self, maker):
        order = signed_order(maker, direction="sell", price=1000)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        resp = self._matching_response(maker, order)
        assert market.verify_fill_response(resp, order_row=row) is True

    def test_mismatched_direction_refused(self, maker):
        order = signed_order(maker, direction="sell", price=1000)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        resp = self._matching_response(maker, order)
        resp["direction"] = "buy"
        with pytest.raises(market.FillResponseRejected, match="direction"):
            market.verify_fill_response(resp, order_row=row)

    def test_mismatched_maker_xlm_addr_refused(self, maker, taker):
        order = signed_order(maker, direction="sell", price=1000)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        resp = self._matching_response(maker, order, maker_xlm_addr=taker["xlm"])
        with pytest.raises(market.FillResponseRejected, match="maker_xlm_addr"):
            market.verify_fill_response(resp, order_row=row)

    def test_mismatched_xlm_total_refused(self, maker):
        """The concrete exploit this closes: a maker signing a real
        accept but naming an xlm_total that does not match its own
        order's price, so a bystander reconstructing the trade would
        look for a payment of the wrong size."""
        order = signed_order(maker, direction="sell", price=1000)
        market.store_order(order)
        row = market.get_order(order["order_id"])
        resp = self._matching_response(maker, order)
        resp["xlm_total"] = resp["xlm_total"] // 2
        with pytest.raises(market.FillResponseRejected, match="xlm_total"):
            market.verify_fill_response(resp, order_row=row)

    def test_no_order_row_skips_the_check(self, maker):
        """The same courtesy expected_maker_addr=None already extends: a
        plain relay that does not have the order this response answers
        cannot check its terms either, and must not be made to."""
        order = signed_order(maker, direction="sell", price=1000)
        # Genuinely signed with a direction that contradicts the order
        # (not mutated after signing, so the signature itself is real)
        # - would fail the order-terms check if order_row were given.
        resp = self._matching_response(maker, order, direction="buy")
        assert market.verify_fill_response(resp) is True


class TestFillResponseStorage:
    def test_store_and_read_back(self, maker):
        resp = signed_fill_response(maker, request_id="req1" * 4)
        assert market.store_fill_response(resp) is True
        assert market.get_fill_response("req1" * 4).lapse_total == 1 * LAPSE

    def test_duplicate_request_id_is_not_stored_twice(self, maker):
        resp = signed_fill_response(maker, request_id="req1" * 4)
        assert market.store_fill_response(resp) is True
        assert market.store_fill_response(resp) is False

    def test_the_pending_response_book_has_a_ceiling(self, maker, monkeypatch):
        """MAX_FILL_RESPONSES_TOTAL bounds the pending/declined population
        only; an accepted response is a different, separately-capped
        population (see test_the_accepted_response_book_has_its_own_ceiling)."""
        monkeypatch.setattr(market, "MAX_FILL_RESPONSES_TOTAL", 2)
        market.store_fill_response(signed_fill_response(
            maker, request_id="a" * 16, accepted=False))
        market.store_fill_response(signed_fill_response(
            maker, request_id="b" * 16, accepted=False))
        with pytest.raises(market.FillResponseRejected, match="full"):
            market.store_fill_response(signed_fill_response(
                maker, request_id="c" * 16, accepted=False))

    def test_the_accepted_response_book_has_its_own_ceiling(self, maker, monkeypatch):
        monkeypatch.setattr(market, "MAX_ACCEPTED_RESPONSES_TOTAL", 2)
        market.store_fill_response(signed_fill_response(maker, request_id="a" * 16))
        market.store_fill_response(signed_fill_response(maker, request_id="b" * 16))
        with pytest.raises(market.FillResponseRejected, match="full"):
            market.store_fill_response(signed_fill_response(maker, request_id="c" * 16))

    def test_prune_removes_old_pending_responses(self, maker):
        resp = signed_fill_response(maker, request_id="req1" * 4, accepted=False)
        market.store_fill_response(resp)
        removed = market.prune_fill_responses(
            now=time.time() + market.FILL_RESPONSE_MAX_AGE_SECONDS + 1)
        assert removed == 1
        assert market.get_fill_response("req1" * 4) is None

    def test_prune_leaves_recent_responses(self, maker):
        resp = signed_fill_response(maker, request_id="req1" * 4, accepted=False)
        market.store_fill_response(resp)
        assert market.prune_fill_responses(now=time.time()) == 0
        assert market.get_fill_response("req1" * 4) is not None

    def test_an_accepted_response_is_never_pruned(self, maker):
        """The whole point of the redesign this covers: paired with its
        own FillRequest, an accepted response is now the durable trade
        record itself (see market.py's FILL_RESPONSE_SIGNED_FIELDS), so
        it must survive prune_fill_responses regardless of age."""
        resp = signed_fill_response(maker, request_id="req1" * 4, accepted=True)
        market.store_fill_response(resp)
        removed = market.prune_fill_responses(
            now=time.time() + market.FILL_RESPONSE_MAX_AGE_SECONDS + 100_000)
        assert removed == 0
        assert market.get_fill_response("req1" * 4) is not None


class TestAlreadyKnownFillResponse:
    def test_unknown_response_is_not_known(self, maker):
        assert market.already_known_fill_response(signed_fill_response(maker)) is False

    def test_stored_response_is_known(self, maker):
        resp = signed_fill_response(maker)
        market.store_fill_response(resp)
        assert market.already_known_fill_response(resp) is True

    def test_non_dict_is_not_known(self):
        assert market.already_known_fill_response("nope") is False
        assert market.already_known_fill_response(None) is False


class TestDepth:
    def test_sides_are_separated_and_sorted(self, maker):
        market.store_order(signed_order(maker, direction="sell", price=1200))
        market.store_order(signed_order(maker, direction="sell", price=1000))
        market.store_order(signed_order(maker, direction="buy", price=800))
        market.store_order(signed_order(maker, direction="buy", price=900))
        depth = market.book_depth(current_height=100)
        assert [e["price"] for e in depth["sells"]] == [1000, 1200]
        assert [e["price"] for e in depth["buys"]] == [900, 800]

    def test_best_prices_and_spread(self, maker):
        market.store_order(signed_order(maker, direction="sell", price=1000))
        market.store_order(signed_order(maker, direction="buy", price=900))
        best = market.best_prices(current_height=100)
        assert best["best_sell"] == 1000
        assert best["best_buy"] == 900
        assert best["spread"] == 100

    def test_empty_book_reports_none_not_a_fake_price(self, maker):
        """On a new coin an empty side is ordinary; the UI must be able to
        say so rather than show a price that does not exist."""
        best = market.best_prices(current_height=100)
        assert best["best_buy"] is None
        assert best["best_sell"] is None
        assert best["spread"] is None

    def test_own_orders_can_be_excluded(self, maker):
        market.store_order(signed_order(maker))
        assert market.open_orders(100, exclude_maker=maker["addr"]) == []

    def test_depth_totals_remaining(self, maker):
        market.store_order(signed_order(maker, direction="sell",
                                        lapse_total=5 * LAPSE))
        market.store_order(signed_order(maker, direction="sell",
                                        lapse_total=3 * LAPSE))
        assert market.best_prices(100)["sell_depth"] == 8 * LAPSE


class TestListOrders:
    """The order book page's own view: one side, paged and sorted.
    Unlike book_depth, "price" and "recent" are pushed to SQL (ordering
    and paging on stored, indexed columns), and remaining_ticks is only
    ever computed for rows actually being considered for the page, so
    the cost of a page does not grow with how many orders are on the
    book."""

    def test_default_sort_matches_book_depth_price_order(self, maker):
        market.store_order(signed_order(maker, direction="sell", price=1200))
        market.store_order(signed_order(maker, direction="sell", price=1000))
        page, total = market.list_orders(100, "sell")
        assert total == 2
        assert [e["price"] for e in page] == [1000, 1200]

    def test_pagination_slices_without_losing_the_total(self, maker):
        for i in range(5):
            market.store_order(signed_order(maker, order_id=f"o{i}", price=1000 + i))
        page, total = market.list_orders(100, "sell", offset=0, limit=2)
        assert total == 5
        assert [e["price"] for e in page] == [1000, 1001]
        page2, total2 = market.list_orders(100, "sell", offset=2, limit=2)
        assert total2 == 5
        assert [e["price"] for e in page2] == [1002, 1003]

    def test_amount_sort_is_largest_first(self, maker):
        market.store_order(signed_order(maker, order_id="small",
                                        direction="sell", lapse_total=1 * LAPSE))
        market.store_order(signed_order(maker, order_id="big",
                                        direction="sell", lapse_total=9 * LAPSE))
        page, _total = market.list_orders(100, "sell", sort="amount")
        assert [e["remaining"] for e in page] == [9 * LAPSE, 1 * LAPSE]

    def test_recent_sort_is_newest_first(self, maker):
        # received_at is set by store_order itself (time.time()), not
        # taken from the signed payload, so the two calls are given
        # explicit, deterministic timestamps afterward rather than
        # relying on real clock time to differ between them.
        market.store_order(signed_order(maker, order_id="old", price=1000))
        market.store_order(signed_order(maker, order_id="new", price=1000))
        Order.update(received_at=1000).where(Order.order_id == "old").execute()
        Order.update(received_at=2000).where(Order.order_id == "new").execute()
        page, _total = market.list_orders(100, "sell", sort="recent")
        assert [e["order_id"] for e in page] == ["new", "old"]

    def test_an_unknown_sort_falls_back_to_price(self, maker):
        market.store_order(signed_order(maker, direction="sell", price=1200))
        market.store_order(signed_order(maker, direction="sell", price=1000))
        page, _total = market.list_orders(100, "sell", sort="nonsense")
        assert [e["price"] for e in page] == [1000, 1200]

    def test_buy_and_sell_are_independent(self, maker):
        market.store_order(signed_order(maker, direction="sell", price=1000))
        market.store_order(signed_order(maker, direction="buy", price=900))
        sells, _t1 = market.list_orders(100, "sell")
        buys, _t2 = market.list_orders(100, "buy")
        assert len(sells) == 1 and len(buys) == 1
        assert sells[0]["price"] == 1000
        assert buys[0]["price"] == 900

    def test_excludes_own_orders_like_open_orders_does(self, maker):
        market.store_order(signed_order(maker, direction="sell"))
        page, total = market.list_orders(100, "sell", exclude_maker=maker["addr"])
        assert page == [] and total == 0

    def test_a_remainder_below_min_fill_is_excluded_from_every_sort(self, maker):
        order = signed_order(maker, direction="sell", lapse_total=10 * LAPSE,
                             min_fill=5 * LAPSE)
        market.store_order(order)
        _accepted_response(maker, order["order_id"], "s" * 16, 7 * LAPSE)  # remaining: 3, below min_fill
        for sort in market.BOOK_SORTS:
            page, _total = market.list_orders(100, "sell", sort=sort)
            assert page == [], f"sort={sort!r} should have hidden the dust remainder"

    def test_price_and_recent_pagination_is_pushed_to_sql(self, maker, monkeypatch):
        """remaining_ticks (the one genuinely live computation here) must
        only ever run for rows in the requested page, not the whole side,
        for the two sorts that can be paged in SQL."""
        for i in range(5):
            market.store_order(signed_order(maker, order_id=f"o{i}", price=1000 + i))
        calls = []
        real_remaining = market.remaining_ticks
        monkeypatch.setattr(market, "remaining_ticks",
                            lambda row, h: calls.append(row.order_id) or real_remaining(row, h))
        for sort in ("price", "recent"):
            calls.clear()
            page, total = market.list_orders(100, "sell", sort=sort, offset=1, limit=2)
            assert total == 5
            assert len(page) == 2
            assert len(calls) == 2, f"sort={sort!r} computed remaining for {len(calls)} rows, not just the page"

    def test_amount_sort_is_bounded_to_the_candidate_window(self, maker, monkeypatch):
        """A page must not cost more than the documented window's worth
        of remaining_ticks calls, however large the book is."""
        monkeypatch.setattr(market, "AMOUNT_SORT_CANDIDATE_WINDOW", 3)
        for i in range(10):
            market.store_order(signed_order(maker, order_id=f"o{i}",
                                            lapse_total=(i + 1) * LAPSE))
        calls = []
        real_remaining = market.remaining_ticks
        monkeypatch.setattr(market, "remaining_ticks",
                            lambda row, h: calls.append(row.order_id) or real_remaining(row, h))
        page, total = market.list_orders(100, "sell", sort="amount", limit=2)
        assert total == 10          # the count is not windowed, only the ranking is
        assert len(calls) == 3      # bounded to the (patched) window, not all 10 orders


# ---------------------------------------------------------------------------
# Ticker (plan item 5.2): a weighted median of this node's own completed
# trades, not a network-wide feed.
# ---------------------------------------------------------------------------

class _TickerState:
    def __init__(self, balances=None):
        self.balances = balances or {}

    def get_balance(self, addr):
        return self.balances.get(addr, 0)


class _TickerView:
    def __init__(self, balances=None):
        self.height = 1000
        self.chain = [{"height": 1000}]
        self.state = _TickerState(balances)


class _TickerStorage:
    def __init__(self, heights_by_addr=None):
        self.heights_by_addr = heights_by_addr or {}

    def get_tx_heights_for_addr(self, addr):
        return self.heights_by_addr.get(addr, [])


class _TickerNode:
    """Just enough of a node for trust.get_detail/address_age_blocks,
    which the ticker's weighting leans on."""

    def __init__(self, addr="me.lapse", balances=None, heights_by_addr=None):
        self.addr = addr
        self.view = _TickerView(balances)
        self.storage = _TickerStorage(heights_by_addr)


def _completed_trade(session_id, peer, lapse_total, xlm_total, updated_at=None):
    now = updated_at if updated_at is not None else time.time()
    return Trade.create(
        session_id=session_id, order_id="order-x", role="taker",
        my_lapse_addr="me.lapse", my_xlm_addr="GME",
        peer_lapse_addr=peer, peer_xlm_addr="GPEER",
        i_send="xlm", lapse_total=lapse_total, xlm_total=xlm_total,
        increment_count=1, confirm_depth=1,
        status=trade_storage.TRADE_COMPLETED, created_at=now, updated_at=now)


class TestTicker:
    def test_no_completed_trades_is_none(self):
        node = _TickerNode()
        assert market.ticker_price(node) is None

    def test_a_single_trades_price_is_reported_exactly(self):
        _completed_trade("s1", "peer.lapse", 10 * LAPSE, 10_000 * XLM)
        node = _TickerNode()
        price = market.ticker_price(node)
        assert price == 10_000 * XLM * 100_000_000 // (10 * LAPSE)

    def test_an_active_trade_is_not_counted(self):
        Trade.create(
            session_id="active1", order_id="order-x", role="taker",
            my_lapse_addr="me.lapse", my_xlm_addr="GME",
            peer_lapse_addr="peer.lapse", peer_xlm_addr="GPEER",
            i_send="xlm", lapse_total=1 * LAPSE, xlm_total=1000 * XLM,
            increment_count=1, confirm_depth=1,
            status=trade_storage.TRADE_ACTIVE,
            created_at=time.time(), updated_at=time.time())
        node = _TickerNode()
        assert market.ticker_price(node) is None

    def test_median_is_not_dragged_by_one_outlier_the_way_a_mean_would_be(self):
        """A mean of {1000, 1000, 1000, 1_000_000} is dominated by the
        outlier; a median of an odd count is simply the middle value."""
        node = _TickerNode(balances={"peer.lapse": 10**12})
        for i in range(3):
            _completed_trade(f"s{i}", "peer.lapse", 1 * LAPSE, 1000 * XLM)
        _completed_trade("outlier", "peer.lapse", 1 * LAPSE, 1_000_000 * XLM)
        price = market.ticker_price(node)
        normal_price = 1000 * XLM * 100_000_000 // (1 * LAPSE)
        assert price == normal_price

    def test_a_trusted_counterpartys_trades_outweigh_a_strangers(self):
        """Weighting by standing means a handful of trades against a
        long-standing, staked counterparty should win a plain vote
        against a larger number of trades with a brand-new stranger."""
        trusted = "trusted.lapse"
        stranger = "stranger.lapse"
        node = _TickerNode(
            balances={trusted: 10**15},
            heights_by_addr={trusted: [(1, "h")]})
        # Prior standing, at the same per-unit price as the sample below
        # so it does not itself skew the median: trust is now read
        # straight off completed Trade rows (trust.local_tally), so
        # establishing it is just another one of those.
        _completed_trade("trusted-history", trusted, 50 * LAPSE, 50 * 100_000 * XLM)

        low_price = 100 * XLM * 100_000_000 // (1 * LAPSE)
        high_price = 100_000 * XLM * 100_000_000 // (1 * LAPSE)
        _completed_trade("trusted1", trusted, 1 * LAPSE, 100_000 * XLM)
        for i in range(5):
            _completed_trade(f"stranger{i}", stranger, 1 * LAPSE, 100 * XLM)

        price = market.ticker_price(node)
        assert price == high_price, \
            "the single trusted trade should outweigh five stranger ones"

    def test_the_limit_caps_how_far_back_it_looks(self):
        node = _TickerNode()
        for i in range(5):
            _completed_trade(f"old{i}", "peer.lapse", 1 * LAPSE, 100 * XLM,
                             updated_at=1000 + i)
        for i in range(3):
            _completed_trade(f"new{i}", "peer.lapse", 1 * LAPSE, 100_000 * XLM,
                             updated_at=2000 + i)
        samples = market.executed_trade_prices(node, limit=3)
        assert len(samples) == 3
        high_price = 100_000 * XLM * 100_000_000 // (1 * LAPSE)
        assert all(p == high_price for p, _w in samples)

    def test_zero_lapse_total_is_never_a_divide_by_zero(self):
        Trade.create(
            session_id="zero1", order_id="order-x", role="taker",
            my_lapse_addr="me.lapse", my_xlm_addr="GME",
            peer_lapse_addr="peer.lapse", peer_xlm_addr="GPEER",
            i_send="xlm", lapse_total=0, xlm_total=1000 * XLM,
            increment_count=1, confirm_depth=1,
            status=trade_storage.TRADE_COMPLETED,
            created_at=time.time(), updated_at=time.time())
        node = _TickerNode()
        assert market.ticker_price(node) is None


# ---------------------------------------------------------------------------
# Accepted fills: the durable trade record that replaced step receipts
# ---------------------------------------------------------------------------

def _stored_accepted_fill(maker, taker, request_id="r" * 16, order_id="order-1",
                          session_id="s" * 16, **overrides):
    """Store and return (request, response) for one accepted fill,
    admitted through the exact same verify-then-store path an inbound
    gossip message would use."""
    req = signed_fill_request(taker, order_id=order_id, session_id=session_id,
                              request_id=request_id)
    resp = signed_fill_response(maker, request_id=request_id, order_id=order_id,
                                session_id=session_id, accepted=True, **overrides)
    market.store_fill_request(req)
    market.store_fill_response(resp)
    return req, resp


class TestAcceptedFillsForAddr:
    def test_found_for_both_maker_and_taker(self, maker, taker):
        _stored_accepted_fill(maker, taker)
        assert len(market.accepted_fills_for_addr(maker["addr"])) == 1
        assert len(market.accepted_fills_for_addr(taker["addr"])) == 1

    def test_not_found_for_an_unrelated_address(self, maker, taker):
        _stored_accepted_fill(maker, taker)
        assert market.accepted_fills_for_addr("someone.else") == []

    def test_a_declined_response_is_not_an_accepted_fill(self, maker, taker):
        req = signed_fill_request(taker, request_id="r" * 16)
        resp = signed_fill_response(maker, request_id="r" * 16, accepted=False)
        market.store_fill_request(req)
        market.store_fill_response(resp)
        assert market.accepted_fills_for_addr(maker["addr"]) == []

    def test_pair_carries_both_signed_rows(self, maker, taker):
        req, resp = _stored_accepted_fill(maker, taker)
        [(got_req, got_resp)] = market.accepted_fills_for_addr(maker["addr"])
        assert got_req.request_id == req["request_id"]
        assert got_resp.request_id == resp["request_id"]
        assert got_resp.maker_opens == resp["maker_opens"]


class TestRecentAcceptedFills:
    def test_orders_newest_first(self, maker, taker, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(market.time, "time", lambda: clock[0])
        _stored_accepted_fill(maker, taker, request_id="a" * 16, session_id="a" * 16)
        clock[0] += 10
        _stored_accepted_fill(maker, taker, request_id="b" * 16, session_id="b" * 16)
        pairs = market.recent_accepted_fills(10)
        assert [resp.request_id for _req, resp in pairs] == ["b" * 16, "a" * 16]

    def test_limit_is_respected(self, maker, taker):
        for i in range(3):
            _stored_accepted_fill(maker, taker, request_id=f"r{i}" * 8,
                                  session_id=f"s{i}" * 8)
        assert len(market.recent_accepted_fills(2)) == 2

    def test_declined_responses_are_excluded(self, maker, taker):
        req = signed_fill_request(taker, request_id="r" * 16)
        resp = signed_fill_response(maker, request_id="r" * 16, accepted=False)
        market.store_fill_request(req)
        market.store_fill_response(resp)
        assert market.recent_accepted_fills(10) == []


class TestAcceptedFillWireHelpers:
    def test_request_wire_round_trip(self, maker, taker):
        req, _resp = _stored_accepted_fill(maker, taker)
        row = market.get_fill_request(req["request_id"])
        wire = market.fill_request_to_wire(row)
        assert market.verify_fill_request(wire) is True
        assert wire == req

    def test_response_wire_round_trip(self, maker, taker):
        _req, resp = _stored_accepted_fill(maker, taker)
        row = market.get_fill_response(resp["request_id"])
        wire = market.fill_response_to_wire(row)
        assert market.verify_fill_response(wire) is True
        assert wire == resp

    def test_accepted_fill_to_wire_bundles_both(self, maker, taker):
        req, resp = _stored_accepted_fill(maker, taker)
        [(req_row, resp_row)] = market.accepted_fills_for_addr(maker["addr"])
        bundle = market.accepted_fill_to_wire(req_row, resp_row)
        assert bundle["request"] == req
        assert bundle["response"] == resp
