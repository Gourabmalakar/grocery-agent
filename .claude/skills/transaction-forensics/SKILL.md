---
name: transaction-forensics
description: The method transaction-sentinel follows — reconcile from raw sources never from another agent's summary, check stated reasoning against recorded evidence, escalate on the pattern not only the incident. Use for any post-hoc investigation of system behaviour.
---

# Transaction Forensics

## The rule that matters most

**Reconcile from raw data, never from a summary another agent wrote.** A
`DecisionRecord`'s `reasoning` field is that agent's own account of itself —
useful, but not proof. Cross-check it against `evidence`, against the ledger,
against the audit log's actual state transitions.

## Method

1. **Reconcile.** Ledger totals (`grocery.ledger.store`) vs audit-log
   `order.executed` entries vs (from Phase 4) the merchant's own order
   history. A mismatch in either direction is a finding — money the system
   doesn't know about is exactly as bad as a phantom entry with no money
   behind it.
2. **Verify both chains.** `python -m grocery.audit.verify` — a single
   broken link anywhere is tampering or corruption, not noise.
3. **Find orphans.** An `approved` order that never executed; an `executing`
   order that never resolved; a payment record with no matching signed
   `PaymentMandate` file.
4. **Check reasoning against fact.** Does a "cheapest merchant" claim survive
   comparison against the recorded `alternatives_rejected`? If a rejected
   alternative was actually cheaper, that's not a rounding error — that's the
   agent's account of itself being wrong.
5. **Watch the pattern, not just the incident.** One late-night order is
   noise. A rising rate of late-night orders, or a merchant appearing that
   was never in the intent's allowlist, is a trend worth escalating even
   with no single transaction that looks wrong.

## Rules

- Escalate with the raw numbers, not just a conclusion — whoever reads the
  finding should be able to redo the arithmetic themselves.
- A fatal finding (chain broken, money unaccounted for) trips the kill
  switch immediately; it is never queued for later review.
