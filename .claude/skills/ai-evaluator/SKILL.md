---
name: ai-evaluator
description: Score a proposed cart or order against objective, versioned criteria and return pass/fail with reasons, following the family office's evals/criteria.yaml pattern. Use on every cart before it reaches the human, and whenever reviewing whether the system behaved correctly.
---

# AI Evaluator

## Method

1. Run the mechanical checks first — `grocery.evaluator.evaluate_cart()` —
   and treat any finding it returns as an automatic fail, full stop. Do not
   average an automatic failure against otherwise-good reasoning.
2. Only after the mechanical pass is clean, apply judgement: does the
   comparison actually reflect landed cost? Does the stated reasoning match
   the recorded evidence? Is a substitution properly flagged rather than
   silently absorbed?
3. Write the result as a JSON verdict — reviewed item, date, findings,
   verdict — mirroring the family office's `evals/results/*.json` shape, so
   `evals/results/` accumulates a real, auditable track record rather than
   living only in a chat transcript.

## Automatic failures (never scored, always block)

`item_without_intent_lineage`, `exceeds_budget`, `unsourced_price`,
`cart_hash_mismatch`, `payment_without_approval`, `approval_survived_
revision`, `revision_raised_cap` — see `evals/criteria.yaml`.

## Rules

- **Independent recomputation is the highest-value act.** Re-derive the
  landed total from raw line items and merchant metadata; do not trust the
  cart's own total field.
- Disclose if this same session produced the thing being evaluated.
- Never silently drop a failing evaluation. If a cart fails twice, it ships
  flagged `FAIL — reasons`, never quietly re-tried until it happens to pass.
