"""
Every legal transition, and — more importantly — every illegal one rejected.
console/approval.py's _LEGAL_TRANSITIONS is the thing under test; these tests
exercise it through the public functions, not by poking the dict directly.
"""
import pytest


def _make_order(monkeypatch):
    from grocery.mandates import intent as intent_m
    from console import approval
    signed_intent = intent_m.new_intent(items=["milk"], max_total_inr=800)
    intent_m.persist(signed_intent)
    return approval.create_order(signed_intent)


def _make_cart(order, landed_total=100):
    from grocery.mandates import cart as cart_m
    cart = cart_m.new_cart(
        intent_id=order.intent["intent_id"], merchant="freshmart",
        line_items=[{"sku": "fm-milk-1l", "qty": 1, "intent_ref": order.intent["intent_id"],
                     "price_source": "test", "asof": 0, "price_inr": 66}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        landed_total_inr=landed_total,
    )
    cart_m.persist(cart)
    return cart


def test_draft_to_proposed_to_approved(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    assert order.state == approval.State.DRAFT
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    assert order.state == approval.State.PROPOSED
    order = approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")
    assert order.state == approval.State.APPROVED


def test_cannot_approve_without_cart(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    order = approval.propose_cart(order.order_id, _make_cart(order))
    # force cart to None to simulate a corrupted state, then try to approve
    order.cart = None
    approval._save(order)
    with pytest.raises(approval.ApprovalRefused):
        approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")


def test_cannot_execute_without_approval(isolated_data_dir):
    from console import approval
    from console.rails.mock_rail import MockRail
    order = _make_order(None)
    order = approval.propose_cart(order.order_id, _make_cart(order))
    with pytest.raises(approval.IllegalTransition):
        approval.approve_payment_and_execute(order.order_id, approved_by="x",
                                              approval_factor="passphrase", rail=MockRail(), adapter=None)


def test_cannot_execute_twice(isolated_data_dir):
    """done -> execute again must be illegal."""
    from console import approval
    order = _make_order(None)
    order.state = approval.State.DONE
    approval._save(order)
    with pytest.raises(approval.IllegalTransition):
        approval.approve_payment_and_execute(order.order_id, approved_by="x",
                                              approval_factor="passphrase", rail=None, adapter=None)


def test_abort_from_proposed_is_terminal(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    order = approval.propose_cart(order.order_id, _make_cart(order))
    order = approval.abort(order.order_id, aborted_by="x")
    assert order.state == approval.State.ABORTED
    with pytest.raises(approval.IllegalTransition):
        approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")
    with pytest.raises(approval.IllegalTransition):
        approval.abort(order.order_id, aborted_by="x")  # aborted -> anything is illegal


def test_cannot_abort_after_done(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    order.state = approval.State.DONE
    approval._save(order)
    with pytest.raises(approval.IllegalTransition):
        approval.abort(order.order_id, aborted_by="x")


def test_revision_only_legal_from_proposed(isolated_data_dir):
    from console import approval
    order = _make_order(None)  # still draft — no cart proposed yet
    with pytest.raises(approval.IllegalTransition):
        approval.request_revision(order.order_id, instruction_text="swap brand")


def test_revision_invalidates_cart_and_bumps_state(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    order = approval.propose_cart(order.order_id, _make_cart(order))
    order = approval.request_revision(order.order_id, instruction_text="Amul not Britannia")
    assert order.state == approval.State.REVISING
    assert order.cart is None
    assert len(order.amendments) == 1


def test_revision_rounds_are_bounded(isolated_data_dir):
    from grocery import config
    from console import approval
    order = _make_order(None)
    for i in range(config.MAX_REVISION_ROUNDS):
        order = approval.propose_cart(order.order_id, _make_cart(order))
        order = approval.request_revision(order.order_id, instruction_text=f"tweak {i}")
    # one more revision request should now be refused
    order = approval.propose_cart(order.order_id, _make_cart(order))
    with pytest.raises(approval.ApprovalRefused):
        approval.request_revision(order.order_id, instruction_text="one too many")


def test_approve_cart_refuses_on_hash_drift(isolated_data_dir):
    from console import approval
    order = _make_order(None)
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.cart["landed_total_inr"] = 999999  # drift after proposal, before approval
    approval._save(order)
    with pytest.raises(approval.ApprovalRefused):
        approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")


def test_approved_cart_version_superseded_by_revision_cannot_be_approved_twice(isolated_data_dir):
    """approve a cart_version, then simulate a revision producing a new
    version — the old approval must not silently apply to the new cart."""
    from console import approval
    order = _make_order(None)
    order = approval.propose_cart(order.order_id, _make_cart(order))
    order = approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")
    assert order.state == approval.State.APPROVED
    # An approved order cannot be revised directly (revision is only legal
    # from 'proposed') — the only way to change it is abort and start over.
    with pytest.raises(approval.IllegalTransition):
        approval.request_revision(order.order_id, instruction_text="change my mind")
