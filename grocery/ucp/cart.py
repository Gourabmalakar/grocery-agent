"""Building a real cart through UCP — the Universal Commerce Protocol.

WHY UCP AND NOT THE MERCHANT'S OWN MCP TOOLS. Zepto exposes both. The MCP tool
surface mixes `update_cart` in with `create_order`, `create_wallet_order` and
`create_upi_reserve_pay_order`, so an adapter that can cart there is one
allowlist mistake away from an adapter that can buy. The UCP surface is
`search_catalog / create_cart / update_cart / get_cart / cancel_cart` and
NOTHING ELSE — there is no checkout tool and no order tool on it at all.

That turns a policy ("we promise not to call the ordering tools") into a fact
about the surface ("there are no ordering tools here"), which is the kind of
safety this repo prefers. assert_cannot_order() checks it at connect time
rather than trusting the docs, because a tool Zepto adds next month would
otherwise arrive unnoticed.

UCP is also an open standard — Google, Shopify, Stripe, Flipkart and 60+ others
— so the next merchant that implements it needs configuration here, not a new
adapter. That is the whole reason to prefer it over Swiggy's bespoke tools.

CARTS ARE REVERSIBLE, and that is load-bearing for how this is used. A cart is
built to discover the TRUE total (delivery, handling and GST are unreadable
until something is in a basket), shown to a human, and cancelled if they say
no. cancel_cart is not a nicety; it is what makes building one on their behalf
a reasonable thing to do without asking first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grocery.mcp.client import McpClient, payload_of

# Anything on this surface whose name reads like buying rather than basketing.
# UCP's cart capability should contain none of these; if one appears, the
# premise above has changed and we want to know loudly.
_ORDERING_SHAPED = ("checkout", "order", "pay", "purchase", "confirm")

_CART_TOOLS = frozenset({"search_catalog", "get_product", "lookup_catalog",
                         "create_cart", "update_cart", "get_cart", "cancel_cart",
                         "search_locations", "lookup_locations"})


class UcpSurfaceChanged(RuntimeError):
    """The UCP endpoint is offering something it did not offer before, and the
    'this surface cannot buy' guarantee no longer holds on its own."""


class UcpToolRefused(PermissionError):
    pass


@dataclass(frozen=True)
class CartTotals:
    """What the merchant says the basket costs, in rupees.

    `subtotal`, `fees` and `taxes` are reported separately when the merchant
    breaks them out, but `total` is ALWAYS the merchant's own number and is the
    only one anything is allowed to pay against. Our components are a check on
    theirs, never a substitute — see console/approval.py's drift refusal for
    the same discipline applied to price.
    """
    total_inr: float
    subtotal_inr: float = 0.0
    fees_inr: float = 0.0
    taxes_inr: float = 0.0
    currency: str = "INR"
    continue_url: str = ""


def money(value: Any) -> float:
    """UCP money is {"amount": 8200, "currency": "INR"} — minor units, with the
    currency stated. Unlike Zepto's bare MCP integers there is nothing to guess
    here, which is one more reason to prefer this surface."""
    if isinstance(value, dict) and "amount" in value:
        return float(value["amount"]) / 100.0
    if isinstance(value, (int, float)):
        return float(value) / 100.0
    return 0.0


class UcpCartClient:
    """A cart session at one UCP merchant.

    CONSTRUCTED ONLY INSIDE console/. The worker must never hold this — it can
    write to a real merchant account, and worker/ is the process that is
    allowed to be wrong. tests/adversarial asserts the import boundary.
    """

    def __init__(self, endpoint: str, *, token_provider, agent_profile: str = "") -> None:
        self._endpoint = endpoint
        self._client = McpClient(endpoint, client_name="grocery-agent",
                                 token_provider=token_provider)
        self._agent_profile = agent_profile
        self._connected = False

    def connect(self) -> dict[str, Any]:
        info = self._client.connect()
        self._connected = True
        return info

    def _meta(self) -> dict[str, Any]:
        if not self._agent_profile:
            return {}
        return {"meta": {"ucp-agent": {"profile": self._agent_profile}}}

    def assert_cannot_order(self) -> dict[str, Any]:
        """Verify at runtime that this surface has no way to buy anything.

        Checked rather than assumed: the safety argument for using UCP is that
        the ordering tools are absent, and an absence is only worth something
        if somebody looks.
        """
        if not self._connected:
            self.connect()
        offered = [t.get("name", "") for t in self._client.list_tools()]
        ordering = [n for n in offered
                    if any(marker in n.lower() for marker in _ORDERING_SHAPED)]
        unknown = [n for n in offered if n not in _CART_TOOLS]
        return {"offered": offered, "ordering_shaped": ordering, "unknown": unknown,
                "cannot_order": not ordering}

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in _CART_TOOLS:
            raise UcpToolRefused(
                f"{tool!r} is not one of this client's cart tools ({sorted(_CART_TOOLS)}). "
                "This client builds baskets; it does not buy them."
            )
        if not self._connected:
            self.connect()
        return payload_of(self._client.call_tool(tool, {**arguments, **self._meta()}))

    # ---- catalogue -----------------------------------------------------

    def search(self, query: str, *, address_id: str) -> list[dict[str, Any]]:
        """Variants, flattened — a variant is the purchasable thing, same as
        everywhere else in this repo."""
        payload = self._call("search_catalog", {
            "query": query,
            "catalog": {"query": query,
                        # Zepto's own response warned: "Send
                        # context.buyer_address_location on every catalog and
                        # cart call to choose explicitly." Without it the server
                        # assumes "home", which is right by luck rather than by
                        # instruction — and would be wrong the day a second
                        # address exists.
                        "context": {"buyer_address_location": address_id}},
        })
        out: list[dict[str, Any]] = []
        for product in (payload or {}).get("products", []):
            for variant in product.get("variants", []) or []:
                out.append({
                    "id": variant.get("id", ""),
                    "title": variant.get("title") or product.get("title", ""),
                    "price_inr": money(variant.get("price")
                                       or (variant.get("prices") or [{}])[0].get("price")),
                    "product_title": product.get("title", ""),
                })
        return out

    # ---- the cart ------------------------------------------------------

    def create(self, line_items: list[tuple[str, int]], *, address_id: str) -> dict[str, Any]:
        return self._call("create_cart", {
            "line_items": [{"item": {"id": vid}, "quantity": qty} for vid, qty in line_items],
            "cart": {"context": {"buyer_address_location": address_id}},
        })

    def fetch(self, cart_id: str) -> dict[str, Any]:
        return self._call("get_cart", {"id": cart_id})

    def cancel(self, cart_id: str) -> Any:
        """Always safe to call, and called whenever a human declines. A cart we
        built and did not cancel is clutter in somebody's real account."""
        return self._call("cancel_cart", {"id": cart_id})


