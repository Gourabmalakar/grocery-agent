"""
The adapter contract every storefront implements identically — mock, browser,
and (later) Swiggy MCP are interchangeable behind this interface. A new
adapter is "working" when it passes tests/adapters/ against this base, which
is what keeps adapter correctness from being re-argued per site.

Read methods (search, get_item, quote) are always available. Write methods
(build_cart, place_order) must raise PermissionError unless the broker has
authorised the call — enforced by requiring an `authorized: bool` flag be
explicitly threaded through, rather than trusting the caller's intentions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Listing:
    merchant: str
    sku: str
    name: str
    brand: str
    pack_qty: float
    pack_unit: str
    price_inr: float
    in_stock: bool
    category: str
    price_source: str
    asof: float
    raw_description: str = ""  # untrusted text — never parsed for instructions, see injection-defense skill


@dataclass
class CartLine:
    sku: str
    qty: int
    name: str = ""
    brand: str = ""
    pack_qty: float = 0.0
    pack_unit: str = ""
    unit_price_inr: float = 0.0
    line_total_inr: float = 0.0
    price_source: str = ""
    asof: float = 0.0
    intent_ref: str = ""  # points at intent_id or amendment_id — the lineage check
    # Only sku+qty are load-bearing for adapters/mock.py's build_cart, which
    # re-derives price/name/etc from the merchant's own current listing —
    # the other fields exist for the CartMandate/decision-log side of the
    # world, where lineage and provenance need to be recorded per line.


@dataclass
class Quote:
    merchant: str
    subtotal_inr: float
    delivery_fee_inr: float
    handling_fee_inr: float
    coupon_discount_inr: float
    landed_total_inr: float
    below_min_order: bool = False
    min_order_shortfall_inr: float = 0.0


class StorefrontAdapter(ABC):
    merchant_id: str

    @abstractmethod
    def search(self, query: str) -> list[Listing]: ...

    @abstractmethod
    def get_item(self, sku: str) -> Listing: ...

    @abstractmethod
    def build_cart(self, lines: list[CartLine], *, authorized: bool) -> str:
        """Returns an opaque cart_ref the adapter can quote/place against.
        Must raise PermissionError if authorized is not True."""
        ...

    @abstractmethod
    def quote(self, cart_ref: str) -> Quote: ...

    @abstractmethod
    def place_order(self, cart_ref: str, payment_ref: str, *, authorized: bool) -> str:
        """Returns an order_id. Must raise PermissionError if authorized is
        not True — this is the last line of the write-gate before real money
        or a real merchant order is touched."""
        ...
