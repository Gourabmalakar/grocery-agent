# Grocery Agent — current state

*rolling summary; rewritten each session, never appended*

**Last updated:** 2026-09-14

## Where the build stands

Phase 0 skeleton built: mandates + signing, hash-chained audit + decision
logs, budget ledger, kill switch (PAUSE/PANIC/BURN), mock storefront (3
merchants), adapter contract + mock adapter, compare/normalise/landed_cost/
rank, Console (FastAPI + passphrase auth, 5 screens), worker (separate
process, deterministic Phase-0 planner), deterministic evaluator checks,
hooks, settings.json deny list, CLAUDE.md, agents, skills.

**MODE=dry_run by default. Nothing in this repo can touch real money or a
real merchant yet** — Phase 2 (real-site read-only adapters), Phase 3
(UPI handoff), Phase 4 (capped wallet rail, WebAuthn, sentinel) are not built.

## Facts established — do not re-derive

- The worker is a genuinely separate OS process from the Console — it talks
  to it only over `worker/tools.py`'s HTTP calls, never imports
  `console.approval` directly.
- Feedback on a proposed cart amends the *intent* (a signed `IntentAmendment`
  chained to it), never edits the cart in place — this is what keeps
  `item_without_intent_lineage` checkable after a revision.
- A revision that reads as a cap-raise attempt is refused in code
  (`console/revision.py`'s `looks_like_cap_raise`) before any amendment is
  minted — never left to the model to police itself.
- Payment execution rebuilds the merchant's cart and re-quotes it at the
  last possible moment before charging — the actual price-drift defence,
  not just a hash comparison against a stale number.
- Swiggy Builders Club (Instamart MCP, OAuth 2.1+PKCE) is the intended
  durable adapter path; application to `builders@swiggy.in` is a Phase-0
  parallel task, not yet sent as of this writing.

## Open questions

- Whether Swiggy's MCP payment tools are agent-executed or user-redirected —
  unverified, blocks the Phase-4 Swiggy rail decision.
- Whether Swiggy Builders Club has a sandbox.
- Tailscale dependency for phone Face ID (Phase 1) — not yet installed.

## Next session should

Run the test suite (`pytest`), fix whatever the first real run surfaces,
then continue with Phase 1 (WebAuthn + Tailscale, `transaction-sentinel`,
adversarial suite).
