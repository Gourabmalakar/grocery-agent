"""
Real, read-only Zepto adapter, spoken over Zepto's own official MCP server
(https://mcp.zepto.co.in/mcp) rather than by scraping zeptonow.com.

WHY THE MCP AND NOT THE BROWSER. The website is store-gated behind a
`user-position` localStorage object and sits behind AWS WAF bot control
(an aws-waf-token cookie plus a JS challenge counter). A browser adapter is
technically possible, but this repo does not work around bot detection, and
Zepto has published a front door. The MCP also returns structured data
instead of parsed text, so it does not carry blinkit_browser.py's standing
risk of a redesign silently changing a price.

WHAT KEEPS THIS FROM BUYING SOMETHING. Zepto's README is blunt: the
integration "is not a sandbox or demo environment. Any order placed through
the Zepto MCP will be processed as a real Zepto order." The same server that
answers a price query also exposes cart and order-placement tools. Four
independent things stop us reaching them:

  1. THE GRANT — AND IT DOES NOT HOLD. This said "the merchant's own
     authorization server, not our code, is what makes a write impossible.
     This is the one that actually matters." Measured on 2026-09-27, it is
     false. We do request scope "tools:read" and nothing else, and
     grocery/mcp/oauth.py does refuse to persist anything broader — the
     issued token carries exactly `"scope": "tools:read"`. But tools/list
     against that token still offers update_cart, create_order,
     create_wallet_order, create_upi_reserve_pay_order and
     create_online_payment_order. Zepto's authorization server records the
     scope; its MCP server does not appear to enforce it on the tool surface.
     So the grant is evidence of intent, not a barrier, and defence 2 below
     is load-bearing here exactly as it is for Swiggy. Run `--tools` to see
     the live surface; it prints a warning when write tools are on offer.
  2. THE ALLOWLIST. _call() refuses any tool name outside _ALLOWED_TOOLS
     before dispatch — a positive allowlist, so a tool Zepto adds next month
     is refused by default rather than reachable by default.
  3. THE CONTRACT. build_cart/quote/place_order raise unconditionally, even
     when authorized=True, exactly as blinkit_browser.py does.
  4. THE PROCESS BOUNDARY. The token lives on the Console's side. The worker
     asks for prices over HTTP and never holds a credential (CLAUDE.md
     rule 1).

DO NOT `claude mcp add` THIS SERVER. Zepto whitelists
claude://claude.ai/settings/connectors as a redirect URI, so adding it to a
Claude client is a two-command temptation. Doing so would hand order-placing
authority to any Claude session — which is precisely the failure this whole
repo is built to make impossible. The MCP belongs here, behind the grant and
the allowlist, or nowhere.

THE RESPONSE SCHEMA IS NOT PUBLISHED. Zepto's repo is README-only: no JSON
schema, no parameter table. _listing_from() therefore maps defensively and,
when it cannot find a field, RAISES WITH THE KEYS IT ACTUALLY SAW rather
than inventing a price. An unsourced price is worse than a missing one
(CLAUDE.md rule 3), and a guessed field mapping is an unsourced price with
extra steps. Run `python -m grocery.adapters.zepto_mcp --probe "milk"` to
see the real shape and pin the mapping against it.
"""
from __future__ import annotations

import time
from typing import Any

from grocery import config
from grocery.adapters.base import CartLine, Listing, Quote, StorefrontAdapter
from grocery.compare import normalise
from grocery.mcp import oauth
from grocery.mcp.client import McpAuthRequired, McpClient, McpError, payload_of

MERCHANT_ID = "zepto"
MCP_ENDPOINT = "https://mcp.zepto.co.in/mcp"
MCP_RESOURCE = "https://mcp.zepto.co.in"
TOKEN_NAME = "zepto_mcp_token"
REQUIRED_SCOPE = "tools:read"
PRICE_SOURCE = "zepto_mcp"

