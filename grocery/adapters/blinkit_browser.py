"""
Real, read-only Blinkit adapter — Phase 2 of the plan: search() and
get_item() against the live site via a headless browser, never logging in
and never carting. build_cart()/place_order() raise unconditionally, on
purpose; carting and payment stay on MockAdapter until Phase 3.

Blinkit gates its catalogue behind a delivery location, not a login — no
account is needed to see real prices. Inspecting the site as an ordinary
logged-out visitor showed that location lives in two plain cookies
(gr_1_lat / gr_1_lon — real coordinates, not a credential), so this adapter
sets them directly rather than driving the interactive location-autocomplete
picker on every call. Clicking through a dropdown that only ever needs the
same fixed point is exactly the kind of brittle automation the plan's
adapter-medic role exists to fix — avoided here from the start rather than
discovered as a flake later.

Card and page layout are Tailwind utility classes with no semantic
data-test-id, and WILL drift with any redesign. This deliberately parses
STRUCTURE and stable anchor TEXT ("Inclusive of all taxes", "Explore all
products", the page <title> template — e.g. "Amul Gold Full Cream Milk
Price - Buy Online at ₹72 in India") rather than specific class names, since
that's the more stable of the two — but it is still real-site scraping and
will still eventually break. That's exactly what
grocery.sentinel.checks.check_adapter_health is for: a merchant whose
lookups start coming back empty shows up there before it shows up as a
silently wrong price.

This module never bypasses a CAPTCHA or bot-detection wall — if one
appears, every method here raises AdapterBlocked rather than trying to get
past it (see the org-wide safety rules and CLAUDE.md).
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from grocery import config
from grocery.adapters.base import CartLine, Listing, Quote, StorefrontAdapter

MERCHANT_ID = "blinkit"
BASE_URL = "https://blinkit.com"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class AdapterBlocked(Exception):
    """Raised when the site shows a bot-detection page or CAPTCHA instead of
    real content. The correct response is to stop and say so — never to try
    to get past it."""


def _location_cookies() -> list[dict]:
    return [
        {"name": "gr_1_lat", "value": str(config.ADAPTER_LOCATION_LAT), "domain": "blinkit.com", "path": "/"},
        {"name": "gr_1_lon", "value": str(config.ADAPTER_LOCATION_LON), "domain": "blinkit.com", "path": "/"},
        {"name": "gr_1_locality", "value": config.ADAPTER_LOCATION_LABEL, "domain": "blinkit.com", "path": "/"},
    ]


def _new_context(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-IN")
    context.add_cookies(_location_cookies())
    return browser, context


def _check_not_blocked(page) -> None:
    title = (page.title() or "").lower()
    try:
        body_sample = page.inner_text("body")[:500].lower()
    except Exception:  # noqa: BLE001 — a page that won't even yield body text is itself suspicious
        body_sample = ""
    if "access denied" in title or "access denied" in body_sample or "captcha" in body_sample:
        raise AdapterBlocked(f"{MERCHANT_ID} showed a bot-detection page — refusing to try to get past it")


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)$")


def _parse_size(text: str) -> tuple[float, str] | None:
    m = _SIZE_RE.match(text.strip())
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower()


def _parse_card_text(card_id: str, text: str) -> Listing | None:
    """A search-result card's innerText is a fixed line sequence:
        ["N% OFF"]? , eta, name, size, price, [mrp]?, cta
    Returns None — rather than guessing — if the shape doesn't match. A
    dropped item is safer than a wrongly-parsed price (CLAUDE.md rule 3,
    applied to a scraped field instead of a catalogue one)."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines and lines[0].upper().endswith("% OFF"):
        lines = lines[1:]
    if len(lines) < 4:
        return None

    name = lines[1]  # lines[0] is the ETA ("9 MINS"), not used
    size = _parse_size(lines[2])
    if size is None:
        return None
    price_lines = [l for l in lines[3:] if l.startswith("₹")]
    if not price_lines:
        return None
    try:
        price = float(price_lines[0].replace("₹", "").replace(",", ""))
    except ValueError:
        return None

    cta = lines[-1].strip().upper()
    pack_qty, pack_unit = size
    return Listing(
        merchant=MERCHANT_ID, sku=card_id, name=name,
        # Blinkit's search cards don't expose a separate brand field (the
        # product page does) — leaving this blank rather than guessing from
        # the name is the same "unsourced is worse than invented" principle
        # CLAUDE.md rule 3 applies to prices.
        brand="",
        pack_qty=pack_qty, pack_unit=pack_unit, price_inr=price,
        in_stock=(cta == "ADD"),
        category="",
        price_source="blinkit_live", asof=time.time(),
    )


def _run_search(query: str) -> list[Listing]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, context = _new_context(p)
        try:
            page = context.new_page()
            page.goto(f"{BASE_URL}/s/?q={quote(query)}", wait_until="domcontentloaded", timeout=20000)
            _check_not_blocked(page)
            page.wait_for_selector('div[role="button"]', timeout=10000)
            page.wait_for_timeout(1200)  # let the async product-card fetch finish rendering
            cards = page.eval_on_selector_all(
                'div[role="button"][id]',
                "els => els.filter(e => /^\\d+$/.test(e.id)).map(e => ({id: e.id, text: e.innerText}))",
            )
        finally:
            browser.close()

    out = []
    for c in cards:
        listing = _parse_card_text(c["id"], c["text"])
        if listing:
            out.append(listing)
    return out


