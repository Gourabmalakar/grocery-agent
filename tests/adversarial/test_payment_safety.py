"""The five holes that had to close before a rupee could move, each asserted.

Every one of these was live in the code that shipped the cart builder. They are
kept in one file because they are one argument: a payment system is only as good
as what it does when something goes wrong halfway.
"""
from __future__ import annotations

import time

import pytest

# NOTE: grocery.* is imported INSIDE each test, never at module level.
# isolated_data_dir re-points DATA_DIR and purges grocery/console/worker/bot
# from sys.modules — a module-level `from grocery.ledger import store` binds
# the object built from the REAL config before any of that happens, so these
# tests would read and write Gourab's actual ledger. Caught exactly that way.


# --- 1. the replay guard, which previously could never fire -----------------

def test_the_same_basket_cannot_be_paid_for_twice(isolated_data_dir):
    """It used to check a payment_id minted four lines earlier in the same
    function, so the answer was always "not spent" and the guard was
    decorative. The key is now derived from the order and the exact basket."""
    from grocery.ledger import store as ledger_store
    key = ledger_store.intent_key("order_x", "cart_hash_abc")

    first, _ = ledger_store.claim_intent(
        key=key, order_id="order_x", cart_hash="cart_hash_abc",
        amount_inr=451.0, rail="swiggy_wallet", payment_id="pay_1")
    second, existing = ledger_store.claim_intent(
        key=key, order_id="order_x", cart_hash="cart_hash_abc",
        amount_inr=451.0, rail="swiggy_wallet", payment_id="pay_2")

    assert first is True
    assert second is False, "the same basket was claimed twice"
    assert existing["payment_id"] == "pay_1"


def test_a_different_basket_for_the_same_order_is_a_different_claim(isolated_data_dir):
    """Scoping to the order alone would block a legitimate re-approval after a
    revision; scoping to order+basket does not."""
    from grocery.ledger import store as ledger_store
    a = ledger_store.intent_key("order_x", "hash_one")
    b = ledger_store.intent_key("order_x", "hash_two")
    assert a != b
    assert ledger_store.claim_intent(key=a, order_id="order_x", cart_hash="hash_one",
                                     amount_inr=1.0, rail="r", payment_id="p1")[0]
    assert ledger_store.claim_intent(key=b, order_id="order_x", cart_hash="hash_two",
                                     amount_inr=1.0, rail="r", payment_id="p2")[0]


# --- 2. crash safety --------------------------------------------------------

def test_a_crash_during_payment_leaves_a_trace_to_reconcile(isolated_data_dir):
    """The intent is written BEFORE the rail is called, so an interruption is
    distinguishable from an order that was never placed. Previously the only
    write happened after rail.pay() returned, so a crash left money possibly
    moved and nothing written down."""
    from grocery.ledger import store as ledger_store
    key = ledger_store.intent_key("order_crash", "hash")
    ledger_store.claim_intent(key=key, order_id="order_crash", cart_hash="hash",
                              amount_inr=212.0, rail="swiggy_wallet", payment_id="p")
    # ... process dies here, between claim and settle ...

    # Scoped to this order: the ledger is shared across tests in this module,
    # and asserting global emptiness would make these tests depend on each
    # other's order.
    mine = [p for p in ledger_store.unsettled() if p["order_id"] == "order_crash"]
    assert len(mine) == 1
    assert mine[0]["amount_inr"] == 212.0


def test_a_settled_payment_is_not_reported_as_needing_reconciliation(isolated_data_dir):
    from grocery.ledger import store as ledger_store
    key = ledger_store.intent_key("order_ok", "hash")
    ledger_store.claim_intent(key=key, order_id="order_ok", cart_hash="hash",
                              amount_inr=50.0, rail="r", payment_id="p")
    ledger_store.settle_intent(key, merchant_order_id="SW123")
    assert not [p for p in ledger_store.unsettled() if p["order_id"] == "order_ok"]
    assert ledger_store.intent_for(key)["merchant_order_id"] == "SW123"


def test_a_handed_off_basket_is_neither_settled_nor_a_failure(isolated_data_dir):
    """Nothing was charged and nothing went wrong. Recording it as either would
    be a lie, and recording it as settled would consume the caps."""
    from grocery.ledger import store as ledger_store
    key = ledger_store.intent_key("order_h", "hash")
    ledger_store.claim_intent(key=key, order_id="order_h", cart_hash="hash",
                              amount_inr=451.0, rail="in_app_handoff", payment_id="p")
    ledger_store.hand_off_intent(key, detail="waiting in the app")

    assert ledger_store.intent_for(key)["state"] == "handed_off"
    assert not [p for p in ledger_store.unsettled() if p["order_id"] == "order_h"]
    # The point: a hand-off writes NO spend row, so the caps are untouched.
    assert ledger_store.spend_today() == 0.0, "a hand-off consumed the daily cap"