# Positive allowlist. Names are confirmed against a live tools/list during
# `--connect`; assert_surface_is_read_only() reports any drift rather than
# letting a renamed tool fail silently as "no results".
# update_user_name is DELIBERATELY ABSENT, and was briefly present. It PATCHes
# a Zepto profile, so it is a write by any honest reading, and keeping it meant
# carrying a named exception through the write guard below. Two things made
# removing it strictly better than excusing it. Zepto's own tool contract says
# the name "must be asked for — never generate it yourself", which is not a
# thing an unattended worker can do. And completing registration from here does
# not work anyway: see _ensure_registered. So registration is a human action in
# Zepto's app, this adapter has no exceptions to its write guard at all, and
# tests/adversarial/test_zepto_write_tools_refused.py can assert that absolutely.
_ALLOWED_TOOLS = frozenset({
    "search_products", "search",
    "get_user_details",
    "list_saved_addresses", "select_saved_address",
})

# Belt to the allowlist's braces: even a mis-typed entry above can never be a
# write, because anything whose name reads like one is refused outright.
#
# WIDENED 2026-09-27 AFTER READING THE REAL SURFACE, and the reason is worth
# keeping. The first pattern set was written from Zepto's README and matched
# "create_order" literally. A live tools/list under a tools:read grant actually
# offers create_wallet_order, create_upi_reserve_pay_order,
# create_online_payment_order, update_cart, add_saved_address, update_drop_zone
# and select_store — and the old patterns caught only two of them. The positive
# allowlist still refused every one, so nothing was ever reachable; but the belt
# was reporting "read only" over a surface that can spend money, which is the
# kind of quiet wrongness that survives until it matters.
#
# These deliberately over-match: get_order_detail, list_order_history and
# get_past_order_items are reads, and are flagged anyway. Refusing a read by
# accident costs a listing. Permitting a write by accident costs an order.
_WRITE_SHAPED = ("cart", "checkout", "pay", "_order", "order_", "add_",
                 "update_", "remove_", "delete_", "clear_", "apply_",
                 "place_", "select_store", "zepto_shop")


def _is_write_shaped(tool: str) -> bool:
    """No exceptions, by design — see the note on _ALLOWED_TOOLS."""
    return any(marker in tool for marker in _WRITE_SHAPED)


class ZeptoNotConnected(RuntimeError):
    """No usable token. The fix is always a human running --connect; this
    adapter never initiates an authorization flow on its own."""


class WriteToolRefused(PermissionError):
    """Something tried to call a tool that could change state at Zepto."""


class ZeptoRegistrationIncomplete(RuntimeError):
    """Zepto's MCP will not answer a shopping tool for this account yet. The
    fix is always a human action in Zepto's own app — never a retry, and never
    treating it as "no results", which is indistinguishable from an empty
    shelf."""


class ZeptoSchemaUnknown(RuntimeError):
    """The response did not carry the fields we need, and guessing would
    manufacture a price. Carries the observed keys so the mapping can be
    pinned against reality instead of against hope."""


# ---- token plumbing ----------------------------------------------------

def _access_token() -> str:
    token = oauth.load(TOKEN_NAME)
    if token is None:
        raise ZeptoNotConnected(
            "Zepto is not connected. Run:\n"
            "    ./.venv/bin/python -m grocery.adapters.zepto_mcp --connect"
        )
    if token.is_expired() and token.refresh_token:
        fresh = oauth.refresh_token(
            token.token_endpoint,
            refresh_tok=token.refresh_token,
            client_id=token.client_id,
            scope=REQUIRED_SCOPE,
            resource=MCP_RESOURCE,
        )
        token = _stored_from_response(fresh, previous=token)
        oauth.store(TOKEN_NAME, token, allowed_scopes={REQUIRED_SCOPE})
    return token.access_token


def _stored_from_response(response: dict[str, Any], *, previous: oauth.StoredToken | None = None,
                          client_id: str = "", token_endpoint: str = "") -> oauth.StoredToken:
    expires_in = response.get("expires_in")
    return oauth.StoredToken(
        access_token=response["access_token"],
        # Servers may omit refresh_token on refresh; keeping the old one is
        # correct and avoids locking ourselves out on the next expiry.
        refresh_token=response.get("refresh_token") or (previous.refresh_token if previous else ""),
        expires_at=(time.time() + float(expires_in)) if expires_in else 0.0,
        scope=response.get("scope") or (previous.scope if previous else ""),
        client_id=client_id or (previous.client_id if previous else ""),
        token_endpoint=token_endpoint or (previous.token_endpoint if previous else ""),
        resource=MCP_RESOURCE,
    )


def is_connected() -> bool:
    return oauth.load(TOKEN_NAME) is not None


