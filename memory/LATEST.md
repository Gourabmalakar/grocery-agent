# Grocery Agent — current state

*rolling summary; rewritten each session, never appended*

**Last updated:** 2026-09-18

## Where the build stands

Phase 0 complete and hardened by real usage (Gourab ran a real order through
it and found genuine bugs, all fixed). Phase 1 in progress:
`transaction-sentinel`'s mechanical checks, the evaluator criteria, and the
adversarial/killswitch/sentinel test suites are built and green; WebAuthn +
Tailscale are deferred until Gourab sets up Tailscale (see
`SETUP-WEBAUTHN.md`) — passphrase auth covers both approval points
meanwhile. 93 tests passing.

The Console was redesigned this session into a consumer-app-styled UI
(stepper, chat-style assistant bubble, item cards, receipt totals) — and in
the course of that redesign, found and fixed a real bug that had been there
since Phase 0's first commit: **there was no `StaticFiles` mount in
`console/app.py`**, so `/static/style.css` 404'd on every page load and the
whole console had been rendering completely unstyled the entire time. Fixed;
now covered by a regression test (`tests/e2e/test_pages_render.py`).

**MODE=dry_run by default. Nothing in this repo can touch real money or a
real merchant yet** — Phase 2 (real-site read-only adapters), Phase 3
(UPI handoff), Phase 4 (capped wallet rail, live mode) are not built.

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
- **The console/app.py `StaticFiles` mount was missing entirely through
  Phase 0** — every template linked `/static/style.css` but nothing served
  it. Found visually while redesigning the UI, not by any earlier test
  (nothing had asserted on the stylesheet's own response before). Fixed,
  and now asserted on.
- The `live` e2e fixture (real mockstore + console subprocesses) moved from
  `tests/e2e/conftest.py` to the top-level `tests/conftest.py` so other test
  directories (e.g. `tests/sentinel/`) can reuse it without a second copy of
  the process-spinning logic. The sentinel suite ended up not needing it —
  constructing order/ledger/mandate files directly was faster and just as
  real a test of the check functions — but it's available either way.
- `transaction-sentinel`'s checks read `console.approval` for order state
  rather than re-parsing order JSON files themselves — a deliberate, noted
  exception to "grocery/ doesn't import console/", since duplicating that
  parsing would be exactly the kind of second implementation the project
  elsewhere warns against.
- `item_unavailable` decisions now record `evidence=[{"source": merchant}]`
  (they didn't before) — needed to make `check_adapter_health` possible at
  all; without it, which merchant a miss happened at lived only in a
  free-text reasoning string, not in structured evidence.

## Open questions

- Whether Swiggy's MCP payment tools are agent-executed or user-redirected —
  unverified, blocks the Phase-4 Swiggy rail decision.
- Whether Swiggy Builders Club has a sandbox.
- Tailscale dependency for phone Face ID (Phase 1) — not yet installed;
  Gourab chose to defer this specifically and build the rest of Phase 1
  first (2026-09-18).
- The mock catalog is still only ~6 SKUs — realistic multi-item grocery
  lists will partially miss. Not yet decided whether to expand it.

## Next session should

Pick up whichever of these Gourab asks for next: WebAuthn + Tailscale setup
(once he's installed Tailscale — see `SETUP-WEBAUTHN.md`), the scheduled
heartbeat for sentinel sweeps (currently on-demand only, via the Control
page or `python -m grocery.sentinel.report`), Phase 2 real-site read-only
adapters, or expanding the mock catalog. Don't start any of these
unprompted — the last few sessions have each had a clear, specific ask;
keep following that pattern rather than front-running the plan.
