"""
Real, read-only Swiggy Instamart adapter, spoken over Swiggy's official MCP
server (https://mcp.swiggy.com/im) via the Builders Club programme.

WHY THE ALLOWLIST MATTERS MORE HERE THAN FOR ZEPTO. Zepto publishes a
"tools:read" scope that lets the authorization server itself refuse write
authority. Swiggy does not — "mcp:tools" covers all 19 tools including
checkout, update_cart, confirm_order, and clear_cart. The scope cannot be
the defence. The allowlist is the ONLY thing between a bug and a real order.

WHAT KEEPS THIS FROM BUYING SOMETHING — four defences, same as Zepto:

  1. THE ALLOWLIST. _call() refuses any tool name outside _ALLOWED_TOOLS
     before dispatch — a positive allowlist, so a tool Swiggy adds next
     month is refused by default.
  2. THE WRITE GUARD. Any tool whose name reads like a write is refused
     outright, even if it were somehow in _ALLOWED_TOOLS.
  3. THE CONTRACT. build_cart/quote/place_order raise unconditionally.
  4. THE PROCESS BOUNDARY. The token lives on the Console's side; the
     worker never holds a credential (CLAUDE.md rule 1).

Defence 1 cannot lean on the grant (unlike Zepto's tools:read), so it is
load-bearing here in a way it isn't there. Do not weaken it.

DO NOT `claude mcp add` THIS SERVER — same reasoning as zepto_mcp.py. The
whitelisted redirect URIs include claude:// and would let any Claude session
place real orders.

TOKEN LIFECYCLE. Swiggy v1.0 issues 5-day access tokens with no refresh
token. On expiry, a full re-authorization is needed. The adapter reports
credential_expired and the comparison proceeds without Swiggy, disclosed.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from grocery import config, location
from grocery.adapters.base import CartLine, Listing, Quote, StorefrontAdapter
from grocery.compare import normalise
from grocery.mcp import oauth
from grocery.mcp.client import McpAuthRequired, McpClient, McpError, payload_of

MERCHANT_ID = "swiggy"
MCP_ENDPOINT = "https://mcp.swiggy.com/im"
AUTH_BASE = "https://mcp.swiggy.com"
TOKEN_NAME = "swiggy_mcp_token"
REQUIRED_SCOPE = "mcp:tools"
PRICE_SOURCE = "swiggy_mcp"

_ALLOWED_TOOLS = frozenset({"search_products", "get_addresses", "your_go_to_items"})

_WRITE_SHAPED = ("cart", "order", "checkout", "confirm", "pay", "coupon",
                 "add_", "update_", "remove_", "delete_", "create_", "clear_",
                 "apply_", "place_")


class SwiggyNotConnected(RuntimeError):
    pass


class WriteToolRefused(PermissionError):
    pass


class SwiggySchemaUnknown(RuntimeError):
    pass


class SwiggyUnavailable(RuntimeError):
    """Swiggy cannot be searched right now for a reason that is not its fault
    — a delivery location with no coordinates. The comparison leaves it out and
    SAYS SO, exactly as blinkit_browser.py's AdapterUnavailable does."""


# ---- token plumbing ----------------------------------------------------

def _access_token() -> str:
    token = oauth.load(TOKEN_NAME)
    if token is None:
        raise SwiggyNotConnected(
            "Swiggy Instamart is not connected. Run:\n"
            "    ./.venv/bin/python -m grocery.adapters.swiggy_mcp --connect"
        )
    if token.is_expired():
        raise SwiggyNotConnected(
            "Swiggy Instamart token has expired (5-day lifetime, no refresh). "
            "Re-connect with:\n"
            "    ./.venv/bin/python -m grocery.adapters.swiggy_mcp --connect"
        )
    return token.access_token


def _stored_from_response(response: dict[str, Any], *, client_id: str = "",
                          token_endpoint: str = "") -> oauth.StoredToken:
    expires_in = response.get("expires_in")
    return oauth.StoredToken(
        access_token=response["access_token"],
        refresh_token="",
        expires_at=(time.time() + float(expires_in)) if expires_in else 0.0,
        scope=response.get("scope", REQUIRED_SCOPE),
        client_id=client_id,
        token_endpoint=token_endpoint,
        resource=AUTH_BASE,
    )


def is_connected() -> bool:
    token = oauth.load(TOKEN_NAME)
    return token is not None and not token.is_expired()


