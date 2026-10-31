"""Swiggy's real bill, pinned against a payload measured from the live account.

This is the verbatim `billBreakdown` from Gourab's cart on 2026-10-12,
cross-checked against a screenshot of the same cart in the Swiggy app: both
said ₹451. It is kept whole, rather than reduced to the fields under test,
because the first mapping here was GUESSED and every guess was wrong — the
total sits under `toPay`, the container is a mapping holding a list rather than
a list of typed rows, and the fee labels are prose. A parser written against
imagination refuses every build; a parser written against this does not.
"""
from __future__ import annotations

from grocery.carts.swiggy import totals_of

_MEASURED = {
    "selectedAddress": "daskj7k1d96s2jh6dgh0",
    "items": [
        {"spinId": "KZWLL4HEY0", "skuId": "BSOIOKC8YQ", "quantity": 1,
         "itemName": "Bajaj Astor Power Strip (1440W, 4 Universal 3-Pin Sockets, Black)",
         "storeId": 1373485, "mrp": 499, "discountedFinalPrice": 299},
        {"spinId": "MOG9UWSMD7", "skuId": "SF6CJ733MP", "quantity": 1,
         "itemName": "Portronics Konnect Sync 1.5M Cord Length HDMI Cable - Black",
         "storeId": 1373485, "mrp": 499, "discountedFinalPrice": 149},
    ],
    "billBreakdown": {
        "lineItems": [
            {"label": "Item Total", "value": "₹448.00"},
            {"label": "Handling Fee", "value": "₹3.01"},
            {"label": "Delivery Partner Tip", "value": "Add a tip"},
            {"label": "Delivery Partner Fee", "value": "FREE"},
        ],
        "toPay": {"label": "To Pay", "value": "₹451"},
    },
    "superData": {"userStatus": "NOT_SUPER"},
    "cartId": "11cece9a-48b2-46a3-9644-fb10b46b12e4",
}


def test_the_total_matches_what_the_app_showed():
    """₹451 on the phone, ₹451 here. This is the number anything would be paid
    against, so it is the one that has to be right."""
    assert totals_of(_MEASURED).total_inr == 451.0


def test_the_components_are_read_as_well_so_they_can_check_the_total():
    totals = totals_of(_MEASURED)
    assert totals.subtotal_inr == 448.0
    assert totals.fees_inr == 3.01          # handling ₹3.01 + delivery FREE
    assert totals.found is True


def test_free_is_an_amount_and_zero():
    """"Delivery Partner Fee: FREE" is a real row worth recording as 0.00 —
    dropping it would make a free delivery indistinguishable from an unread one."""
    rows = dict(totals_of(_MEASURED).raw_rows)
    assert rows["deliverypartnerfee"] == 0.0


def test_a_tip_invitation_never_becomes_a_charge():
    """"Delivery Partner Tip: Add a tip" is an invitation, not money. Reading it
    as a charge would inflate the bill with a number nobody agreed to."""
    rows = dict(totals_of(_MEASURED).raw_rows)
    assert "deliverypartnertip" not in rows
    assert totals_of(_MEASURED).fees_inr == 3.01


def test_the_components_reconcile_against_the_merchants_own_total():
    """Our arithmetic is a CHECK on Swiggy's number, never a substitute for it."""
    totals = totals_of(_MEASURED)
    rebuilt = totals.subtotal_inr + totals.fees_inr + totals.taxes_inr
    assert abs(rebuilt - totals.total_inr) <= 0.01, (
        f"components {rebuilt} do not reconcile with the stated total "
        f"{totals.total_inr} — a fee row is being missed")


def test_an_unreadable_bill_is_reported_as_unknown_not_as_zero():
    """The refusal path. A shape we cannot read must not look like a free cart."""
    totals = totals_of({"items": [{"spinId": "X"}], "billBreakdown": {"mystery": {}}})
    assert totals.found is False
    assert totals.total_inr == 0.0
