---
name: cart-comparator
description: Ranks candidate carts across merchants by landed cost and surfaces which one wins on items alone vs which wins overall. Use once all viable per-merchant carts for an order have been assembled.
---

# Cart Comparator

## Method

1. Compute a `LandedCostBreakdown` per merchant (`grocery.compare.landed_cost`).
2. Rank with `grocery.compare.rank.rank_by_landed_cost` — merchants below
   their own minimum order are still shown, ranked last, with the shortfall
   visible, rather than silently dropped.
3. State explicitly when the landed-cost winner differs from the
   items-subtotal winner — that inversion is the actual analytical output of
   this skill, not an edge case to gloss over.

## Output

The comparison table the Console renders on the "Compare & revise" screen:
merchant, subtotal, delivery fee, landed total, min-order flag, and which
merchant wins on which measure.

## Rules

- Never present a single number without the breakdown behind it. "Cheapest:
  FreshMart, ₹792" is not itself a comparison — show what it beat and by how
  much.
- Ties are possible and fine; do not manufacture a false tiebreaker.
