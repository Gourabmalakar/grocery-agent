"""The wallet rail refuses before it spends, not after it fails.

Measured on the live account 2026-10-13:
    {'wallet_available': True, 'agentic_eligible': False,
     'cod_available': True, 'amount_due': '213'}

Swiggy Money IS an available method; what is off is Swiggy's own
`agenticPaymentEligible` gate on agent-INITIATED payment. Those are different
facts and conflating them would send Gourab to fund a wallet that is already
fine, or to email support about a wallet he simply has not funded.
"""
from __future__ import annotations

import pytest


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.called = []

    def connect(self):
        return {}

    def call_tool(self, name, args):
        self.called.append(name)
        return {"content": [{"type": "text", "text": __import__("json").dumps(self._payload)}]}


def _client(payload):
    from grocery.carts.swiggy_checkout import SwiggyCheckoutClient
    client = SwiggyCheckoutClient(token_provider=lambda: "t")
    client._client = _FakeClient(payload)
    client._connected = True
    return client


_LIVE = {"swiggyMoney": {"available": True}, "agenticPaymentEligible": False,
         "cod": {"available": True}, "paymentAmount": "213"}


def test_the_live_shape_is_read_correctly(isolated_data_dir):
    report = _client(_LIVE).payment_preflight()
    assert report == {"wallet_available": True, "agentic_eligible": False,
                      "cod_available": True, "amount_due": "213"}


def test_an_ineligible_account_is_refused_without_calling_checkout(isolated_data_dir):
    """The point of the preflight: no order attempt, so no uncertain state."""
    from grocery.carts.swiggy_checkout import WalletNotEligible

    client = _client(_LIVE)
    with pytest.raises(WalletNotEligible) as exc:
        client.assert_can_pay_from_wallet()

    assert "agenticPaymentEligible=false" in str(exc.value)
    assert "checkout" not in client._client.called, "it tried to place an order anyway"


def test_an_unfunded_wallet_gets_different_advice_from_an_ineligible_account(isolated_data_dir):
    from grocery.carts.swiggy_checkout import WalletNotEligible

    unfunded = dict(_LIVE, swiggyMoney={"available": False})
    with pytest.raises(WalletNotEligible) as exc:
        _client(unfunded).assert_can_pay_from_wallet()
    assert "not an available payment method" in str(exc.value)
    assert "builders@swiggy.in" not in str(exc.value)


def test_an_eligible_account_passes(isolated_data_dir):
    eligible = dict(_LIVE, agenticPaymentEligible=True)
    assert _client(eligible).assert_can_pay_from_wallet()["agentic_eligible"] is True


def test_the_rail_reports_the_refusal_rather_than_raising(isolated_data_dir, monkeypatch):
    """execute() must get a PayResult it can act on, not an exception that
    strands the order in EXECUTING."""
    from console.rails.swiggy_wallet import SwiggyWalletRail
    from grocery.carts import swiggy as swiggy_cart

    monkeypatch.setattr(swiggy_cart.SwiggyCartClient, "fetch", lambda self: {
        "billBreakdown": {"lineItems": [{"label": "Item Total", "value": "₹213"}],
                          "toPay": {"label": "To Pay", "value": "₹213"}}})

    from grocery.carts import swiggy_checkout
    monkeypatch.setattr(swiggy_checkout.SwiggyCheckoutClient,
                        "assert_can_pay_from_wallet",
                        lambda self: (_ for _ in ()).throw(
                            swiggy_checkout.WalletNotEligible("not eligible")))

    rail = SwiggyWalletRail(token_provider=lambda: "t", address_id="addr")
    result = rail.pay(payment_mandate={"amount_inr": 213.0, "payment_id": "p"},
                      cart_ref="c", adapter=None)

    assert result.ok is False
    assert result.settled is False
    assert "not eligible" in result.detail


# --- COD: the literal, and what counts as an unknown ------------------------

def test_cod_sends_the_literal_swiggy_actually_accepts(isolated_data_dir):
    """Swiggy's checkout schema says: Use one of: "UPI", "Cash"/"COD",
    "SwiggyPay" — which reads as though either spelling works. It does not:

        Unsupported payment method "COD". Supported methods: Cash, SwiggyPay.

    And get_payment_options reports the method with id "COD", which is NOT the
    checkout value. Two vocabularies for one thing, in one API, with the docs
    using the wrong one."""
    from grocery.carts import swiggy_checkout

    assert swiggy_checkout.COD == "Cash"
    assert set(swiggy_checkout._AUTONOMOUS) == {"SwiggyPay", "Cash"}


def test_a_rejected_parameter_is_a_refusal_not_an_unknown(isolated_data_dir):
    """The server validated the request and declined it before doing anything,
    so no order exists — confirmed against get_orders, which was empty.
    Classifying it as uncertain parked order_0c4c15c9fd71 in
    NEEDS_RECONCILIATION and sent Gourab to check his Swiggy orders, over a
    typo in a constant."""
    from grocery.carts import swiggy_checkout

    assert swiggy_checkout._is_a_clean_refusal(
        'tool checkout reported an error: Unsupported payment method "COD". '
        'Supported methods: Cash, SwiggyPay.')


def test_anything_unrecognised_stays_uncertain(isolated_data_dir):
    """Erring toward "unknown" remains right for everything we do not
    recognise: guessing "it failed" about a payment that succeeded is the
    expensive direction to be wrong in."""
    from grocery.carts import swiggy_checkout

    for message in ("Read timed out", "connection reset by peer",
                    "502 Bad Gateway", "stream closed unexpectedly"):
        assert not swiggy_checkout._is_a_clean_refusal(message), message


def test_upi_is_still_refused_even_though_swiggy_offers_it(isolated_data_dir):
    """A UPI intent needs a human in the payment app, so an agent "paying" by
    UPI is an agent handing over a half-finished order."""
    from grocery.carts.swiggy_checkout import CheckoutRefused, SwiggyCheckoutClient

    client = SwiggyCheckoutClient(token_provider=lambda: "t")
    with pytest.raises(CheckoutRefused):
        client.place_order(address_id="a", payment_method="UPI")
