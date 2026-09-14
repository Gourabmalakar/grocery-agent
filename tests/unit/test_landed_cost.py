from grocery.adapters.base import Quote
from grocery.compare.landed_cost import compute, suggest_min_order_topup, compute_from_subtotal
from grocery.compare.rank import rank_by_landed_cost


def test_inversion_item_subtotal_vs_landed_cost():
    """The actual analytical point: cheaper items can lose on landed cost."""
    cheap_items_high_delivery = compute_from_subtotal(
        "quickbasket", subtotal_inr=100,
        meta={"delivery_fee_inr": 60, "min_order_inr": 0, "free_delivery_over_inr": 9999, "handling_fee_inr": 8},
    )
    pricier_items_free_delivery = compute_from_subtotal(
        "freshmart", subtotal_inr=120,
        meta={"delivery_fee_inr": 25, "min_order_inr": 0, "free_delivery_over_inr": 100, "handling_fee_inr": 0},
    )
    assert cheap_items_high_delivery.items_subtotal_inr < pricier_items_free_delivery.items_subtotal_inr
    assert cheap_items_high_delivery.landed_total_inr > pricier_items_free_delivery.landed_total_inr

    ranked = rank_by_landed_cost([cheap_items_high_delivery, pricier_items_free_delivery])
    assert ranked[0].breakdown.merchant == "freshmart"
    assert ranked[0].cheapest_by_landed_cost
    assert not ranked[0].cheapest_by_items_only  # freshmart did NOT win on items alone


def test_min_order_topup_suggestion():
    q = Quote(merchant="x", subtotal_inr=400, delivery_fee_inr=40, handling_fee_inr=0,
              coupon_discount_inr=0, landed_total_inr=440)
    msg = suggest_min_order_topup(q, next_free_delivery_threshold_inr=500)
    assert msg is not None
    assert "100" in msg and "40" in msg


def test_below_min_order_ranked_last_not_dropped():
    ok = compute_from_subtotal("a", 300, {"delivery_fee_inr": 0, "min_order_inr": 0, "free_delivery_over_inr": 0, "handling_fee_inr": 0})
    below = compute_from_subtotal("b", 50, {"delivery_fee_inr": 0, "min_order_inr": 199, "free_delivery_over_inr": 0, "handling_fee_inr": 0})
    ranked = rank_by_landed_cost([ok, below])
    assert len(ranked) == 2  # shown, not dropped — just ranked after every eligible merchant
    assert ranked[0].breakdown.merchant == "a"
    assert ranked[1].breakdown.merchant == "b"
    assert ranked[1].breakdown.below_min_order
