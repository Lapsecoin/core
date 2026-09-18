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
import trade_storage
import xlm as xlm_mod
from trade_storage import Order


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

    def test_one_maker_cannot_fill_the_book(self, maker):
        for _ in range(market.MAX_ORDERS_PER_MAKER):
            market.store_order(signed_order(maker))
        with pytest.raises(market.OrderRejected, match="limit"):
            market.store_order(signed_order(maker))

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
        assert market.remaining_ticks(row) == 10 * LAPSE


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
