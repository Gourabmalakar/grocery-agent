"""Fees he was shown are not a price drift.

order_2c4eef60badd, 2026-10-31. Approved basket ₹122.00 of items. The built
Swiggy cart came back at ₹125.00 — ₹122.00 of items plus ₹3.01 handling — and
the chat said so twice, in the basket message ("Items ₹122.00 · fees ₹3.01 =
₹125.00") and on the button he pressed ("Order now, pay ₹125 cash"). He typed
his passphrase. The payment was then refused:

    price drifted since cart approval: was ₹122.00, now ₹125.00
    — refusing to charge a total you did not see

He had seen it. The drift check was comparing the COMPARISON-stage total, which
is item prices only because fees are unreadable before a cart exists, against a
fresh quote that includes fees. The two differ by exactly the fee, so the check
fired on every basket carrying one — meaning payment never worked on Swiggy at
all, for anybody, ever.
"""
from __future__ import annotations

import pytest


class _Order:
    """Just enough of an Order for the amount helper."""
    def __init__(self, cart, merchant_cart=None):
        self.cart = cart
        self.merchant_cart = merchant_cart


def test_the_approved_amount_is_the_total_he_was_shown(isolated_data_dir):
    from console import approval

    order = _Order(
        cart={"landed_total_inr": 122.0},          # comparison stage, items only
        merchant_cart={"subtotal_inr": 122.0, "fees_inr": 3.01,
                       "total_inr": 125.0, "fees_known": True},
    )
    assert approval._amount_the_human_approved(order) == 125.0, (
        "the drift check would compare against the pre-fee total and refuse "
        "every basket that carries a fee")


def test_without_a_built_cart_it_falls_back_to_the_quoted_total(isolated_data_dir):
    """The mock path builds no merchant cart, and must keep working."""
    from console import approval

    order = _Order(cart={"landed_total_inr": 310.0}, merchant_cart=None)
    assert approval._amount_the_human_approved(order) == 310.0


def test_an_empty_merchant_cart_does_not_zero_the_approval(isolated_data_dir):
    """A build that recorded nothing useful must not make the approved amount
    ₹0 — that would pass every cap and every drift check."""
    from console import approval

    order = _Order(cart={"landed_total_inr": 310.0}, merchant_cart={"total_inr": 0})
    assert approval._amount_the_human_approved(order) == 310.0


def test_a_real_drift_is_still_caught(isolated_data_dir):
    """The check keeps its purpose: the price moving between the basket being
    shown and the money leaving. Only the baseline changed."""
    from console import approval

    order = _Order(
        cart={"landed_total_inr": 122.0},
        merchant_cart={"total_inr": 125.0, "fees_known": True},
    )
    approved = approval._amount_the_human_approved(order)
    # Swiggy surge-prices the basket to ₹180 after he approved ₹125.
    assert abs(180.0 - approved) > 0.005, "a genuine drift must still exceed tolerance"
    # And the fee case that broke him must NOT.
    assert abs(125.0 - approved) <= 0.005
