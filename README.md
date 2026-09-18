# Grocery Agent

A controlled proof-of-concept in agentic commerce: an AI agent that shops for
groceries — searches merchants, compares carts on true landed cost, and
proposes a purchase — while a human reviews and approves every cart and every
payment. The point is not the shopping; it's proving that an agent can act
**autonomously but controllably**, with the blast radius bounded by
construction rather than by good behaviour.

Full design rationale: [`.claude/plans/00-build-plan.md`](.claude/plans/00-build-plan.md).
Load-bearing summary for anyone (or any agent) working in this repo: [`CLAUDE.md`](CLAUDE.md).

## What's built

**Phase 0 — the loop, entirely fake:**

- Signed, hash-chained mandates (Intent, IntentAmendment, Cart, Payment) —
  AP2-shaped, locally signed with ES256.
- Two independent hash-chained logs: `data/audit/` (facts) and
  `data/audit/decisions/` (reasoning) — both verifiable by recomputation,
  `python -m grocery.audit.verify`.
- A hard-capped SQLite spend ledger (`grocery/ledger/`) checked before every
  payment, never inferred from a prompt.
- A three-tier kill switch (PAUSE / PANIC / BURN) that fails closed and
  works from any terminal (`touch console/PANIC`) without the app running.
- A mock storefront (`mockstore/`) — three fake merchants with real-shaped
  pricing quirks (minimum orders, delivery thresholds, a deliberately
  poisoned product description) — so the whole loop runs with zero real
  money, zero real sites, zero network dependency.
- The Console (`console/`) — a FastAPI app that is simultaneously the UI and
  the broker: search → compare → revise → approve cart → approve payment →
  execute, with a real state machine enforcing what can happen when. Styled
  as a consumer app, not an admin panel — a stepper, a chat-style assistant
  message in plain English, item cards and a receipt-style total, with the
  full machine-reasoning trace kept one tap away for whenever it's wanted.
- `grocery/narrator.py` — turns the cart, the comparison and the decision
  trace into that plain-English assistant message. Deterministic and
  template-based, not an LLM call, and never says anything not already
  computed elsewhere.
- The worker (`worker/`) — a genuinely separate OS process that only ever
  talks to the Console over HTTP (`worker/tools.py` is its entire interface)
  and can propose a cart but never approve or pay for one. Parses requested
  quantities ("30 eggs" actually buys 30 eggs' worth) and interprets
  free-text revisions narrowly, flagging plainly when it couldn't act on
  part of one rather than silently re-proposing an unchanged cart.

**Phase 1 — proving the guarantees (in progress):**

- `evals/criteria.yaml` — versioned, machine-checkable evaluator criteria
  with automatic failures, following the family office's pattern.
- `.claude/agents/cart-auditor.md` — the independent pre-approval reviewer.
- `.claude/agents/transaction-sentinel.md` + `grocery/sentinel/` — its
  mechanical checks: chain integrity, ledger-vs-order reconciliation,
  orphaned/stuck states, reasoning-vs-fact, behavioural drift against a
  rolling baseline, and per-merchant adapter health. Trips PANIC itself on
  a fatal finding. Run on demand from the Control page or via
  `python -m grocery.sentinel.report`; a scheduled cadence is still Phase 1
  work (see the plan's heartbeat section).
- A dead-man's switch (`grocery.sentinel.report.dead_mans_switch_tripped`) —
  a sentinel that's gone quiet for 3+ hours shows up in `/health` as its own
  finding, on the theory that a silent watchdog is the failure this system
  is least allowed to have.
- `tests/adversarial/`, `tests/killswitch/` and `tests/sentinel/` — the
  suites every later phase must stay green against before `MODE=live`.

**Not yet built:** WebAuthn passkeys + Tailscale (deferred until Gourab sets
up Tailscale — passphrase auth covers the same approvals in the meantime;
see `SETUP-WEBAUTHN.md`), the scheduled heartbeat for sentinel sweeps, real
browser adapters (Phase 2), UPI handoff (Phase 3), and the capped wallet
rail + `MODE=live` (Phase 4).

**`MODE=dry_run` by default. Nothing in this repo can touch real money or a
real merchant yet.**

## Setup

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit if you want non-default caps
```

## Run it

Three terminals:

```bash
# 1. the mock storefront
./.venv/bin/python -m mockstore.app

# 2. the console (open http://127.0.0.1:8800 in a browser — set a passphrase on first visit)
./.venv/bin/python -m console.app

# 3. once you've submitted a request from the Ask page and have an order_id,
#    the worker (a separate process, exactly as the architecture requires):
./.venv/bin/python -m worker.run <order_id>
```

Then, in the Console: watch the trace, revise the cart in plain language if
you want to, approve the cart, approve the payment. `python -m
grocery.audit.verify` at any point to confirm both hash chains are intact;
the Control page's "Run a check now" button (or `python -m
grocery.sentinel.report`) runs a full transaction-sentinel sweep on demand.

## Test

```bash
./.venv/bin/python -m pytest
```

93 tests, no live server, no real site, no network — `tests/e2e/` spins up
mockstore and the console as real (but isolated, ephemeral) processes on
test-only ports, so even the two-process integration tests are fully
self-contained.

See `.claude/plans/00-build-plan.md`'s test plan (suites A–I) for what each
suite is meant to prove — `tests/adversarial/`, `tests/killswitch/` and
`tests/sentinel/` are the ones that gate every later phase.