_TITLE_RE = re.compile(r"^(.*) Price - Buy Online at ₹[\d.,]+ in India$")

def _find_line(lines: list[str], needle: str) -> int | None:
    needle = needle.lower()
    for i, l in enumerate(lines):
        if needle in l.lower():
            return i
    return None


def _extract_size_and_price(lines: list[str], anchor_idx: int) -> tuple[float, str, float] | None:
    """Blinkit's PDP repeats the *selected* variant's size and price
    immediately before its "inclusive of all taxes" disclaimer — e.g.
    "...1 ltr\\n₹72\\nInclusive of all taxes...". Scanning backwards from
    that disclaimer by TEXT rather than by DOM-tree position is the point:
    a first attempt anchored on `.closest('div').parentElement` broke
    between two otherwise-identical page loads, because the element nesting
    around that line isn't stable even though the line ordering is — a
    small real lesson in why this adapter parses structure/text sequence,
    not DOM shape, wherever it can."""
    price = None
    size = None
    j = anchor_idx - 1
    while j >= 0 and (price is None or size is None):
        line = lines[j]
        if price is None and line.startswith("₹"):
            try:
                price = float(line.replace("₹", "").replace(",", ""))
            except ValueError:
                pass
        elif price is not None and size is None:
            parsed = _parse_size(line)
            if parsed:
                size = parsed
        j -= 1
    if price is None or size is None:
        return None
    return size[0], size[1], price


def _run_get_item(sku: str) -> Listing:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, context = _new_context(p)
        try:
            page = context.new_page()
            # The URL slug is cosmetic — Blinkit resolves the product purely
            # from the trailing prid, so a fixed placeholder works for any
            # sku without needing to know its real slug up front.
            page.goto(f"{BASE_URL}/prn/x/prid/{sku}", wait_until="domcontentloaded", timeout=20000)
            _check_not_blocked(page)
            page.wait_for_timeout(1200)

            title = page.title() or ""
            body_text = page.inner_text("body")
        finally:
            browser.close()

    m = _TITLE_RE.match(title)
    if not m:
        raise ValueError(f"blinkit product page for sku={sku} didn't match the expected title shape: {title!r}")
    name = m.group(1)

    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    tax_idx = _find_line(lines, "inclusive of all taxes")
    if tax_idx is None:
        raise ValueError(f"blinkit product page for sku={sku} didn't show a price disclaimer to anchor on")
    parsed = _extract_size_and_price(lines, tax_idx)
    if parsed is None:
        raise ValueError(f"blinkit product page for sku={sku}: couldn't find size+price before the tax disclaimer")
    pack_qty, pack_unit, price = parsed

    brand_idx = _find_line(lines, "explore all products")
    brand = lines[brand_idx - 1] if brand_idx is not None and brand_idx > 0 else ""

    return Listing(
        merchant=MERCHANT_ID, sku=sku, name=name, brand=brand,
        pack_qty=pack_qty, pack_unit=pack_unit, price_inr=price,
        # A page that resolves to a real product at all is treated as in
        # stock. Blinkit's out-of-stock PDP shape hasn't been observed yet —
        # flagged here rather than guessed at, per the same rule as above.
        in_stock=True,
        category="", price_source="blinkit_live", asof=time.time(),
    )


class BlinkitBrowserAdapter(StorefrontAdapter):
    """Phase 2: search() and get_item() hit the real site, read-only.
    build_cart()/place_order() are Phase 3+ work and raise unconditionally
    here — this adapter has never been wired to anything that could check
    `authorized`, and must not be trusted to enforce it itself."""

    merchant_id = MERCHANT_ID

    def search(self, query: str) -> list[Listing]:
        return _run_search(query)

    def get_item(self, sku: str) -> Listing:
        return _run_get_item(sku)

    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        raise NotImplementedError("blinkit_browser is read-only (Phase 2) — carting is Phase 3+ work")

    def quote(self, cart_ref: str) -> Quote:
        raise NotImplementedError("blinkit_browser is read-only (Phase 2) — quoting a real cart is Phase 3+ work")

    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        raise NotImplementedError("blinkit_browser is read-only (Phase 2) — ordering is Phase 3+ work")


if __name__ == "__main__":
    # A quick, tangible demonstration that Phase 2 actually works, without
    # touching the worker/console order flow at all:
    #   python -m grocery.adapters.blinkit_browser milk
    import sys
    query = " ".join(sys.argv[1:]) or "milk"
    print(f"searching blinkit.com for {query!r} (location: {config.ADAPTER_LOCATION_LABEL})...\n")
    for listing in BlinkitBrowserAdapter().search(query):
        stock = "in stock" if listing.in_stock else "OUT OF STOCK"
        print(f"  {listing.name:<45} {listing.pack_qty:>6g}{listing.pack_unit:<4} "
              f"₹{listing.price_inr:>7.2f}  [{stock}]  sku={listing.sku}")
