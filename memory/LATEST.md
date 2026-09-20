# Grocery Agent — current state

*rolling summary; rewritten each session, never appended*

**Last updated:** 2026-09-20

## Where the build stands

Phase 0 complete. Phase 1 partly done (sentinel's mechanical checks, evaluator
criteria, adversarial/killswitch/sentinel suites; WebAuthn + Tailscale deferred).
**Phase 2 now has three real read-only merchants** — Blinkit (browser), JioMart
(JSON API) and Zepto (official MCP). **138 tests passing, 8 skipped** (live-network,
opt-in via `RUN_LIVE_ADAPTER_TESTS=1`; all 23 pass when run).

**The real adapters are NOT wired into the order flow yet.** `build_cart → quote →
place_order` still runs entirely on `mockstore`, so the Console still shows
Freshmart/Quickbasket/Dailygrocer in "Why this store". That worker refactor is the
next step and is the one Gourab is waiting on.

`MODE=dry_run`. Nothing here can touch real money or place a real order.

## Facts established — do not re-derive

- **Zepto publishes a real `tools:read` scope.** Confirmed from
  `https://mcp.zepto.co.in/.well-known/oauth-protected-resource`:
  `["tools:read", "tools:write", "dev.ucp.shopping.cart:manage"]`. An earlier
  session concluded "no read-only scope exists" — that was wrong. This matters:
  the token can be read-only *by grant*, enforced by Zepto's authorization
  server, which is far stronger than an application-level allowlist.
- Zepto's auth server supports **Dynamic Client Registration** (`/register`),
  PKCE S256, public clients (`token_endpoint_auth_method: none`) and refresh
  tokens — so no pre-registered client_id and no redirect whitelisting needed.
- **Never `claude mcp add` the Zepto server.** It whitelists
  `claude://claude.ai/settings/connectors`, so it is designed to be added to a
  Claude client — which would give any Claude session order-placing authority.
- **JioMart is a Fynd Platform storefront**: `GET /api/service/application/
  catalog/v1.0/products/?q=&page_size=` returns clean JSON with `name`, `uid`,
  `brand.name`, `price.effective.min` (what you pay) vs `price.marked` (MRP),
  `sellable`, and declared `net-quantity-value`/`net-quantity-unit`.
- JioMart's gate is the header `x-location-detail: {"pincode":"...","country":"India"}`.
  An `x-pincode` header also works *from a browser that already holds JioMart's
  location cookies* but NOT from a bare client — that distinction cost a
  confusing 400.
- **JioMart answers an unserviceable pincode with HTTP 200 and a full catalogue**
  — its national marketplace, not local stock. 560001 → `item_total` 329,
  `seller_id` 1, many local `store_ids`. 999999 / 111111 / **110001** → 63,
  third-party sellers, one store each. `_assert_serviceable()` refuses the
  second. NB: 110001 was briefly misread as "Delhi has no fresh milk" — it was
  this fallback all along.
- **`parse_quantity_phrase()` is NOT a pack-size parser.** It reads what a human
  asked for ("30 eggs"), anchors on a LEADING number, and returns a base kind
  (`"volume_ml"`). Feeding that back into `normalise()` yields
  `base_kind="unknown"`, which silently drops a listing out of cross-merchant
  comparison without raising. New `parse_pack_size()` reads merchant titles
  (trailing size, real pack units). Caught by a test, not in production.
- **The MCP Python SDK is not safe to install here**: `pip install mcp` resolves
  pydantic to 2.13.5 and starlette to 1.6.0 against the pinned `pydantic==2.10.3`
  / `fastapi==0.115.6` — 17 packages. `grocery/mcp/` speaks the protocol on
  `requests` instead. The same hazard applies to `deepeval` (it also registers a
  pytest plugin) — it gets a second venv when the eval suite lands.
- **The delivery address decides what is on the shelves**, so it is data
  (`grocery/location.py`, `data/location.json`, edited on the Control page), not
  `.env` config. Coordinates are optional; their absence is disclosed and the
  merchant is dropped, never shopped at a guessed point.
- Zepto's homepage advertises "₹0 Handling Fee / ₹0 Delivery Fee" and JioMart
  "Shop for ₹199 to unlock free delivery" — both promo banners with T&Cs, **not**
  fee schedules. Not usable as `confidence="read"`.
- zeptonow.com sits behind **AWS WAF bot control** (`aws-waf-token` +
  `aws_waf_token_challenge_attempts`); its store gate is a `user-position`
  localStorage object. We use the MCP instead and do not work around the WAF.
- Amazon Now is app-only on the web (`amazon.in/now` → "Download the app") and
  Amazon won a March 2026 injunction against a third-party shopping agent.
  Flipkart's openly-readable catalogue is its shippable marketplace, not Minutes
  (Minutes is at `?marketplace=HYPERLOCAL` and needs a location; unproven).
  BigBasket: Akamai `Access Denied`.
- Standing repo facts unchanged: the worker is a separate OS process talking only
  over `worker/tools.py`; feedback amends the *intent*, never the cart; payment
  re-quotes at the last moment; `console/app.py` needs its `StaticFiles` mount;
  every `grocery.*`/`console.*` import in a test using the `live` fixture must be
  INSIDE the test function.

## Open questions

- **Gourab's real pincode.** 560001 (Bengaluru) is a *testing* value he chose;
  he will enter his actual address in the UI. Do not hardcode it anywhere.
- Zepto's response schema is unpublished (README-only repo). `_listing_from()`
  maps defensively and raises with observed keys rather than guessing. Run
  `--probe` after connecting and pin the mapping against the real dump.
- Whether Zepto's `tools:read` grant hides the write tools from `tools/list`
  entirely — `--tools` will answer it on first connect.
- Swiggy Instamart MCP: enrollment is a GitHub issue on
  `Swiggy/swiggy-mcp-server-manifest`. Not applied for. Gourab's call: keep as a
  third option / enhancement.
- Real delivery/handling fees still aren't readable without carting, so landed
  cost will be partial and must be disclosed (planned, not built).

## Next session should

Continue the approved plan at `~/.claude/plans/hey-i-wanted-to-clever-rocket.md`.
Next up is the **worker refactor** (steps 4–6): fees + disclosure, then dropping
`from mockstore.catalog import CATALOG` out of `worker/run.py` in favour of
`adapter.search()`, then the LLM planner. That refactor is the riskiest change in
the plan — capture `pytest -q` before and after and require an identical pass
list, and pin `MERCHANT_BACKEND=mock` in both `tests/conftest.py` fixtures or the
default suite will launch Chromium.
