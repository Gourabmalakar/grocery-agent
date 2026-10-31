"""Building a Swiggy basket, and the three ways it must refuse.

Swiggy's `update_cart` REPLACES the cart — there is no add-one-item call. That
single fact drives the whole design: an existing cart is somebody's hand-made
shopping list, and overwriting it is the only write Swiggy offers, so the
builder has to refuse rather than proceed.
"""
from __future__ import annotations

import pytest

from console import ordering


class FakeSwiggy:
    """Stands in for the live client. Records what it was asked to do."""

    def __init__(self, *, existing=None, built=None):
        self._existing = existing if existing is not None else {"items": [], "totalItems": 0}
        self._built = built
        self.replaced = None
        self.cleared = 0

    def fetch(self):
        return self._built if self.replaced is not None else self._existing

    def replace(self, items, *, address_id):
        self.replaced = (items, address_id)
        return {}

    def clear(self):
        self.cleared += 1

    def addresses(self):
        return [{"id": "addr-1"}]


def _install(monkeypatch, fake):
    monkeypatch.setattr(ordering, "_swiggy_client", lambda: fake)


_APPROVED = {
    "merchant": "swiggy",
    "line_items": [
        {"name": "Amul Taaza 1L", "sku": "SPIN1:SKU1", "qty": 2, "line_total_inr": 150.0},
        {"name": "Onion 1kg", "sku": "SPIN2:SKU2", "qty": 1, "line_total_inr": 59.0},
    ],
}

_GOOD_BILL = {
    "items": [{"spinId": "SPIN1"}, {"spinId": "SPIN2"}],
    "totalItems": 3,
    "billBreakdown": [
        {"type": "subtotal", "amount": 209},
        {"type": "delivery", "amount": 25},
        {"type": "gst", "amount": 11},
        {"type": "total", "amount": 245},
    ],
}


def test_a_cart_with_items_already_in_it_is_never_overwritten(isolated_data_dir, monkeypatch):
    """The whole reason this path is more careful than Zepto's."""
    fake = FakeSwiggy(existing={"items": [{"spinId": "SOMETHING_HIS"}], "totalItems": 1})
    _install(monkeypatch, fake)

    with pytest.raises(ordering.CannotBuild) as exc:
        ordering.build(_APPROVED)

    assert "already has items" in str(exc.value)
    assert fake.replaced is None, "it wrote to the cart anyway"
    assert fake.cleared == 0, "it cleared a cart it was told not to touch"


def test_the_real_bill_including_delivery_and_gst_is_what_comes_back(isolated_data_dir, monkeypatch):
    """Swiggy is the only merchant that can give a true landed total — Zepto's
    cart stage has no delivery and no GST in it at all."""
    _install(monkeypatch, FakeSwiggy(built=_GOOD_BILL))

    built = ordering.build(_APPROVED)

    assert built.totals.total_inr == 245.0
    assert built.totals.subtotal_inr == 209.0
    assert built.totals.fees_inr == 25.0
    assert built.totals.taxes_inr == 11.0
    assert built.to_json()["fees_known"] is True


def test_the_approved_lines_are_carted_by_id_not_by_name(isolated_data_dir, monkeypatch):
    fake = FakeSwiggy(built=_GOOD_BILL)
    _install(monkeypatch, fake)

    ordering.build(_APPROVED)

    items, address_id = fake.replaced
    assert items == [("SPIN1", "SKU1", 2), ("SPIN2", "SKU2", 1)]
    assert address_id == "addr-1"


def test_a_cart_whose_total_cannot_be_read_is_cleared_and_refused(isolated_data_dir, monkeypatch):
    """A basket whose cost is unknown is not something to hand a human for
    approval, and leaving it in his account would be worse than refusing."""
    fake = FakeSwiggy(built={"items": [{"spinId": "SPIN1"}], "totalItems": 3})
    _install(monkeypatch, fake)

    with pytest.raises(ordering.CannotBuild) as exc:
        ordering.build(_APPROVED)

    assert "did not report a total" in str(exc.value)
    assert fake.cleared == 1, "it left an unpriced cart sitting in his account"


def test_a_cart_that_costs_more_than_was_approved_is_cleared_and_refused(isolated_data_dir, monkeypatch):
    """The price moved between the proposal and the write. He said yes to a
    number, so a different number is a different question."""
    drifted = dict(_GOOD_BILL, billBreakdown=[
        {"type": "subtotal", "amount": 280},
        {"type": "total", "amount": 316},
    ])
    fake = FakeSwiggy(built=drifted)
    _install(monkeypatch, fake)

    with pytest.raises(ordering.CannotBuild) as exc:
        ordering.build(_APPROVED)

    # Both numbers, so the refusal is readable: what it found vs what he agreed.
    message = str(exc.value)
    assert "280" in message and "209" in message and "did not agree" in message
    assert fake.cleared == 1


def test_a_line_with_no_swiggy_id_refuses_before_anything_is_written(isolated_data_dir, monkeypatch):
    fake = FakeSwiggy(built=_GOOD_BILL)
    _install(monkeypatch, fake)

    with pytest.raises(ordering.CannotBuild) as exc:
        ordering.build({"merchant": "swiggy", "line_items": [
            {"name": "Amul Taaza 1L", "sku": "SPIN1:SKU1", "qty": 1, "line_total_inr": 75.0},
            {"name": "Mystery item", "sku": "", "qty": 1, "line_total_inr": 10.0},
        ]})

    assert "Mystery item" in str(exc.value)
    assert fake.replaced is None, "it half-built the basket"


def test_cancelling_a_swiggy_cart_clears_it(isolated_data_dir, monkeypatch):
    """Swiggy's cart has no id to cancel — it is bound to the account — so the
    only way to take it back is to empty it."""
    fake = FakeSwiggy()
    _install(monkeypatch, fake)

    ordering.cancel("swiggy", "swiggy:account-cart")

    assert fake.cleared == 1


def test_a_closed_store_says_so_instead_of_leaking_an_mcp_error(isolated_data_dir, monkeypatch):
    """Measured 2026-10-12 at 01:00 IST: search_products returned 11 priced
    products while get_cart answered "The store is unavailable at the moment".
    A basket can therefore be priced at an hour when it cannot be built."""
    class Closed(FakeSwiggy):
        def fetch(self):
            raise RuntimeError("tool get_cart reported an error: The store is "
                               "unavailable at the moment. Please try again after some time.")

    fake = Closed()
    _install(monkeypatch, fake)

    with pytest.raises(ordering.CannotBuild) as exc:
        ordering.build(_APPROVED)

    message = str(exc.value)
    assert "closed right now" in message
    assert "search still works" in message
    assert "get_cart" not in message, "leaked the raw tool name at the user"
    assert fake.replaced is None