def connection_status() -> dict[str, Any]:
    """Safe for /health and the Control page — never exposes the token."""
    token = oauth.load(TOKEN_NAME)
    if token is None:
        return {"connected": False}
    return {"connected": True, **token.redacted()}


# ---- the adapter -------------------------------------------------------

class ZeptoMCPAdapter(StorefrontAdapter):
    merchant_id = MERCHANT_ID

    def __init__(self) -> None:
        self._client: McpClient | None = None
        self._registered: bool = False
        # Sticky: once the gate has refused us, every later search fails fast
        # from memory instead of asking Zepto the same question per query.
        self._registration_error: ZeptoRegistrationIncomplete | None = None

    def _connected_client(self) -> McpClient:
        if self._client is None:
            client = McpClient(
                MCP_ENDPOINT,
                client_name="grocery-agent",
                token_provider=_access_token,
            )
            client.connect()
            self._client = client
        return self._client

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in _ALLOWED_TOOLS or _is_write_shaped(tool):
            raise WriteToolRefused(
                f"tool {tool!r} is not on this adapter's read-only allowlist "
                f"({sorted(_ALLOWED_TOOLS)}). This adapter never calls a tool that "
                "could change state at Zepto."
            )
        try:
            return payload_of(self._connected_client().call_tool(tool, arguments))
        except McpAuthRequired as exc:
            raise ZeptoNotConnected(
                "Zepto rejected our token. Re-connect with "
                "`python -m grocery.adapters.zepto_mcp --connect`."
            ) from exc

    def available_tools(self) -> list[dict[str, Any]]:
        return self._connected_client().list_tools()

    def assert_surface_is_read_only(self) -> dict[str, Any]:
        """Compares what the server offers this token against what we allow.

        Under a tools:read grant the write tools should not appear at all. If
        they do, the grant is broader than we asked for and that is a finding,
        not a curiosity — it is reported, and the caller decides.
        """
        offered = [t.get("name", "") for t in self.available_tools()]
        writes = [n for n in offered if _is_write_shaped(n)]
        return {
            "offered": offered,
            "allowed": sorted(_ALLOWED_TOOLS),
            "write_shaped_offered": writes,
            "read_only": not writes,
            "usable": sorted(set(offered) & _ALLOWED_TOOLS),
        }

    def _search_tool(self) -> str:
        offered = {t.get("name", "") for t in self.available_tools()}
        for candidate in ("search_products", "search"):
            if candidate in offered:
                return candidate
        raise ZeptoSchemaUnknown(
            f"no known search tool on the Zepto MCP; it offers {sorted(offered)}"
        )

    def _ensure_registered(self) -> None:
        """Complete Zepto's one-time profile + address gate, once per process.

        VERIFIED BY HAND, 2026-09-27 — THIS GATE CANNOT CURRENTLY BE CLEARED
        FROM THE CLIENT SIDE ON THIS ACCOUNT, and the code below is written to
        say so rather than to keep trying. What was actually observed, on a
        freshly initialised session and a freshly refreshed token:

            get_user_details      -> "Name: Gourab Malakar  Registered: Yes"
            update_user_name      -> "Registration complete! Welcome to Zepto"
            list_saved_addresses  -> "Registration required. Please call
                                      get_user_details first, then
                                      update_user_name ..."
            search_products       -> same
            get_past_order_items / list_order_history /
            get_location_serviceability / view_cart -> same

        So the server contradicts itself: the profile tool reports the account
        registered while every shopping tool reports it is not. Ruled out, each
        by experiment rather than by argument: call ordering inside one session,
        a brand-new MCP session, a brand-new access token minted by refresh, a
        cookie-preserving HTTP session (the server sets no cookie), and a
        different clientInfo name. Zepto's own tracker carries the identical
        contradiction on a different account as issue zeptonow/mcp#33, open
        since 2026-08-02 and unanswered.

        Two consequences are deliberate. It RAISES rather than returning no
        listings — an empty result would look exactly like an item being out of
        stock, which is the reporting defect that same issue is about. And it
        remembers the refusal, so an order for six items asks Zepto once, not
        six times.
        """
        if self._registered:
            return
        if self._registration_error is not None:
            raise self._registration_error
        try:
            self._register_and_select_address()
        except ZeptoRegistrationIncomplete as exc:
            self._registration_error = exc
            raise
        except McpError as exc:
            self._registration_error = _gate_failure(exc)
            raise self._registration_error from exc
        self._registered = True

    def _register_and_select_address(self) -> None:
        """The sequence update_user_name's own description makes mandatory:
        get_user_details, then (only if unregistered) update_user_name, then
        list_saved_addresses, then select_saved_address before any shopping."""
        details = self._call("get_user_details", {})
        if isinstance(details, str) and "Registered: No" in details:
            raise ZeptoRegistrationIncomplete(
                "This Zepto account has not finished registration. Completing it "
                "needs update_user_name, which this adapter deliberately cannot call "
                "— it writes to a profile, and Zepto's own contract says the name must "
                "be asked for and never generated. Finish the profile in the Zepto app "
                "or on zeptonow.com, then re-run --connect."
            )

        addresses = self._call("list_saved_addresses", {})
        addr_id = config.ZEPTO_ADDRESS_ID or _sole_address_id(addresses)
        if not addr_id:
            raise ZeptoRegistrationIncomplete(
                "Zepto returned no single saved delivery address to shop from "
                f"(got: {str(addresses)[:200]!r}). Save one in the Zepto app, or set "
                "ZEPTO_ADDRESS_ID to the one to use. Searching without a delivery "
                "context would price a neighbourhood nobody chose."
            )
        # A BROKEN ACKNOWLEDGEMENT IS NOT A FAILED OPERATION, and on this server
        # they are different things. select_saved_address opens an SSE stream and
        # then drops it — `requests` reports "Response ended prematurely" and our
        # client turns that into "empty response to tools/call". But the store
        # selection HAS taken effect: measured 2026-10-07, a search_products call
        # immediately afterwards returns real products where moments earlier it
        # answered "Store not selected."
        #
        # So the ack is treated as inconclusive rather than fatal, and SEARCH IS
        # THE TEST. If the store genuinely was not selected, the very next search
        # says so in plain words and the caller reports that instead. Raising on
        # a dropped ack would make Zepto permanently unusable over a cosmetic
        # defect in its response handling — which is what it did until now.
        try:
            self._call("select_saved_address", {"addressId": addr_id})
            print(f"[zepto] selected delivery address {addr_id}")
        except McpError as exc:
            if "empty response" not in str(exc):
                raise
            print(f"[zepto] select_saved_address dropped its response stream "
                  f"({exc}) — the selection usually lands anyway, so continuing "
                  f"and letting the first search be the test")

    def search(self, query: str) -> list[Listing]:
        self._ensure_registered()
        payload = self._call(self._search_tool(), {"query": query})
        rows = _rows_of(payload)
        listings = []
        for row in rows:
            listing = _listing_from(row, strict=False)
            if listing is not None:
                listings.append(listing)
        return listings

    def get_item(self, sku: str) -> Listing:
        raise NotImplementedError(
            "the Zepto MCP exposes search, not fetch-by-sku; callers should keep "
            "the Listing that search returned. Revisit if a get-product tool appears."
        )

    # ---- write methods: closed, permanently, at this layer -------------

    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        raise NotImplementedError("zepto_mcp is read-only — carting is Phase 3+ work")

    def quote(self, cart_ref: str) -> Quote:
        raise NotImplementedError("zepto_mcp is read-only — quoting a real cart is Phase 3+ work")

    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        raise NotImplementedError("zepto_mcp is read-only — ordering is Phase 3+ work")


