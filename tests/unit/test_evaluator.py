def _intent(max_total_inr=800):
    from grocery.mandates import intent as intent_m
    return intent_m.new_intent(items=["milk"], max_total_inr=max_total_inr)


def _cart(intent, *, intent_ref=None, landed_total=100, unsourced=False):
    from grocery.mandates import cart as cart_m
    line = {
        "sku": "x", "qty": 1, "price_inr": landed_total,
        "intent_ref": intent_ref if intent_ref is not None else intent["intent_id"],
    }
    if not unsourced:
        line["price_source"] = "test"
        line["asof"] = 0
    return cart_m.new_cart(
        intent_id=intent["intent_id"], merchant="freshmart", line_items=[line],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0}, landed_total_inr=landed_total,
    )


def test_clean_cart_has_no_findings(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    intent = _intent()
    cart = _cart(intent)
    assert evaluate_cart(cart, intent) == []


def test_lineage_failure(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    intent = _intent()
    cart = _cart(intent, intent_ref="some_unrelated_id")
    findings = evaluate_cart(cart, intent)
    assert any(f.id == "item_without_intent_lineage" for f in findings)


def test_lineage_passes_via_amendment(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    from grocery.mandates import amendment as amendment_m
    intent = _intent()
    amendment = amendment_m.new_amendment(intent=intent, prev_amendment=None,
                                           instruction_text="swap brand", kind="swap_brand", detail={})
    cart = _cart(intent, intent_ref=amendment["amendment_id"])
    findings = evaluate_cart(cart, intent, amendments=[amendment])
    assert findings == []


def test_exceeds_budget_failure(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    intent = _intent(max_total_inr=50)
    cart = _cart(intent, landed_total=100)
    findings = evaluate_cart(cart, intent)
    assert any(f.id == "exceeds_budget" for f in findings)


def test_unsourced_price_failure(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    intent = _intent()
    cart = _cart(intent, unsourced=True)
    findings = evaluate_cart(cart, intent)
    assert any(f.id == "unsourced_price" for f in findings)


def test_cart_hash_mismatch_failure(isolated_data_dir):
    from grocery.evaluator import evaluate_cart
    intent = _intent()
    cart = _cart(intent)
    cart["landed_total_inr"] = 999999  # drift after signing, without recomputing cart_hash
    findings = evaluate_cart(cart, intent)
    assert any(f.id == "cart_hash_mismatch" for f in findings)