def connection_status() -> dict[str, Any]:
    token = oauth.load(TOKEN_NAME)
    if token is None:
        return {"connected": False}
    return {"connected": True, "expired": token.is_expired(), **token.redacted()}


# ---- the adapter -------------------------------------------------------

class SwiggyMCPAdapter(StorefrontAdapter):
    merchant_id = MERCHANT_ID

    def __init__(self) -> None:
        self._client: McpClient | None = None
        self._address_args: dict[str, Any] | None = None

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
        if tool not in _ALLOWED_TOOLS or any(marker in tool for marker in _WRITE_SHAPED):
            raise WriteToolRefused(
                f"tool {tool!r} is not on this adapter's read-only allowlist "
                f"({sorted(_ALLOWED_TOOLS)}). This adapter never calls a tool "
                "that could change state at Swiggy."
            )
        try:
            return payload_of(self._connected_client().call_tool(tool, arguments))
        except McpAuthRequired as exc:
            raise SwiggyNotConnected(
                "Swiggy rejected our token. Re-connect with "
                "`python -m grocery.adapters.swiggy_mcp --connect`."
            ) from exc

    def available_tools(self) -> list[dict[str, Any]]:
        return self._connected_client().list_tools()

    def assert_surface_is_read_only(self) -> dict[str, Any]:
        offered = [t.get("name", "") for t in self.available_tools()]
        writes = [n for n in offered if any(m in n for m in _WRITE_SHAPED)]
        return {
            "offered": offered,
            "allowed": sorted(_ALLOWED_TOOLS),
            "write_shaped_offered": writes,
            "read_only": not writes,
            "usable": sorted(set(offered) & _ALLOWED_TOOLS),
        }

    def _delivery_args(self) -> dict[str, Any]:
        """Swiggy needs a saved addressId, and GUEST MODE IS A DEAD END — which
        is worth writing down, because the tool schema says otherwise.

        search_products advertises `latitude`/`longitude` "for guest mode", and
        its description says plainly: "For guest mode, pass latitude and
        longitude; for signed-in mode, use addressId from get_addresses."
        Measured against the live server on 2026-09-27 with a valid token, all
        three of these came back "addressId is required":

            {query, latitude: 12.9767, longitude: 77.5993}   (numbers)
            {query, latitude: "12.9767", longitude: "77.5993"} (strings)
            {query}                                          (neither)

        get_addresses called WITH those coordinates does not resolve a candidate
        either — it returns resolutionSource "no_saved_address" and an empty
        candidateAddressIds. So guest mode appears to be for unauthenticated
        callers only, and an authenticated one must use a saved address. Do not
        re-derive this from the schema; the schema is wrong.

        Resolved once per process, not once per query: an order for six items
        should ask Swiggy for its address list once.
        """
        if self._address_args is None:
            self._address_args = {"addressId": self._resolve_address_id()}
        return dict(self._address_args)

    def _resolve_address_id(self) -> str:
        if config.SWIGGY_ADDRESS_ID:
            return config.SWIGGY_ADDRESS_ID
        payload = self._call("get_addresses", {})
        addresses = _addresses_of(payload)
        if not addresses:
            raise SwiggyUnavailable(
                "Swiggy has no saved delivery address on this account, and its "
                "search_products requires one — the latitude/longitude \"guest mode\" "
                "its schema advertises is refused for an authenticated token (verified "
                "2026-09-27). Add a delivery address in the Swiggy app, then this "
                "works with no further change. Creating one from here would need "
                "create_address, which is a write and is deliberately refused."
            )
        if len(addresses) > 1 and not config.SWIGGY_ADDRESS_ID:
            labels = ", ".join(f"{a.get('id')}={str(a.get('label') or a.get('name') or '?')[:24]}"
                               for a in addresses[:6])
            raise SwiggyUnavailable(
                f"Swiggy has {len(addresses)} saved addresses and nothing says which to "
                f"shop from ({labels}). Set SWIGGY_ADDRESS_ID in .env. Taking the first "
                "would price a neighbourhood nobody chose."
            )
        address = addresses[0]
        addr_id = str(address.get("id") or "")
        if not addr_id:
            # The OBSERVED keys, not this function's normalised ones — the whole
            # point of the message is to be able to fix the mapping from it.
            raise SwiggySchemaUnknown(
                "a Swiggy saved address carried no recognisable id. Observed keys: "
                f"{sorted(address.get('raw', {}).keys())}. Pin this against a real "
                "`--addresses` dump."
            )
        _warn_if_address_disagrees_with_location(address)
        return addr_id

    def search(self, query: str) -> list[Listing]:
        payload = self._call("search_products", {"query": query, **self._delivery_args()})
        rows = _rows_of(payload)
        listings: list[Listing] = []
        for row in rows:
            listings.extend(_listings_from_product(row))
        if rows and not listings:
            # 11 products in and 0 listings out is what a broken field mapping
            # looks like, and it is indistinguishable from an empty shelf to
            # everyone downstream. Say which it is (CLAUDE.md rule 3).
            raise SwiggySchemaUnknown(
                f"Swiggy returned {len(rows)} products for {query!r} and not one "
                "could be mapped to a Listing. The response shape has changed. "
                "Observed keys on the first product: "
                f"{sorted(rows[0].keys())}. Re-pin against `--probe`."
            )
        return listings

    def get_item(self, sku: str) -> Listing:
        raise NotImplementedError(
            "the Swiggy MCP exposes search, not fetch-by-sku; callers should "
            "keep the Listing that search returned."
        )

    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        raise NotImplementedError("swiggy_mcp is read-only — carting is Phase 3+ work")

    def quote(self, cart_ref: str) -> Quote:
        raise NotImplementedError("swiggy_mcp is read-only — quoting a real cart is Phase 3+ work")

    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        raise NotImplementedError("swiggy_mcp is read-only — ordering is Phase 3+ work")


