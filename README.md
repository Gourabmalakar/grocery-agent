# Butler — an agentic grocery concierge for Indian quick-commerce

Tell it what you need in Telegram. It searches the real quick-commerce
merchants, compares them on true landed cost, builds a real basket in your own
account, and — where regulation and merchant APIs allow — places the order.
A human approves every cart and every payment.

The shopping is the excuse. The actual subject is **how much autonomy an agent
can safely be given when real money is involved**, and the answer is written
into the architecture rather than into a prompt.

```
Telegram  ──▶  Console (FastAPI, 127.0.0.1)  ──▶  merchant MCP / APIs
   │               │  approvals, mandates, ledger,
   │               │  kill switch, audit chains
   └── chat UI     └── worker (no credentials, allowed to be wrong)
```

---

## Why it exists

Two reasons, both honest:

1. **Comparing four quick-commerce apps by hand is tedious** and nobody
   actually does it, so you overpay by default. This does it in 60 seconds.
2. **To find out where agentic commerce in India genuinely stands** — not from
   press releases, but by building against the real APIs and recording what
   breaks.

What it found is in [Findings](#findings-what-actually-works-in-india-oct-2026),
and it is the more interesting half of this repository.

---

## How it works

**1. You send a list.** `2 bananas, nandini curd, amul butter 500g` in Telegram.

**2. Understanding.** An LLM splits the message into items and reads each one
for brand, pack size and intent. `amul butter 500g` is three constraints, not a
search string. The model is *advisory* — it never picks what to buy on its own.

**3. Search, in parallel, across every reachable merchant.** Each merchant has
its own adapter normalising a different response shape into one `Listing` type.

**4. Normalise and rank on landed cost.** Unit price, pack multiples, delivery,
handling. A ₹64 basket with a ₹38 small-cart fee loses to a ₹90 one that ships
free, and the ranking knows that.

**5. Audit the cart.** A separate checker asks, per line: *is this actually the
thing that was asked for?* It has rejected "Amul Masti Spiced Buttermilk" for
butter and "Spine Gourd" for bitter gourd. A rejected line is reported, never
silently substituted.

**6. Propose.** Both baskets land in the chat with items and totals, one marked
as recommended. **You choose** — the recommendation is a suggestion, not a
filter.

**7. Revise in chat.** `swap the bread for 2kg atta` re-runs only the affected
line. If the change can't be made, it says why rather than silently doing
nothing.

**8. Build the real cart** in your own merchant account, verified line by line
against what you approved, with the merchant's own bill read back.

**9. Pay** — by the method you pick (see [Payments](#payments)).

The full reasoning trace, the three-way price comparison, rejected
alternatives, the audit chains and the kill switch all live in the Console at
`127.0.0.1:8800`. Telegram is the interface; the Console is the record.

---

## Architecture

### The process boundary is the security model

```
worker/     no credentials. No network to a merchant with write scope.
            Allowed to be wrong. An AST test fails the build if it ever
            imports a cart or payment client.

console/    holds every credential, every approval, the ledger, the kill
            switch. Bound to 127.0.0.1 — never exposed.

bot/        relays Telegram. Holds a Console token accepted on routes that
            cannot spend, and refused on the one that can.

grocery/    adapters, mandates, ledger, audit, kill switch. No model, no
            prompt, no network except to merchants.
```

The worker is the component most likely to be wrong — it is where the language
model lives. So it is the component with nothing to lose: four functions, no
credentials, no way to approve anything. That is enforced by a test that walks
the AST, not by convention.

### Agents and checkers

| Component | Job |
|---|---|
| **Understander** | Turns a free-text message into structured items with brand, size and quantity |
| **Searcher** | One adapter per merchant, normalising wildly different payload shapes |
| **Ranker** | Landed-cost comparison — unit price, pack maths, fees, thresholds |
| **Cart auditor** | Per-line: is this the thing that was asked for? Rejects lookalikes |
| **Reviser** | Applies one change to one line, from a closed operation set |
| **Narrator** | Explains what happened, including what it could not do |
| **Sentinel** | Reconciles the ledger against orders; flags drift |

The reviser's operation set is deliberately closed — `set_quantity`,
`swap_brand`, `drop`, `add`, `reject_product`, `replace_item`, `ask`,
`cannot_do`. A model that cannot invent an operation cannot invent a surprise.

### Mandates, signed and chained

Every step mints a signed document: **Intent** → **Amendment** → **Cart** →
**Payment**. ES256 over canonical JSON, hash-chained, append-only. Two separate
chains — one for facts, one for reasoning — both verifiable by recomputation:

```bash
python -m grocery.audit.verify
```

The shape is AP2-flavoured (Google's Agent Payments Protocol) because that is
where this is going, but it is locally signed and not networked to anything.

---

## Safety

Nothing here relies on the agent behaving well.

| Control | What it does |
|---|---|
| **Spend caps** | Per order, per day, per month. Checked against a SQLite ledger before any payment, on IST day boundaries |
| **Two switches for autonomy** | `MODE=live` *and* `PAYMENT_BACKEND` must both be set before a rupee can move |
| **Kill switch** | `touch console/PANIC` stops everything from any terminal, with the app down, no env needed. Fails closed if unreadable |
| **Durable idempotency** | `sha256(order_id + cart_hash)` written *before* the rail is called, so a crash mid-payment leaves a trace to reconcile instead of an unknown |
| **Cart-hash drift refusal** | The basket you approved is the basket that gets paid for, to the paisa |
| **Last-moment re-quote** | The merchant's own total is read again immediately before paying |
| **Signature verification** | The cart mandate's ES256 signature is checked at payment time, not just its hash |
| **Passphrase per payment** | One basket, five minutes, single use. Deleted from the chat before it is verified, never stored. Three failures pause the system |
| **Single allowlisted chat** | Every other Telegram chat id is dropped and audited |
| **Positive tool allowlists** | Merchant clients can call named tools only; anything new is refused by default |
| **Append-only audit** | Two hash chains, verifiable by recomputation |
| **Never retry an unknown** | A timeout is not a failure. Ambiguous payments go to `NEEDS_RECONCILIATION` and stop |

### Tests as guardrails

**431 tests**, and the interesting ones are adversarial — they assert that
things *cannot* happen:

- the worker cannot import a payment client (AST walk)
- a merchant cart client cannot call `checkout`, by name and by shape
- a passphrase never reaches a log, an order file or an audit record
- a demo button cannot reach any state-changing call
- no committable file contains anything shaped like a credential
- test modules cannot bind the real data directory

Most of these exist because something went wrong first. The comments say which.

---

## Findings: what actually works in India (Oct 2026)

Measured against live APIs, not documentation.

### Merchants

| Merchant | Search | Real cart | Order | Notes |
|---|---|---|---|---|
| **Swiggy Instamart** | ✅ | ✅ | ✅ | Official MCP. Cart keyed by address, so **the agent's cart *is* your app cart**. Full bill breakdown — the only merchant that gives a true landed total |
| **Zepto** | ✅ | ⚠️ | ❌ | Official MCP + a UCP endpoint. Builds a real cart — but keyed by `deviceId`, so **it never appears in your app**, and its hand-off link discards the cart id. Price comparison only |
| **JioMart** | ✅ | ❌ | ❌ | Unofficial JSON API. Read-only, permanently |
| **Blinkit** | ❌ | ❌ | ❌ | Bot-detection. The adapter **refuses to evade it** rather than try |

The Zepto result is the one worth internalising: its cart API works perfectly
and delivers a basket to *nobody*. Every signal said success.

### Payments — the regulatory wall

| Method | Agent can complete it? | Why |
|---|---|---|
| **Cash on delivery** | ✅ **yes** | No instrument touched. Blast radius is one order you can refuse at the door |
| **Swiggy Money** (merchant wallet) | ⚠️ in principle | Needs the merchant to set `agenticPaymentEligible` on your account. Ours is `false` |
| **UPI** — GPay, PhonePe, Paytm, CRED | ❌ **no, by design** | Every one is `upiIntent: true`. The intent launches the app for *you* to approve. **That approval is the point of UPI** |

**Money in a PhonePe wallet linked to a merchant does not make an agent able to
spend it.** UPI apps are not stored-value balances a merchant can debit; they
are apps that ask a human. In India today, **COD is the only genuinely
hands-off payment path**, with merchant-held prepaid wallets a close second
once merchants enable them.

---

## Running it

### Requirements

Python 3.11+, a Telegram bot token, and accounts at the merchants you want to
use.

### Setup

```bash
git clone https://github.com/Gourabmalakar/grocery-agent.git
cd grocery-agent
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then edit it
```

Minimum `.env`:

```bash
MODE=dry_run                 # nothing can move money
MERCHANT_BACKEND=mock        # fake merchants, no network
SESSION_SECRET_KEY=<random>
```

### Try it with no real anything

```bash
./.venv/bin/python -m mockstore.app &     # three fake merchants
./.venv/bin/python -m console.app         # http://127.0.0.1:8800
./.venv/bin/python -m pytest -q           # 431 tests, no network
```

### Connect a real merchant (read-only)

```bash
./.venv/bin/python -m grocery.adapters.swiggy_mcp --connect
./.venv/bin/python -m grocery.adapters.swiggy_mcp --tools      # see the surface
./.venv/bin/python -m tools.whoami swiggy                      # whose account is this?
```

`--tools` prints what the server offers and what this client will touch. Worth
reading before trusting anything.

### Telegram

```bash
TELEGRAM_BOT_TOKEN=...       # from @BotFather
TELEGRAM_CHAT_ID=...         # yours; every other chat is refused
BOT_API_TOKEN=<random>
```

```bash
./.venv/bin/python -m bot.run
```

### Enabling payments

Deliberately awkward. Both switches, plus a passphrase:

```bash
PAYMENT_BACKEND=off|handoff|cod|wallet
MODE=live
CAP_PER_ORDER_INR=500
CAP_PER_DAY_INR=500
```

```bash
./tools/set_passphrase.sh    # prompts; never echoed, never in shell history
```

Each level includes the ones before it. `handoff` charges nothing — the basket
lands in your app and you pay there. `cod` lets the agent place the order.
`wallet` needs the merchant to enable agentic payment. The Console **refuses to
start** with payments on and an unsafe configuration.

### When something goes wrong

```bash
touch console/PANIC                                  # stop everything, now
./.venv/bin/python -m tools.reconcile                # resolve unknown payments
./.venv/bin/python -m grocery.audit.verify           # check the chains
```

`reconcile` never places, cancels or retries anything — it reads the merchant's
order history and compares. If it cannot read it, it changes nothing and says
so.

---

## What this is not

- **Not a product.** One user, one machine, loopback only.
- **Not a scraper.** Official APIs and MCP endpoints. Where a merchant blocks
  automation, the adapter refuses rather than evades.
- **Not autonomous.** Every cart and every payment is approved by a human.
- **Not an eval suite.** There is no systematic measure of how often the wrong
  product is picked. Spend caps and per-order approval are the defence, and
  that is a stated limitation, not an oversight.

## Known limitations

- Swiggy tokens expire every 5 days with no refresh; Zepto's roughly weekly.
- The passphrase is typed into Telegram, so it exists on Telegram's servers.
  Mitigated (separate secret, deleted on receipt, never stored, single-use,
  five-minute scope) — **not eliminated**.
- Chat state is in memory; restarting the bot orphans an in-flight order.
- Search quality is the weakest link. `karela` returns Spine Gourd at one
  merchant and seed packets at another.

## Licence

MIT. No affiliation with Swiggy, Zepto, JioMart or Blinkit.