# UCP returns totals as a LIST of typed rows, not a mapping:
#   "totals": [{"type": "subtotal", "display_text": "Subtotal", "amount": 2200},
#              {"type": "total",    "display_text": "Total",    "amount": 2200}]
# Observed verbatim from Zepto on 2026-10-11. Reading it as a dict silently
# returned 0.00 for every component while the total happened to parse, which is
# the shape of bug that shows a plausible number and is wrong.
_TOTAL_TYPES = ("total", "grand_total", "payable", "order_total")
_SUBTOTAL_TYPES = ("subtotal", "items_total", "item_total")
_FEE_TYPES = ("delivery", "delivery_fee", "shipping", "handling", "fee", "fees",
              "service", "surge", "small_cart")
_TAX_TYPES = ("tax", "taxes", "gst", "vat")


def totals_of(cart: dict[str, Any]) -> CartTotals:
    """Read the merchant's own totals off a UCP cart response.

    WHAT A CART-STAGE TOTAL DOES AND DOES NOT CONTAIN — measured, not assumed.
    Zepto's cart returns `subtotal` and `total` and nothing else: no delivery,
    no handling, no GST. Those arrive at CHECKOUT, and Zepto has not implemented
    the UCP checkout capability (its discovery document lists catalog, cart,
    identity_linking and location only). So building a cart does NOT, on its
    own, make the landed total knowable — `fees_known` has to stay false until a
    merchant gives us a checkout-stage number.
    """
    body = cart.get("cart", cart) if isinstance(cart, dict) else {}
    rows = body.get("totals") or []
    if isinstance(rows, dict):           # tolerate a mapping if one ever appears
        rows = [{"type": k, "amount": v} for k, v in rows.items()]

    def sum_of(*types: str) -> float:
        return sum(money(r.get("amount")) for r in rows
                   if isinstance(r, dict) and str(r.get("type", "")).lower() in types)

    return CartTotals(
        total_inr=sum_of(*_TOTAL_TYPES),
        subtotal_inr=sum_of(*_SUBTOTAL_TYPES),
        fees_inr=sum_of(*_FEE_TYPES),
        taxes_inr=sum_of(*_TAX_TYPES),
        currency=str(body.get("currency") or "INR"),
        continue_url=str(body.get("continue_url") or cart.get("continue_url") or ""),
    )