# ---- pack size, and an independent check on it -------------------------

# "1 ltr x 12", "500 g x 2", "250 ml x 4" — Swiggy's multipack spelling.
_MULTIPACK_RE = re.compile(r"\bx\s*(\d+)\s*$", re.IGNORECASE)
# "7.7/100 ml", "51/100 g" — the price Swiggy itself publishes per 100 base units.
_UNIT_LEVEL_RE = re.compile(r"([\d.]+)\s*/\s*100\s*([a-zA-Z]+)")
_DECLARED_BASE_KIND = {"ml": "volume_ml", "l": "volume_ml", "ltr": "volume_ml",
                       "g": "mass_g", "gm": "mass_g", "kg": "mass_g"}


def _pack_of(variation: dict[str, Any], name: Any) -> tuple[float, str] | None:
    """Pack size INCLUDING the multipack count, which is the whole point.

    normalise.parse_pack_size() reads "1 ltr x 12" as one litre — it says so in
    its own docstring, and tells adapters that can read a declared pack count to
    prefer it (jiomart_api._pack_of does exactly this). Swiggy can: the count is
    the trailing "x 12" of quantityDescription.

    Getting this wrong is not cosmetic. Measured on the live server: "Amul Taaza
    Toned Milk" comes back as 1 ltr ₹77, "1 ltr x 4" ₹308, "1 ltr x 6" ₹462 and
    "1 ltr x 12" ₹924. Read naively, that is four listings all claiming to be one
    litre, three of them at up to twelve times the price — and a cart that picked
    one would buy twelve litres while every check passed.

    The multiplier is applied ONLY when the size came from quantityDescription.
    If it fell back to the product name, a count from a different field would be
    multiplying a quantity it does not describe.
    """
    described = str(_first(variation, _PACK_KEYS) or "")
    parsed = normalise.parse_pack_size(described)
    if parsed is not None:
        qty, unit = parsed
        multipack = _MULTIPACK_RE.search(described)
        if multipack:
            count = int(multipack.group(1))
            if count > 1:
                qty *= count
        return qty, unit
    return normalise.parse_pack_size(str(name))