# ---- registration helpers ----------------------------------------------

def _address_ids(payload: Any) -> list[str]:
    """Every address ID in a list_saved_addresses response.

    THE SHAPE IS UNVERIFIED and deliberately admitted as such: the gate above
    has never once let this tool answer, so there is no real response to pin
    this against — only the tool's description, which promises "structured
    address data with IDs". It therefore accepts a list, a wrapped list, or
    prose, and returns nothing at all rather than half-guessing.
    """
    ids: list[str] = []
    if isinstance(payload, str):
        import re
        ids = re.findall(r"(?:addressId|address_id|ID)[\"'\s:]+([a-zA-Z0-9_-]{6,})", payload)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                for key in ("id", "addressId", "address_id"):
                    if item.get(key):
                        ids.append(str(item[key]))
                        break
    elif isinstance(payload, dict):
        for key in ("addresses", "savedAddresses", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return _address_ids(payload[key])
        for key in ("id", "addressId", "address_id"):
            if payload.get(key):
                ids.append(str(payload[key]))
                break
    return ids


def _sole_address_id(payload: Any) -> str | None:
    """The account's address ID only when there is exactly ONE of them.

    Taking "the first" of several would pick a delivery neighbourhood nobody
    chose and then price it confidently — the same class of mistake
    blinkit_browser.py refuses to make when lat/lon are missing. Several
    addresses means ZEPTO_ADDRESS_ID has to say which.
    """
    ids = _address_ids(payload)
    return ids[0] if len(ids) == 1 else None


def _gate_failure(exc: McpError) -> ZeptoRegistrationIncomplete:
    """Turn Zepto's self-contradicting gate message into something a human can
    act on. Anything else is passed through with its own text intact."""
    if "Registration required" not in str(exc):
        return ZeptoRegistrationIncomplete(f"Zepto refused the setup sequence: {exc}")
    return ZeptoRegistrationIncomplete(
        "Zepto's MCP is refusing every shopping tool for this account with "
        "\"Registration required\", while its own get_user_details reports the "
        "same account as Registered: Yes. Verified 2026-09-27 that this cannot be "
        "cleared from the client side: correct call order, a fresh MCP session, a "
        "freshly refreshed token and a cookie-preserving HTTP session all hit it. "
        "It is Zepto's bug — zeptonow/mcp#33, open since 2026-08-02.\n"
        "ALREADY RULED OUT, so do not spend time on them again: completing the "
        "profile and saving a delivery address in the Zepto app (done 2026-09-27 "
        "— the gate did not move), and refreshing the access token.\n"
        "What is left, in order:\n"
        "  1. A FULL re-authorization: `--connect`. A refresh reuses the same "
        "grant; only a fresh authorize re-derives the session from an account "
        "that now has an address. This is the last client-side lever there is.\n"
        "  2. If it persists, it is Zepto's to fix — comment on zeptonow/mcp#33 "
        "with this second reproduction, and shop the merchants that answer.\n"
        "Until then Zepto is left out of the comparison and SAID SO, never "
        "silently dropped and never reported as an empty shelf."
    )


# ---- response mapping --------------------------------------------------

_NAME_KEYS = ("name", "productName", "product_name", "title", "displayName")
_PRICE_KEYS = ("price", "sellingPrice", "selling_price", "discountedPrice",
               "discounted_price", "finalPrice", "final_price", "mrp")
_SKU_KEYS = ("sku", "id", "productId", "product_id", "variantId", "variant_id")
_BRAND_KEYS = ("brand", "brandName", "brand_name")
_PACK_KEYS = ("packSize", "pack_size", "quantity", "unit", "weight", "size", "variant")
_STOCK_KEYS = ("inStock", "in_stock", "available", "isAvailable", "is_available")


def _rows_of(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("products", "results", "items", "data", "productList"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                return _rows_of(value)
    return []


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


# RESOLVED BY PROBE, 2026-10-07 — ZEPTO QUOTES PAISE.
#
# This function used to refuse to divide, on the grounds that guessing is how a
# ₹72 milk becomes ₹0.72, and said the probe should settle it. The probe has now
# settled it, and the evidence is recorded here so nobody has to guess again:
#
#   {"name": "Milky Mist Lactose Free Milk", "price": 3400, "mrp": 4000,
#    "packSize": "1 pack (250 ml)"}
#
# ₹34.00 for 250 ml, with an MRP of ₹40.00 — both plausible, and the ratio is
# right. Corroborated across merchants on the same day: Zepto quoted 7700 for
# "Amul Taaza Homogenised Toned Milk 1 L", and Blinkit's live price for the same
# litre was ₹77.00. Two independent readings agreeing to the paisa is what makes
# this a measurement rather than a hunch.
#
# Why it mattered more than it looks: at 100x, Zepto could never win a
# comparison, so it would have sat in every basket looking merely expensive and
# would never have been chosen — a silently wrong merchant rather than a visibly
# broken one. tests/adapters/test_zepto_mapping.py pins the real row.
_PAISE_PER_RUPEE = 100


def _as_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / _PAISE_PER_RUPEE
    if isinstance(value, str):
        # A string carrying a ₹ sign is already rupees — only the numeric API
        # fields are paise. Converting both would be the ₹0.72 bug in the other
        # direction.
        cleaned = value.replace("₹", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    if isinstance(value, dict):
        return _as_price(_first(value, _PRICE_KEYS))
    return None


def _listing_from(row: dict[str, Any], *, strict: bool = True) -> Listing | None:
    name = _first(row, _NAME_KEYS)
    price = _as_price(_first(row, _PRICE_KEYS))
    if name is None or price is None:
        if strict:
            raise ZeptoSchemaUnknown(
                "could not find a name and price in a Zepto product row. "
                f"Observed keys: {sorted(row.keys())}. "
                "Pin the mapping against a real --probe dump rather than guessing."
            )
        return None

    # parse_pack_size, not parse_quantity_phrase: the latter reads what a
    # human asked for and returns a base kind ("volume_ml") that normalise()
    # cannot map back, silently dropping the listing out of comparison.
    pack_qty, pack_unit = 1.0, "pcs"
    parsed = (normalise.parse_pack_size(str(_first(row, _PACK_KEYS) or ""))
              or normalise.parse_pack_size(str(name)))
    if parsed:
        pack_qty, pack_unit = parsed

    stock_value = _first(row, _STOCK_KEYS)
    # THE SKU IS THE COMPOSITE, AND THAT IS WHAT MAKES CARTING POSSIBLE.
    # Zepto's MCP search returns productVariantId and storeProductId as separate
    # fields; its UCP cart API addresses a purchasable thing as
    # "{productVariantId}:{storeProductId}". Verified 2026-10-11 against a live
    # catalogue: every composite built this way was present in the UCP search
    # results for the same query.
    #
    # Keeping only the first half — which this did — meant an approved line
    # could not be put in a real basket, and the fallback was searching the
    # merchant for the product's own title. Those titles are things like
    # "Coca-Cola PET| Cola Sparkling Soft Drink | The Coca-Cola Company", which
    # is a product name and a terrible search query; it found nothing and the
    # cart build failed (order_efaa1edd24a1). Carrying both halves turns that
    # search into no lookup at all. Same reasoning as swiggy_mcp.py's spinId:skuId.
    variant_id = str(row.get("productVariantId") or _first(row, _SKU_KEYS) or "")
    store_product_id = str(row.get("storeProductId") or "")
    sku = f"{variant_id}:{store_product_id}" if store_product_id else variant_id

    return Listing(
        merchant=MERCHANT_ID,
        sku=sku,
        name=str(name),
        brand=str(_first(row, _BRAND_KEYS) or ""),
        pack_qty=pack_qty,
        pack_unit=pack_unit,
        price_inr=price,
        in_stock=True if stock_value is None else bool(stock_value),
        category="",
        price_source=PRICE_SOURCE,
        asof=time.time(),
        # raw_description is deliberately left empty: merchant-controlled
        # prose is untrusted (CLAUDE.md rule 2) and nothing downstream needs
        # it, so the safest place for it is nowhere.
        raw_description="",
    )


# ---- one-time connect, and the probe that pins the schema --------------

def connect_interactively(*, open_browser: bool = True) -> dict[str, Any]:
    """Runs the OAuth dance once and stores a tools:read token.

    Deliberately a human-run CLI, never something the worker or an agent can
    trigger: linking a real Zepto account is Gourab's decision, taken at a
    keyboard, and the OTP goes to his phone. Nothing here types a credential
    on his behalf — it opens a URL and waits for the redirect.

    The client is registered dynamically (RFC 7591) against a loopback
    redirect on an ephemeral port, so there is no client secret to store and
    nothing to pre-arrange with Zepto.
    """
    import http.server
    import threading
    import urllib.parse
    import webbrowser

    received: dict[str, str] = {}
    done = threading.Event()

    class _Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib's spelling
            query = urllib.parse.urlparse(self.path).query
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in received
            self.wfile.write(
                b"<h2>Zepto connected. You can close this tab.</h2>" if ok
                else b"<h2>Authorization failed. Check the terminal.</h2>"
            )
            done.set()

        def log_message(self, *_args):
            pass  # a local callback server should not spam the terminal

    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    redirect_uri = f"http://localhost:{server.server_port}/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    resource_meta = oauth.discover_resource(MCP_RESOURCE)
    if REQUIRED_SCOPE not in resource_meta.get("scopes_supported", []):
        raise oauth.OAuthError(
            f"Zepto no longer advertises {REQUIRED_SCOPE}; it offers "
            f"{resource_meta.get('scopes_supported')}. Refusing to request a broader scope."
        )
    auth_meta = oauth.discover_auth_server(resource_meta["authorization_servers"][0])
    registration = oauth.register_client(
        auth_meta["registration_endpoint"],
        redirect_uri=redirect_uri,
        client_name="Gourab's grocery butler (read-only)",
    )
    pending = oauth.begin_authorization(
        authorization_endpoint=auth_meta["authorization_endpoint"],
        token_endpoint=auth_meta["token_endpoint"],
        client_id=registration["client_id"],
        redirect_uri=redirect_uri,
        scope=REQUIRED_SCOPE,
        resource=MCP_RESOURCE,
    )

    print(f"\nRequesting scope: {REQUIRED_SCOPE}  (read-only — this token cannot order)")
    print("Open this URL, sign in with your mobile + OTP, and approve:\n")
    print(f"  {pending.authorize_url}\n")
    if open_browser:
        webbrowser.open(pending.authorize_url)

    if not done.wait(timeout=300):
        server.shutdown()
        raise oauth.OAuthError("timed out after 5 minutes waiting for the Zepto redirect")
    server.shutdown()

    if "code" not in received:
        raise oauth.OAuthError(f"authorization failed: {received}")
    if received.get("state") != pending.state:
        # A mismatched state means the redirect did not come from the flow we
        # started. Never exchange such a code.
        raise oauth.OAuthError("state mismatch on the OAuth redirect — refusing to continue")

    response = oauth.exchange_code(pending, received["code"], resource=MCP_RESOURCE)
    token = _stored_from_response(
        response,
        client_id=registration["client_id"],
        token_endpoint=auth_meta["token_endpoint"],
    )
    oauth.store(TOKEN_NAME, token, allowed_scopes={REQUIRED_SCOPE})
    return token.redacted()


if __name__ == "__main__":
    import json as _json
    import sys

    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        print("usage:\n"
              "  python -m grocery.adapters.zepto_mcp --connect          one-time OAuth (tools:read)\n"
              "  python -m grocery.adapters.zepto_mcp --status           show the stored grant, redacted\n"
              "  python -m grocery.adapters.zepto_mcp --tools            what the server offers this token\n"
              "  python -m grocery.adapters.zepto_mcp --probe <query>    dump ONE raw row, to pin the schema\n"
              "  python -m grocery.adapters.zepto_mcp <query>            live search")
        raise SystemExit(0)

    if args[0] == "--connect":
        print(_json.dumps(connect_interactively(), indent=2))
        raise SystemExit(0)

    if args[0] == "--status":
        print(_json.dumps(connection_status(), indent=2))
        raise SystemExit(0)

    adapter = ZeptoMCPAdapter()

    if args[0] == "--tools":
        report = adapter.assert_surface_is_read_only()
        print(_json.dumps(report, indent=2))
        print("\nread-only surface" if report["read_only"]
              else "\nWARNING: write-shaped tools are on offer — the grant is broader than requested")
        raise SystemExit(0)

    if args[0] == "--probe":
        query = " ".join(args[1:]) or "milk"
        raw = adapter._call(adapter._search_tool(), {"query": query})
        rows = _rows_of(raw)
        print(f"rows: {len(rows)}")
        if rows:
            print("\nfirst row, verbatim — pin _listing_from() against this:")
            print(_json.dumps(rows[0], indent=2)[:3000])
        else:
            print("\nno rows recognised; top-level payload was:")
            print(_json.dumps(raw, indent=2)[:3000] if raw is not None else "None")
        raise SystemExit(0)

    try:
        results = adapter.search(" ".join(args))
    except (ZeptoRegistrationIncomplete, ZeptoNotConnected) as exc:
        # A traceback here would bury the one thing the reader needs: what to
        # do next. The adapter cannot fix either of these itself.
        print(f"\nzepto is unavailable:\n\n{exc}\n")
        raise SystemExit(2)
    print(f"{len(results)} listings from {MERCHANT_ID}\n")
    for item in results[:25]:
        stock = "" if item.in_stock else "  (out of stock)"
        print(f"  {item.name[:45]:<45} {item.pack_qty:>6g}{item.pack_unit:<4} "
              f"₹{item.price_inr:>7.2f}{stock}")
