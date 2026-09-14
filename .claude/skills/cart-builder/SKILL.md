---
name: cart-builder
description: Assembles a signed CartMandate from a set of chosen line items — fees, coupons, landed total, correct intent_ref lineage on every line. Use once per merchant candidate, after price-scout has picked its items.
---

# Cart Builder

## Method

1. Every line item's `intent_ref` points at the intent_id, or — if this cart
   is a response to a revision — the most recent amendment_id. This is the
   field `cart-auditor`'s lineage check reads; get it right or the cart fails
   automatically regardless of price correctness.
2. Fees are explicit and itemised: `delivery`, `handling`, `surge`, `coupon`
   (negative). Never fold them into a single opaque "misc" figure — the
   landed-cost breakdown the Console renders depends on them being separate.
3. Sign with `grocery.mandates.cart.new_cart`, which also computes and
   attaches `cart_hash` — this is the value the human actually approves and
   the value `execute()` re-checks before charging.
4. Persist with `grocery.mandates.cart.persist` — carts are append-only once
   issued; a revision produces a new `cart_id` at an incremented
   `cart_version`, never an edit to the old one.

## Rules

- Every line's `price_source` and `asof` must be set from the moment the
  listing was actually read, not backfilled later.
- `cart_version` starts at 1 and increments by exactly one per revision
  round — never skip a version or reuse one.