def _agrees_with_declared_unit_price(variation: dict[str, Any], pack_qty: float,
                                     pack_unit: str, price: float) -> bool:
    """Cross-check our pack maths against the per-100 price SWIGGY publishes.

    Swiggy sends unitLevelPrice ("7.7/100 ml") alongside the pack, so our own
    arithmetic can be checked against the merchant's rather than trusted. This is
    the repo's independent-recomputation habit applied to a field mapping: the
    multipack bug above would have been caught here on the first run — a ₹924
    twelve-pack read as one litre computes ₹92.40/100 ml against a declared ₹7.70.

    A disagreement DROPS that variation and says so, rather than shipping a
    confident wrong price. If every variation disagrees, search() raises, because
    rows in and nothing out means the mapping is broken, not the shelf empty.

    Absent or unparseable unitLevelPrice means no opinion — passes. Tolerance is
    3% or 5 paise, whichever is larger: the published figure is rounded to one
    decimal (450 ml at ₹30 is 6.667 published as "6.7").
    """
    declared_text = str((variation.get("price") or {}).get("unitLevelPrice") or "")
    match = _UNIT_LEVEL_RE.search(declared_text)
    if not match:
        return True
    declared_per_100 = float(match.group(1))
    declared_kind = _DECLARED_BASE_KIND.get(match.group(2).lower())
    try:
        ours = normalise.normalise(pack_qty, pack_unit, price)
    except ValueError:
        return False
    if declared_kind and ours.base_kind != declared_kind:
        return True  # comparing different dimensions proves nothing
    if abs(ours.price_per_100_base - declared_per_100) <= max(0.05, 0.03 * declared_per_100):
        return True
    print(f"[swiggy] dropped {str(_first(variation, _NAME_KEYS))[:40]!r} "
          f"{pack_qty:g}{pack_unit}: we compute ₹{ours.price_per_100_base:.2f}/100 "
          f"but Swiggy declares ₹{declared_per_100:.2f}/100 — our pack maths and "
          "theirs disagree, so the price is not trustworthy.")
    return False


# ---- saved addresses ---------------------------------------------------

_ADDRESS_ID_KEYS = ("id", "addressId", "address_id")
_ADDRESS_LABEL_KEYS = ("label", "name", "annotation", "type")
_PINCODE_RE = re.compile(r"\b([1-9][0-9]{5})\b")


def _addresses_of(payload: Any) -> list[dict[str, Any]]:
    """Saved addresses, normalised to {id, label, raw}.

    get_addresses returns structuredContent — verified on a live call, though
    only ever with an EMPTY list, so the per-address field names below are still
    read from the tool's description rather than from data. Anything unrecognised
    yields no id and _resolve_address_id raises with the keys it saw, rather than
    shopping an address it could not identify.
    """
    rows: list[Any] = []
    if isinstance(payload, dict):
        for key in ("addresses", "savedAddresses", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    elif isinstance(payload, list):
        rows = payload
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": next((row[k] for k in _ADDRESS_ID_KEYS if row.get(k)), ""),
            "label": next((row[k] for k in _ADDRESS_LABEL_KEYS if row.get(k)), ""),
            "raw": row,
        })
    return out


def _warn_if_address_disagrees_with_location(address: dict[str, Any]) -> None:
    """Swiggy forces us to shop its saved address, while Blinkit is shopped at
    location.shopping_locus()'s coordinates and JioMart at its pincode. If those are
    different places, the cheapest-of-three answer is meaningless while looking
    perfectly healthy — so a visible pincode disagreement is announced.

    Deliberately a warning and not a refusal: the pincode may simply not appear
    in the payload, and refusing on a field we cannot reliably read would drop a
    working merchant over a parsing gap.
    """
    here = location.shopping_locus()
    found = set(_PINCODE_RE.findall(json.dumps(address.get("raw", {}))))
    if found and here.pincode and here.pincode not in found:
        print(f"[swiggy] WARNING: shopping Swiggy's saved address (pincode "
              f"{'/'.join(sorted(found))}) while the delivery location on file is "
              f"{here.pincode}. Prices are then not comparable across merchants — "
              f"fix one or the other.")


# ---- response mapping --------------------------------------------------

_NAME_KEYS = ("displayName", "display_name", "name", "productName",
              "product_name", "title", "item_name")
# offerPrice BEFORE mrp: mrp is the printed price, offerPrice is what is
# actually charged. Ranking merchants on mrp while paying offerPrice would
# compare two different numbers and pick the wrong store.
_PRICE_KEYS = ("offerPrice", "offer_price", "selling_price", "sellingPrice",
               "discountedPrice", "discounted_price", "finalPrice", "price", "mrp")
_SKU_KEYS = ("skuId", "spinId", "sku", "variation_id", "variationId", "id",
             "productId", "product_id", "item_id", "itemId")
_BRAND_KEYS = ("brandName", "brand_name", "brand")
_PACK_KEYS = ("quantityDescription", "quantity_description", "weight", "packSize",
              "pack_size", "quantity", "unit", "size", "variant", "display_unit")
_STOCK_KEYS = ("in_stock", "inStock", "available", "is_available", "inventory")


