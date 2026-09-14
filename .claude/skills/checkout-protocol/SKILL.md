---
name: checkout-protocol
description: The exact propose -> approve cart -> approve payment -> execute sequence, what each step is allowed to do, and what must never be skipped or reordered. Use whenever walking through or explaining an order's approval flow.
---

# Checkout Protocol

## The sequence

```
draft -> proposed -> (revising -> proposed)* -> approved -> executing -> done
                                              -> aborted (from any pre-approved state)
```

Enforced in code by `console/approval.py`'s `_LEGAL_TRANSITIONS` — this
skill describes it, it does not re-implement it, and nothing here overrides
what that state machine actually permits.

## What each step may do

- **Propose** — worker only, via `POST /api/order/{id}/propose_cart`. Cannot
  approve anything.
- **Revise** — human only, via the Console. Amends the intent, invalidates
  any prior state, bounded to `MAX_REVISION_ROUNDS`.
- **Approve cart** — human only. Freezes `cart_hash`.
- **Approve payment** — human only, requires the live-window check. This is
  the step that triggers a fresh merchant re-quote and the budget check,
  *then* mints the `PaymentMandate` — never the reverse order.
- **Execute** — the broker only, immediately after payment approval, using
  the freshly re-quoted cart. Never a separate human-initiated step.
- **Abort** — human only, legal any time before execution has actually paid.

## Rules

- Never suggest, imply, or attempt a path that skips cart approval before
  payment approval, or that reuses an approval across a `cart_version` bump.
- If asked to "just approve and pay in one step", explain that the two
  approvals are deliberately separate — see the plan's rationale — and walk
  through both rather than collapsing them.
