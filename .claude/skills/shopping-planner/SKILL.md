---
name: shopping-planner
description: Turns a signed Intent into a shopping plan — which merchants to check, how to interpret ambiguous item names, when a request has no viable answer under its cap. Use at the start of every order and after every revision.
---

# Shopping Planner

The method the `shopper` agent follows. Planning, not execution — this skill
never touches an adapter's write methods or the Console's approval routes.

## When invoked

- A new order enters `draft` state.
- An order re-enters `proposed` after a `revising` round, with a new
  amendment to account for.

## Method

1. **Read the intent literally.** "Milk, eggs, atta 5kg, biscuits" is four
   line items, one of which specifies a size. Do not infer additional items
   or drop ambiguous ones — if "biscuits" matches two brands in the
   catalogue, that ambiguity is resolved by price (see `cart-comparator`),
   not by picking arbitrarily and hoping it's fine.
2. **Fan out to `price-scout`, one call per merchant, in parallel.** Each
   scout only ever sees its own merchant's catalogue.
3. **Hand every viable candidate to `cart-comparator`.** Landed cost, not
   item subtotal, decides the winner — see `landed-cost`.
4. **If nothing clears the intent's cap**, stop and say so. Report the
   cheapest landed total found and by how much it misses, rather than
   silently substituting cheaper items the human didn't ask for.
5. **On a revision**, re-run only the affected part of the plan. A brand
   swap on one item should not re-shop the other three.

## Output

A `CartMandate` candidate per viable merchant, plus a comparison table
(merchant, subtotal, delivery fee, landed total, below-min-order flag) —
the exact shape `cart-comparator`'s ranking already produces.

## Rules

- Never call an adapter's `build_cart` or `place_order`. Planning stays
  read-only end to end; carting-for-real only happens inside the broker at
  payment time (see `console/approval.py`'s re-quote step).
- Every pick, skip and rejection gets a `DecisionRecord`. No exceptions for
  "obvious" choices — the trace is the actual deliverable, not a side effect.
