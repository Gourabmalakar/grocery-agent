def test_per_order_cap(isolated_data_dir):
    from grocery.ledger import budget
    check = budget.check(801)
    assert not check.allowed
    assert "per-order" in check.reason


def test_within_caps_ok(isolated_data_dir):
    from grocery.ledger import budget
    check = budget.check(500)
    assert check.allowed


def test_daily_cap_accumulates(isolated_data_dir):
    from grocery.ledger import budget, store
    store.record_spend(payment_id="p1", cart_id="c1", amount_inr=700, rail="mock")
    store.record_spend(payment_id="p2", cart_id="c2", amount_inr=700, rail="mock")
    check = budget.check(200)  # 700+700+200 = 1600 > daily cap 1500
    assert not check.allowed
    assert "remaining" in check.reason


def test_replay_guard(isolated_data_dir):
    from grocery.ledger import store
    store.record_spend(payment_id="dup", cart_id="c1", amount_inr=100, rail="mock")
    assert store.already_spent("dup")
    assert not store.already_spent("never-spent")
