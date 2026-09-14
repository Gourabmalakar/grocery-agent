def test_sign_and_verify_roundtrip(isolated_data_dir):
    from grocery.mandates.signing import sign, verify
    signed = sign({"hello": "world", "n": 1})
    assert verify(signed)


def test_tampering_fails_verification(isolated_data_dir):
    from grocery.mandates.signing import sign, verify
    signed = sign({"amount": 100})
    tampered = dict(signed)
    tampered["amount"] = 100000
    assert not verify(tampered)


def test_intent_and_amendment_chain(isolated_data_dir):
    from grocery.mandates import intent as intent_m, amendment as amend_m
    intent = intent_m.new_intent(items=["milk"], max_total_inr=500)
    a1 = amend_m.new_amendment(intent=intent, prev_amendment=None,
                                instruction_text="make it 2 milks", kind="change_quantity", detail={})
    a2 = amend_m.new_amendment(intent=intent, prev_amendment=a1,
                                instruction_text="drop milk", kind="drop_item", detail={})
    assert a1["prev_hash"]
    assert a2["prev_hash"] != a1["prev_hash"]  # chained to a1, not to the original intent


def test_cart_hash_matches_and_detects_tamper(isolated_data_dir):
    from grocery.mandates import cart as cart_m
    cart = cart_m.new_cart(
        intent_id="intent_x", merchant="freshmart",
        line_items=[{"sku": "a", "qty": 1, "price_inr": 10}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        landed_total_inr=10,
    )
    assert cart_m.hash_matches(cart)
    cart["landed_total_inr"] = 999999
    assert not cart_m.hash_matches(cart)


def test_payment_mandate_refuses_drifted_cart(isolated_data_dir):
    from grocery.mandates import cart as cart_m, payment as payment_m
    import pytest
    cart = cart_m.new_cart(intent_id="i1", merchant="freshmart", line_items=[],
                            fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0}, landed_total_inr=10)
    cart["landed_total_inr"] = 55555  # drift the content without updating cart_hash
    with pytest.raises(ValueError):
        payment_m.new_payment_mandate(cart=cart, amount_inr=10, rail="mock",
                                       approved_by="x", approval_factor="passphrase")
