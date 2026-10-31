"""
Adapter over mockstore/app.py — the reference implementation every other
adapter is validated against via tests/adapters/.
"""
from __future__ import annotations

import requests

from grocery import config
from grocery.adapters.base import StorefrontAdapter, Listing, CartLine, Quote


class MockAdapter(StorefrontAdapter):
    def __init__(self, merchant_id: str, base_url: str | None = None):
        self.merchant_id = merchant_id
        self.base_url = base_url or config.MOCKSTORE_URL

    def search(self, query: str) -> list[Listing]:
        r = requests.get(f"{self.base_url}/search", params={"q": query, "merchant": self.merchant_id}, timeout=10)
        r.raise_for_status()
        return [
            Listing(
                merchant=row["merchant"], sku=row["sku"], name=row["name"], brand=row["brand"],
                pack_qty=row["pack_qty"], pack_unit=row["pack_unit"], price_inr=row["price_inr"],
                in_stock=row["in_stock"],
            category=" ".join([row["category"], *row.get("query_terms", [])]),
                price_source=row["price_source"], asof=row["asof"],
                raw_description=row.get("description", ""),
            )
            for row in r.json()["results"]
        ]

    def get_item(self, sku: str) -> Listing:
        r = requests.get(f"{self.base_url}/item/{self.merchant_id}/{sku}", timeout=10)
        r.raise_for_status()
        row = r.json()
        return Listing(
            merchant=self.merchant_id, sku=row["sku"], name=row["name"], brand=row["brand"],
            pack_qty=row["pack_qty"], pack_unit=row["pack_unit"], price_inr=row["price_inr"],
            in_stock=row["in_stock"],
            category=" ".join([row["category"], *row.get("query_terms", [])]),
            price_source=row["price_source"], asof=row["asof"],
            raw_description=row.get("description", ""),
        )

    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        if not authorized:
            raise PermissionError("build_cart called without broker authorization")
        payload = {"merchant": self.merchant_id, "lines": [{"sku": l.sku, "qty": l.qty} for l in lines]}
        r = requests.post(f"{self.base_url}/cart", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()["cart_id"]

    def quote(self, cart_ref: str) -> Quote:
        r = requests.get(f"{self.base_url}/cart/{cart_ref}/quote", timeout=10)
        r.raise_for_status()
        row = r.json()
        return Quote(
            merchant=self.merchant_id, subtotal_inr=row["subtotal_inr"],
            delivery_fee_inr=row["delivery_fee_inr"], handling_fee_inr=row["handling_fee_inr"],
            coupon_discount_inr=0.0, landed_total_inr=row["landed_total_inr"],
        )

    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        if not authorized:
            raise PermissionError("place_order called without broker authorization")
        r = requests.post(f"{self.base_url}/order", json={"cart_id": cart_ref, "payment_ref": payment_ref}, timeout=10)
        r.raise_for_status()
        return r.json()["order_id"]


def all_mock_adapters() -> list[MockAdapter]:
    return [MockAdapter("freshmart"), MockAdapter("quickbasket"), MockAdapter("dailygrocer")]


def fetch_merchant_meta(base_url: str | None = None) -> dict:
    """Read-only: GET /merchants. Used by the worker to rank landed cost
    without ever calling a write endpoint."""
    import requests
    from grocery import config
    r = requests.get(f"{base_url or config.MOCKSTORE_URL}/merchants", timeout=10)
    r.raise_for_status()
    return r.json()
