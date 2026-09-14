"""
Mock storefront — 3 fake merchants with real-ish catalogues and prices.
Deterministic, testable, zero ToS risk. The substrate every adapter (and
therefore the whole Phase 0-1 system) is proven against before any real site
is touched.

Run: python -m mockstore.app   (serves on :8801)
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mockstore.catalog import CATALOG, MERCHANTS, search as catalog_search

app = FastAPI(title="mockstore", docs_url=None, redoc_url=None, openapi_url=None)

_CARTS: dict[str, dict[str, Any]] = {}
_ORDERS: dict[str, dict[str, Any]] = {}


class CartLineIn(BaseModel):
    sku: str
    qty: int = 1


class CartIn(BaseModel):
    merchant: str
    lines: list[CartLineIn]


def _find_listing(merchant: str, sku: str) -> dict[str, Any] | None:
    for item in CATALOG.values():
        listing = item["listings"].get(merchant)
        if listing and listing["sku"] == sku:
            return {**listing, "category": item["category"]}
    return None


@app.get("/health")
def health():
    return {"ok": True, "merchants": list(MERCHANTS.keys())}


@app.get("/merchants")
def merchants():
    return MERCHANTS


@app.get("/search")
def search(q: str, merchant: str | None = None):
    """Read-only. Returns every merchant's listing for each matching item so
    the caller can compare, unless `merchant` narrows it to one."""
    item_keys = catalog_search(q)
    out = []
    for key in item_keys:
        item = CATALOG[key]
        for m, listing in item["listings"].items():
            if merchant and m != merchant:
                continue
            out.append({
                "item_key": key, "merchant": m, "category": item["category"],
                **listing,
                "price_source": "mockstore_page", "asof": time.time(),
            })
    return {"query": q, "results": out}


@app.get("/item/{merchant}/{sku}")
def get_item(merchant: str, sku: str):
    listing = _find_listing(merchant, sku)
    if not listing:
        raise HTTPException(404, f"no such sku {sku} at {merchant}")
    return {**listing, "price_source": "mockstore_page", "asof": time.time()}


@app.post("/cart")
def build_cart(cart_in: CartIn):
    """Builds a cart and returns a fresh quote. Read+write, but no money
    moves here — this only ever produces a CartMandate candidate for the
    caller to hash and propose; mockstore itself never authorises anything."""
    if cart_in.merchant not in MERCHANTS:
        raise HTTPException(404, f"unknown merchant {cart_in.merchant}")

    lines = []
    subtotal = 0.0
    for line in cart_in.lines:
        listing = _find_listing(cart_in.merchant, line.sku)
        if not listing:
            raise HTTPException(404, f"no such sku {line.sku} at {cart_in.merchant}")
        if not listing["in_stock"]:
            raise HTTPException(409, f"{line.sku} is out of stock at {cart_in.merchant}")
        line_total = listing["price_inr"] * line.qty
        subtotal += line_total
        lines.append({**listing, "qty": line.qty, "line_total_inr": line_total,
                       "price_source": "mockstore_page", "asof": time.time()})

    m = MERCHANTS[cart_in.merchant]
    if subtotal < m["min_order_inr"]:
        raise HTTPException(
            422,
            f"subtotal ₹{subtotal:.2f} is below {m['name']}'s minimum order of ₹{m['min_order_inr']}",
        )
    delivery_fee = 0 if subtotal >= m["free_delivery_over_inr"] else m["delivery_fee_inr"]
    handling_fee = m["handling_fee_inr"]
    landed_total = subtotal + delivery_fee + handling_fee

    cart_id = f"mockcart_{uuid.uuid4().hex[:10]}"
    cart = {
        "cart_id": cart_id, "merchant": cart_in.merchant, "lines": lines,
        "subtotal_inr": subtotal, "delivery_fee_inr": delivery_fee,
        "handling_fee_inr": handling_fee, "landed_total_inr": landed_total,
        "created_at": time.time(),
    }
    _CARTS[cart_id] = cart
    return cart


@app.get("/cart/{cart_id}/quote")
def requote(cart_id: str):
    """Re-derive the cart's total from current catalogue prices. Used to
    detect price drift between cart-approval and payment-approval — see
    tests/e2e scenario 8."""
    cart = _CARTS.get(cart_id)
    if not cart:
        raise HTTPException(404, "no such cart")
    fresh_lines = []
    subtotal = 0.0
    for line in cart["lines"]:
        listing = _find_listing(cart["merchant"], line["sku"])
        if not listing or not listing["in_stock"]:
            raise HTTPException(409, f"{line['sku']} is no longer available")
        line_total = listing["price_inr"] * line["qty"]
        subtotal += line_total
        fresh_lines.append({**listing, "qty": line["qty"], "line_total_inr": line_total})
    m = MERCHANTS[cart["merchant"]]
    delivery_fee = 0 if subtotal >= m["free_delivery_over_inr"] else m["delivery_fee_inr"]
    landed_total = subtotal + delivery_fee + m["handling_fee_inr"]
    return {
        "cart_id": cart_id, "subtotal_inr": subtotal, "delivery_fee_inr": delivery_fee,
        "handling_fee_inr": m["handling_fee_inr"], "landed_total_inr": landed_total,
        "drifted": landed_total != cart["landed_total_inr"],
        "prior_landed_total_inr": cart["landed_total_inr"],
    }


class OrderIn(BaseModel):
    cart_id: str
    payment_ref: str  # opaque reference to a PaymentMandate id — mockstore never sees the mandate itself


@app.post("/order")
def place_order(order_in: OrderIn):
    """The only 'write money' endpoint mockstore exposes. Nothing upstream of
    this should ever call it without a PaymentMandate already minted by the
    broker — that discipline lives in console/rails/mock_rail.py, not here."""
    cart = _CARTS.get(order_in.cart_id)
    if not cart:
        raise HTTPException(404, "no such cart")
    order_id = f"mockorder_{uuid.uuid4().hex[:10]}"
    order = {
        "order_id": order_id, "cart_id": cart["cart_id"], "merchant": cart["merchant"],
        "landed_total_inr": cart["landed_total_inr"], "payment_ref": order_in.payment_ref,
        "placed_at": time.time(), "status": "confirmed",
    }
    _ORDERS[order_id] = order
    return order


@app.get("/order/{order_id}")
def get_order(order_id: str):
    order = _ORDERS.get(order_id)
    if not order:
        raise HTTPException(404, "no such order")
    return order


if __name__ == "__main__":
    import uvicorn
    from urllib.parse import urlparse
    from grocery import config
    port = urlparse(config.MOCKSTORE_URL).port or 8801
    uvicorn.run("mockstore.app:app", host="127.0.0.1", port=port, reload=False)
