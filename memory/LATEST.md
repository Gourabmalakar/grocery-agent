# Grocery Agent — current state

*rolling summary; rewritten each session, never appended*

**Last updated:** 2026-09-18

## Where the build stands

Phase 0 complete and hardened by real usage. Phase 1 in progress
(`transaction-sentinel`'s mechanical checks, evaluator criteria, and the
adversarial/killswitch/sentinel suites are built and green; WebAuthn +
Tailscale deferred — see `SETUP-WEBAUTHN.md`). Phase 2 started: a real,
read-only Blinkit adapter is live and verified against the actual site.
98 tests passing (2 more skipped by default — live-network, opt-in).

The Console was redesigned into a consumer-app-styled UI this session
(stepper, chat-style assistant bubble, item cards, receipt totals) — and in
the course of that redesign, found and fixed a real bug that had been there
since Phase 0's first commit: **there was no `StaticFiles` mount in
`console/app.py`**, so `/static/style.css` 404'd on every page load and the
whole console had been rendering completely unstyled the entire time. Fixed;
now covered by a regression test.

**MODE=dry_run by default. Nothing in this repo can touch real money or a
real merchant yet** — the order flow (build_cart → quote → place_order)
still runs entirely on MockAdapter. Phase 3 (UPI handoff) and Phase 4
(capped wallet rail, live mode) are not built.

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
- **Neither Swiggy Instamart's official MCP nor Zepto's official MCP is
  "read-only, no account" — checked live this session.** Both require
  OAuth-linking a real personal account for essentially everything,
  including search; Zepto's is explicitly "not a sandbox," and any order it
  places is real. Swiggy Builders Club is real and invite-led for
  production (`builders@swiggy.in`, not yet sent), with a free local
  sandbox for prototyping against tool schemas — but that doesn't cover an
  anonymous/read-only tier either. **BigBasket blocks non-browser traffic
  outright** (Akamai `Access Denied` on the plain homepage). Given this,
  Gourab chose: build the Blinkit browser adapter now, revisit real MCP
  integration only after Phase 3 proves out — don't jump to linking a real
  account ahead of the plan's phasing.
- **Blinkit gates its catalogue behind a delivery location, not a login** —
  no account needed to see real prices. That location lives in two plain
  cookies (`gr_1_lat` / `gr_1_lon`, real coordinates), discovered by
  inspecting the site as an ordinary visitor — `grocery/adapters/
  blinkit_browser.py` sets them directly rather than driving the
  interactive location-autocomplete picker on every call.
- Blinkit's page is Tailwind utility classes with no semantic
  `data-test-id`, and the DOM nesting around a given anchor text is NOT
  stable across page loads (confirmed directly — the same "Inclusive of all
  taxes" disclaimer had different ancestor structure on two consecutive
  loads of the same product page). The adapter parses by TEXT-LINE SEQUENCE
  instead of DOM-tree position for exactly this reason.
- `console/app.py`'s `StaticFiles` mount was missing entirely through
  Phase 0 — every template linked `/static/style.css` but nothing served
  it. Found visually while redesigning the UI. Fixed, and now asserted on.
- The `live` e2e fixture (real mockstore + console subprocesses) moved from
  `tests/e2e/conftest.py` to the top-level `tests/conftest.py` so other test
  directories can reuse it — `tests/adapters/`'s MockAdapter conformance
  tests now use it too.
- Every `grocery.*`/`console.*` import in a test that uses the `live`
  fixture must happen INSIDE the test function, never at module level — the
  fixture purges those modules from `sys.modules` before yielding, so a
  module-level import binds to a stale, pre-purge class and later
  `isinstance()` checks against it silently fail even on structurally
  identical objects. Hit this directly writing `tests/adapters/`; fixed by
  moving the imports inline, which is already the convention every other
  test file using `live` follows.
- `transaction-sentinel`'s checks read `console.approval` for order state
  rather than re-parsing order JSON files themselves — a deliberate, noted
  exception to "grocery/ doesn't import console/".
- `item_unavailable` decisions now record `evidence=[{"source": merchant}]`
  — needed to make `check_adapter_health` possible at all.

## Open questions

- Whether Swiggy's MCP payment tools are agent-executed or user-redirected —
  unverified, blocks the Phase-4 Swiggy rail decision.
- Tailscale dependency for phone Face ID (Phase 1) — deferred by Gourab's
  choice (2026-09-18).
- The mock catalog is still only ~6 SKUs — realistic multi-item grocery
  lists will partially miss the order flow (which still runs on mock).
  Real prices are now visible via the Blinkit adapter regardless.
- Whether to build Zepto/BigBasket browser adapters next, or move on to
  wiring Blinkit's real prices into an actual (still mock-carting) landed-
  cost comparison, or start Phase 3 (UPI handoff) — not yet decided.

## Next session should

Pick up whichever of these Gourab asks for next: another browser adapter
(Zepto is likely similarly tractable; BigBasket is blocked outright and
would need a different approach entirely), wiring real Blinkit prices into
the worker's comparison step (still mock-carting), WebAuthn + Tailscale
setup, the scheduled sentinel heartbeat, or Phase 3 UPI handoff. Don't start
any of these unprompted — keep following the session-by-session pattern
rather than front-running the plan.
