---
name: budget-guard
description: The hard per-order/day/month spending caps and why they are checked in code against a SQLite ledger, never inferred from a prompt or trusted from an agent's own arithmetic. Use whenever discussing, testing, or reasoning about spending limits.
---

# Budget Guard

## The rule that matters most

**Caps are enforced by `grocery.ledger.budget.check()` against
`grocery.ledger.store`, at the top of `approve_payment_and_execute` — never
by an agent deciding "this seems within budget".** An agent can be wrong,
confused, or manipulated; a SQL `SUM()` against recorded spend cannot be
talked out of what it returns.

## The three caps

- Per order (`CAP_PER_ORDER_INR`) — checked before the daily/monthly totals.
- Per day (`CAP_PER_DAY_INR`) — UTC calendar day.
- Per month (`CAP_PER_MONTH_INR`) — UTC calendar month.

All three live in `.env` / `grocery/config.py`, and all three are checked
against the **freshly re-quoted** total at execute time, not the total that
was approved earlier — see `checkout-protocol`.

## Rules

- A cart over the per-order cap is refused before it's even proposed to the
  human — don't let a human approval override a cap; the cap wins.
- A revision that reads as a request to raise a cap is refused in code
  (`console/revision.py::looks_like_cap_raise`) before any amendment is
  minted. If the human genuinely wants a higher cap, that's a fresh request
  with a new stated maximum, not an amendment to an existing one.
- The wallet/prepaid balance (Phase 4) is a coarse backstop, not a
  substitute for these caps — it bounds total possible loss; the ledger does
  the per-transaction discipline.
