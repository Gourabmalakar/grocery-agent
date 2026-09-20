"""
Real, read-only JioMart adapter over JioMart's own storefront JSON API.

NOT A BROWSER ADAPTER, AND THE NAME SAYS SO. jiomart.com is a Fynd Platform
storefront, so its catalogue is served by a documented-shape JSON API at
/api/service/application/catalog/v1.0/products/ rather than rendered into
HTML. That makes this adapter strictly better than blinkit_browser.py on
every axis that matters here: no Chromium to launch (milliseconds, not
seconds), no Tailwind class names or line ordering to parse, explicit
pack-size fields instead of a regex over a product title, and no page
structure that can drift under a redesign. Where a JSON API exists, use it.

THE LOCATION GATE IS A HEADER, NOT A LOGIN. The catalogue is pincode-gated:
without one the API answers 400 {"success":false,"message":"invalid
pincode"}. Verified by hand that the gate is satisfied by a single header —

    x-location-detail: {"pincode":"400001","country":"India"}

— with no account, no cookie and no session. (An `x-pincode` header also
works from inside a browser that already holds JioMart's location cookies,
but NOT from a bare client; `x-location-detail` is the one that stands
alone, so it is the one used here. That distinction cost a confusing 400
and is recorded so nobody re-derives it.)

A pincode is a real public postcode, not a credential — the same reasoning
as blinkit_browser.py's lat/lon cookies.

READ-ONLY, PERMANENTLY, AT THIS LAYER. build_cart/quote/place_order raise
unconditionally, including when authorized=True. Carting and payment stay on
MockAdapter until Phase 3.

If JioMart ever answers with a bot-detection page instead of JSON, this
raises AdapterBlocked and stops. It never tries to get past one.
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

from grocery import location
from grocery.adapters.base import CartLine, Listing, Quote, StorefrontAdapter
from grocery.compare import normalise

MERCHANT_ID = "jiomart"
BASE_URL = "https://www.jiomart.com"
_PRODUCTS_PATH = "/api/service/application/catalog/v1.0/products/"
PRICE_SOURCE = "jiomart_api"

# JioMart's own first-party / quick-commerce inventory. Derived by comparing
# responses across pincodes (see _assert_serviceable): a serviceable pincode
# returns seller_id 1 backed by many local store_ids, an unserviceable one
# returns only third-party marketplace sellers with a single store each.
_FIRST_PARTY_SELLER_ID = 1

_TIMEOUT_SECONDS = 30
_DEFAULT_PAGE_SIZE = 24
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class AdapterBlocked(Exception):
    """The site answered with a bot wall or a login page instead of data.
    Stop and say so — never try to get past it."""


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Referer": f"{BASE_URL}/",
        "x-location-detail": json.dumps(
            {"pincode": location.current().pincode, "country": "India"},
            separators=(",", ":"),
        ),
    }


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(
            BASE_URL + path, params=params, headers=_headers(), timeout=_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise AdapterBlocked(f"could not reach jiomart: {exc}") from exc

    if response.status_code == 400 and "invalid pincode" in response.text:
        raise AdapterBlocked(
            f"jiomart rejected pincode {location.current().pincode!r}. "
            "Set a serviceable delivery pincode on the Console's Control page."
        )
    if response.status_code in (401, 403):
        raise AdapterBlocked(
            f"jiomart returned HTTP {response.status_code} — this looks like a bot wall "
            "or an auth requirement. Stopping rather than working around it."
        )
    if response.status_code != 200:
        raise AdapterBlocked(f"jiomart returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise AdapterBlocked("jiomart returned a non-JSON body where the catalogue should be") from exc


def _price_of(item: dict[str, Any]) -> float | None:
    """The effective (post-discount) price actually charged.

    Fynd quotes a min/max band because one product can span several sizes.
    We take `min` and let pack-size normalisation do the comparing — taking
    the midpoint of a band would invent a price nothing is sold at.
    """
    effective = (item.get("price") or {}).get("effective") or {}
    value = effective.get("min", effective.get("max"))
    return float(value) if isinstance(value, (int, float)) else None


def _pack_of(item: dict[str, Any], name: str) -> tuple[float, str]:
    """Prefers JioMart's own structured quantity fields over parsing the
    title. The title parse stays as a fallback because `net-quantity-*` is
    absent on some items, but a declared quantity beats an inferred one."""
    qty, unit = item.get("net-quantity-value"), item.get("net-quantity-unit")
    if isinstance(qty, (int, float)) and qty > 0 and isinstance(unit, str) and unit.strip():
        return float(qty), unit.strip().lower()
    parsed = normalise.parse_pack_size(name)
    if parsed:
        return parsed
    return 1.0, "pcs"


def _listing_from(item: dict[str, Any]) -> Listing | None:
    name = item.get("name")
    price = _price_of(item)
    if not name or price is None:
        # No price, no listing. A row we cannot price is dropped rather than
        # carried with a zero — an unsourced price is worse than a missing one.
        return None
    pack_qty, pack_unit = _pack_of(item, str(name))
    categories = item.get("categories") or []
    return Listing(
        merchant=MERCHANT_ID,
        sku=str(item.get("uid") or item.get("sku_code") or ""),
        name=str(name),
        brand=str((item.get("brand") or {}).get("name") or ""),
        pack_qty=pack_qty,
        pack_unit=pack_unit,
        price_inr=price,
        in_stock=bool(item.get("sellable", True)),
        category=str(categories[0].get("name", "")) if categories else "",
        price_source=PRICE_SOURCE,
        asof=time.time(),
        # Merchant-controlled prose is untrusted (CLAUDE.md rule 2) and
        # nothing downstream needs it, so it is never carried.
        raw_description="",
    )


_SERVICEABILITY_PROBE = "milk"


class NotServiceable(AdapterBlocked):
    """The pincode is structurally valid and JioMart answered 200 — but with
    its NATIONAL FALLBACK catalogue rather than local quick-commerce stock."""


def _has_first_party_stock(payload: dict[str, Any]) -> bool:
    return any(item.get("seller_id") == _FIRST_PARTY_SELLER_ID
               for item in payload.get("items", []))


def _assert_serviceable(pincode: str) -> None:
    """The most dangerous thing this API does is succeed unhelpfully.

    An unserviceable-but-well-formed pincode does NOT return an error. It
    returns HTTP 200 with a full page of products — just from JioMart's
    national marketplace rather than a local dark store. Measured directly:

        560001  item_total 329   seller_id 1       many local store_ids
        999999  item_total  63   seller_id 22305   one store, third-party
        111111  item_total  63   ...identical to 999999...

    Both look like a healthy catalogue. Only one can be delivered in minutes.
    Without this check the Butler would compare Blinkit's 10-minute Amul Gold
    against a nationally-shipped chocolate gift combo, quote both with a real
    price_source and a real asof, and be entirely wrong while passing every
    mechanical check in the repo.

    (This cost a real confusion: 110001 was read as "Delhi has no fresh milk"
    when it was actually this fallback all along.)

    JioMart's own serviceability endpoint would be the authoritative answer,
    but it requires a session token the app bootstraps and a bare client
    cannot get. So this infers from the one structural signal that is visible
    and stable: whether any first-party inventory is on offer. It is an
    inference, and it is named as one — it can false-negative on a query so
    niche that no first-party seller stocks it, which is why it probes a
    staple rather than the user's actual query.
    """
    payload = _get(_PRODUCTS_PATH, {"q": _SERVICEABILITY_PROBE, "page_size": 12})
    if not _has_first_party_stock(payload):
        raise NotServiceable(
            f"jiomart does not appear to deliver to {pincode}: a staple search returned "
            "only third-party marketplace listings, which is what its national fallback "
            "catalogue looks like. Those ship in days and are not comparable with "
            "10-minute delivery, so this merchant is left out rather than quoted."
        )


class JioMartApiAdapter(StorefrontAdapter):
    merchant_id = MERCHANT_ID

    def __init__(self, *, page_size: int = _DEFAULT_PAGE_SIZE) -> None:
        self._page_size = page_size
        self._serviceable_for: str | None = None

    def _ensure_serviceable(self) -> None:
        """Probed once per adapter instance per pincode — an order searches
        several queries and re-probing each time would triple the traffic for
        an answer that cannot change mid-order."""
        pincode = location.current().pincode
        if self._serviceable_for == pincode:
            return
        _assert_serviceable(pincode)
        self._serviceable_for = pincode

    def search(self, query: str) -> list[Listing]:
        self._ensure_serviceable()
        payload = _get(_PRODUCTS_PATH, {"q": query, "page_size": self._page_size})
        listings = []
        for item in payload.get("items", []):
            listing = _listing_from(item)
            if listing is not None:
                listings.append(listing)
        return listings

    def get_item(self, sku: str) -> Listing:
        """Fynd's product-detail route is keyed by slug, not uid, and we only
        ever hold a uid — so this re-finds the item through search rather
        than inventing a URL. Slower, but it cannot silently return the
        wrong product, which is the failure that matters."""
        payload = _get(_PRODUCTS_PATH, {"q": sku, "page_size": self._page_size})
        for item in payload.get("items", []):
            if str(item.get("uid")) == str(sku):
                listing = _listing_from(item)
                if listing is not None:
                    return listing
        raise LookupError(f"jiomart has no priced listing for sku {sku!r}")

    # ---- write methods: closed, permanently, at this layer -------------

    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        raise NotImplementedError("jiomart_api is read-only (Phase 2) — carting is Phase 3+ work")

    def quote(self, cart_ref: str) -> Quote:
        raise NotImplementedError("jiomart_api is read-only (Phase 2) — quoting a real cart is Phase 3+ work")

    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        raise NotImplementedError("jiomart_api is read-only (Phase 2) — ordering is Phase 3+ work")


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "milk"
    results = JioMartApiAdapter().search(query)
    print(f"{len(results)} listings from {MERCHANT_ID} "
          f"(pincode {location.current().pincode})\n")
    for item in results[:25]:
        stock = "" if item.in_stock else "  (out of stock)"
        print(f"  {item.name[:45]:<45} {item.pack_qty:>7g}{item.pack_unit:<5} "
              f"₹{item.price_inr:>8.2f}{stock}")