# --- 3 & 4. the two rails that failed wrongly -------------------------------

def test_the_upi_link_rail_reports_failure_rather_than_a_phantom_payment(isolated_data_dir):
    """It returned ok=True having paid nothing, so execute() marked the order
    DONE and recorded a spend that had not happened."""
    from grocery.ledger import store as ledger_store
    from console.rails.upi_link import UpiHandoffRail

    result = UpiHandoffRail(lambda m: "vpa@bank").pay(
        payment_mandate={"amount_inr": 100.0, "payment_id": "p"},
        cart_ref="c", adapter=None)
    assert result.ok is False
    assert result.settled is False


def test_the_generic_wallet_rail_fails_closed_instead_of_raising(isolated_data_dir):
    """It raised NotImplementedError from inside execute(), AFTER the order had
    moved to EXECUTING — stranding it in a state with no recovery, looking
    exactly like an order that might have been paid."""
    from grocery.ledger import store as ledger_store
    from console.rails.wallet_rail import WalletRail

    result = WalletRail().pay(payment_mandate={"amount_inr": 1.0, "payment_id": "p"},
                              cart_ref="c", adapter=None)
    assert result.ok is False and result.settled is False


# --- 5. caps on IST ---------------------------------------------------------

def test_the_daily_cap_rolls_at_midnight_ist_not_utc(isolated_data_dir):
    """UTC midnight is 05:30 IST, so an order at 02:00 IST counted against the
    PREVIOUS day — letting the small hours spend a day's budget twice."""
    from grocery.ledger import store as ledger_store
    import datetime

    ist = ledger_store.IST
    # 02:00 IST on the 12th. In UTC this is 20:30 on the 11th.
    two_am_ist = datetime.datetime(2026, 10, 12, 2, 0, tzinfo=ist).timestamp()
    assert ledger_store._ist(two_am_ist).strftime("%Y-%m-%d") == "2026-10-12"
    assert datetime.datetime.fromtimestamp(
        two_am_ist, tz=datetime.timezone.utc).strftime("%Y-%m-%d") == "2026-10-11"

    ledger_store.record_spend(payment_id="p1", cart_id="c", amount_inr=300.0, rail="r")
    assert ledger_store.spend_today() == 300.0


# --- the signature, which had no production call site ----------------------

def test_a_tampered_cart_mandate_does_not_verify(isolated_data_dir):
    """signing.verify() was never called before spending. Worse, it could not
    have been: sign() hashes the payload BEFORE cart_hash is added and verify()
    included it, so it returned False for every genuine cart."""
    from grocery.ledger import store as ledger_store
    from grocery.mandates import cart as cart_mandate

    cart = cart_mandate.new_cart(
        intent_id="i", merchant="swiggy",
        line_items=[{"sku": "S:K", "name": "Butter", "qty": 1, "unit_price_inr": 60.0}],
        fees={"delivery": 3.0}, landed_total_inr=63.0)

    assert cart_mandate.signature_valid(cart) is True, (
        "a genuine mandate must verify, or wiring the check in would refuse "
        "every payment")

    tampered = dict(cart, landed_total_inr=6300.0)
    assert cart_mandate.signature_valid(tampered) is False


def test_autonomous_payment_needs_both_switches(isolated_data_dir, monkeypatch):
    """MODE is described in config.py as the most safety-critical value in the
    project, and IS_LIVE was computed and then checked nowhere in the payment
    path. A box left at PAYMENT_BACKEND=wallet would have spent real money
    while every status page it serves reported mode: dry_run."""
    from grocery import config

    monkeypatch.setattr(config, "PAYMENT_BACKEND", "wallet")
    monkeypatch.setattr(config, "IS_LIVE", False)
    assert config.autonomous_payment_enabled() is False, "dry_run could spend"
    # The hand-off charges nothing, so it stays available in dry_run.
    assert config.payments_enabled() is True

    monkeypatch.setattr(config, "IS_LIVE", True)
    assert config.autonomous_payment_enabled() is True


def test_the_wallet_button_is_not_offered_in_dry_run(isolated_data_dir, monkeypatch):
    """The surfaces agree: what config forbids, the chat must not show."""
    from grocery import config
    from console.rails import select

    monkeypatch.setattr(config, "PAYMENT_BACKEND", "wallet")
    monkeypatch.setattr(config, "IS_LIVE", False)
    assert select.available() == ["app"]

    # COD now sits between them, and it needs MODE=live too: placing a COD
    # order commits a real purchase even though no instrument is charged.
    monkeypatch.setattr(config, "IS_LIVE", True)
    assert select.available() == ["app", "cod", "wallet"]