def _rows_of(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("products", "results", "items", "data", "widgets",
                    "productList", "product_list", "search_results"):
            value = payload.get(key)
            if isinstance(value, list):
                flat: list[dict[str, Any]] = []
                for item in value:
                    if isinstance(item, dict):
                        inner = item.get("products") or item.get("items") or item.get("data")
                        if isinstance(inner, list):
                            flat.extend(r for r in inner if isinstance(r, dict))
                        else:
                            flat.append(item)
                return flat
            if isinstance(value, dict):
                return _rows_of(value)
    return []


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _as_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("₹", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    if isinstance(value, dict):
        return _as_price(_first(value, _PRICE_KEYS))
    return None


def _listings_from_product(product: dict[str, Any]) -> list[Listing]:
    """ONE LISTING PER VARIATION, not per product — this is the whole fix.

    Swiggy's search_products returns products whose price and pack size live
    inside a `variations` array, never at the product level:

        {"displayName": "Amul Taaza Tetra", "brand": "Amul", "productId": "...",
         "variations": [{"spinId": "31VQRTD5DH", "skuId": "M2SBFF9LIR",
                         "quantityDescription": "200 ml",
                         "price": {"mrp": 17, "offerPrice": 17,
                                   "unitLevelPrice": "8.5/100 ml"},
                         "isInStockAndAvailable": true}, ...]}

    The first mapping here looked for a name and a price on the product itself,
    found no price, and — because it was called with strict=False — returned
    None for every row. Eleven real products became "0 listings from swiggy",
    which reads exactly like Swiggy having no milk. That is the failure shape
    this repo cares most about, and it is why search() now raises when rows go
    in and nothing comes out.

    A variation IS the purchasable thing, and different pack sizes are different
    prices, so each becomes its own Listing — the same granularity Blinkit and
    JioMart already produce, which is what makes per-100-unit comparison valid.
    """
    variations = product.get("variations")
    if not isinstance(variations, list) or not variations:
        return []

    product_name = _first(product, _NAME_KEYS)
    product_brand = _first(product, _BRAND_KEYS)
    out: list[Listing] = []
    for variation in variations:
        if not isinstance(variation, dict):
            continue
        listing = _listing_from_variation(variation, product_name, product_brand)
        if listing is not None:
            out.append(listing)
    return out


def _listing_from_variation(variation: dict[str, Any], product_name: Any,
                            product_brand: Any) -> Listing | None:
    name = _first(variation, _NAME_KEYS) or product_name
    price = _as_price(_first(variation, ("price",))) or _as_price(_first(variation, _PRICE_KEYS))
    if name is None or price is None:
        return None

    pack = _pack_of(variation, name)
    if pack is None:
        return None
    pack_qty, pack_unit = pack

    if not _agrees_with_declared_unit_price(variation, pack_qty, pack_unit, price):
        return None

    # Carting a Swiggy item needs BOTH spinId and skuId (its update_cart says
    # so). Nothing carts a real merchant yet, but dropping half the identifier
    # here would mean re-searching to recover it later, so keep both. Ordering
    # is fixed — spinId first — so a sku is stable across runs and comparable.
    spin_id = str(variation.get("spinId") or "")
    sku_id = str(variation.get("skuId") or "")
    sku = ":".join(p for p in (spin_id, sku_id) if p) or str(_first(variation, _SKU_KEYS) or "")

    stock = variation.get("isInStockAndAvailable")
    if stock is None:
        stock = _first(variation, _STOCK_KEYS)

    return Listing(
        merchant=MERCHANT_ID,
        sku=sku,
        name=str(name),
        brand=str(_first(variation, _BRAND_KEYS) or product_brand or ""),
        pack_qty=pack_qty,
        pack_unit=pack_unit,
        price_inr=price,
        in_stock=True if stock is None else bool(stock),
        category="",
        price_source=PRICE_SOURCE,
        asof=time.time(),
        # Swiggy sends badges and maxQuantityMessage as merchant-controlled
        # prose. None of it is carried: this field is never parsed for
        # instructions, and the cheapest way to honour that is to not have it.
        raw_description="",
    )


# ---- one-time connect --------------------------------------------------

def connect_interactively(*, open_browser: bool = True) -> dict[str, Any]:
    """Runs the OAuth dance once and stores a token.

    Same design as zepto_mcp.py: a human-run CLI, never something the worker
    or an agent initiates. Opens a URL and waits for the redirect; the user
    signs in with their Swiggy credentials.
    """
    import http.server
    import threading
    import urllib.parse
    import webbrowser

    received: dict[str, str] = {}
    done = threading.Event()

    class _Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.urlparse(self.path).query
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in received
            self.wfile.write(
                b"<h2>Swiggy Instamart connected. You can close this tab.</h2>" if ok
                else b"<h2>Authorization failed. Check the terminal.</h2>"
            )
            done.set()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    redirect_uri = f"http://localhost:{server.server_port}/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_meta = oauth.discover_auth_server(AUTH_BASE)
    reg_endpoint = auth_meta.get("registration_endpoint", f"{AUTH_BASE}/auth/register")
    registration = oauth.register_client(
        reg_endpoint,
        redirect_uri=redirect_uri,
        client_name="Gourab's grocery butler (read-only)",
    )
    token_endpoint = auth_meta.get("token_endpoint", f"{AUTH_BASE}/auth/token")
    authorization_endpoint = auth_meta.get("authorization_endpoint", f"{AUTH_BASE}/auth/authorize")
    pending = oauth.begin_authorization(
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        client_id=registration["client_id"],
        redirect_uri=redirect_uri,
        scope=REQUIRED_SCOPE,
    )

    print(f"\nRequesting scope: {REQUIRED_SCOPE}")
    print("Open this URL, sign in with your Swiggy account, and approve:\n")
    print(f"  {pending.authorize_url}\n")
    if open_browser:
        webbrowser.open(pending.authorize_url)

    if not done.wait(timeout=300):
        server.shutdown()
        raise oauth.OAuthError("timed out after 5 minutes waiting for the Swiggy redirect")
    server.shutdown()

    if "code" not in received:
        raise oauth.OAuthError(f"authorization failed: {received}")
    if received.get("state") != pending.state:
        raise oauth.OAuthError("state mismatch on the OAuth redirect — refusing to continue")

    response = oauth.exchange_code(pending, received["code"])
    token = _stored_from_response(
        response,
        client_id=registration["client_id"],
        token_endpoint=token_endpoint,
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
              "  python -m grocery.adapters.swiggy_mcp --connect          one-time OAuth\n"
              "  python -m grocery.adapters.swiggy_mcp --status           show the stored grant\n"
              "  python -m grocery.adapters.swiggy_mcp --tools            what the server offers\n"
              "  python -m grocery.adapters.swiggy_mcp --addresses        saved delivery addresses\n"
              "  python -m grocery.adapters.swiggy_mcp --probe <query>    dump ONE raw row\n"
              "  python -m grocery.adapters.swiggy_mcp <query>            live search")
        raise SystemExit(0)

    if args[0] == "--connect":
        print(_json.dumps(connect_interactively(), indent=2))
        raise SystemExit(0)

    if args[0] == "--status":
        print(_json.dumps(connection_status(), indent=2))
        raise SystemExit(0)

    adapter = SwiggyMCPAdapter()

    if args[0] == "--tools":
        report = adapter.assert_surface_is_read_only()
        print(_json.dumps(report, indent=2))
        print("\nread-only surface" if report["read_only"]
              else "\nWARNING: write-shaped tools are on offer")
        raise SystemExit(0)

    if args[0] == "--addresses":
        # search_products cannot run without one of these, so seeing them is
        # the first thing to do after connecting.
        raw = adapter._call("get_addresses", {})
        found = _addresses_of(raw)
        print(f"{len(found)} saved address(es)")
        for a in found:
            print(f"  id={a['id']}  label={a['label']}")
        if not found:
            print("\nnone — add one in the Swiggy app; search cannot run without it.")
            print("\nraw payload:")
            print(_json.dumps(raw, indent=2)[:2000])
        raise SystemExit(0)

    if args[0] == "--probe":
        query = " ".join(args[1:]) or "milk"
        raw = adapter._call("search_products", {"query": query, **adapter._delivery_args()})
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
    except (SwiggyNotConnected, SwiggyUnavailable) as exc:
        print(f"\nswiggy is unavailable:\n\n{exc}\n")
        raise SystemExit(2)
    except WriteToolRefused as exc:
        # Reachable for a real reason: _ALLOWED_TOOLS was written from Swiggy's
        # docs, never from a live tools/list. If the search tool is called
        # something else, this is what says so — run --tools to see the surface.
        print(f"\n{exc}\n\nRun `--tools` to see what this server actually offers.\n")
        raise SystemExit(2)
    print(f"{len(results)} listings from {MERCHANT_ID}\n")
    for item in results[:25]:
        stock = "" if item.in_stock else "  (out of stock)"
        print(f"  {item.name[:45]:<45} {item.pack_qty:>6g}{item.pack_unit:<4} "
              f"₹{item.price_inr:>7.2f}{stock}")
