---
name: landed-cost
description: Computes what a cart actually costs once delivery, handling, surge and minimum-order thresholds are accounted for — the number that decides the cheapest merchant, which is routinely not the one with the cheapest item subtotal. Use whenever ranking merchants or advising on a min-order top-up.
---

# Landed Cost

## Why this exists

Item price alone is marketing. `mockstore/catalog.py`'s QuickBasket fixture
is deliberately the cheapest per-item merchant with the worst free-delivery
threshold — proving the comparison is real, not headline-price theatre, is
the actual point of this skill (see `tests/e2e` scenario 2).

## Method

`landed_total = items_subtotal + delivery_fee + handling_fee + surge - coupon`

- `delivery_fee` is zero once `items_subtotal >= free_delivery_over`,
  otherwise the merchant's flat fee.
- A cart below `min_order_inr` isn't just expensive, it's **not fulfillable**
  — flag `below_min_order` and compute the exact shortfall so advice like
  "add ₹120 to avoid a ₹40 delivery fee" is arithmetically grounded, not a
  generic nudge (see `grocery.compare.landed_cost.suggest_min_order_topup`).

## Rules

- Recompute from raw catalogue data every time; never trust a cached total
  carried over from an earlier round — prices and thresholds can change
  between searches, and a stale total is a wrong total.
- Rank on `landed_total_inr`, never on `items_subtotal_inr` alone.
