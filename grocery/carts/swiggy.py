"""Building a real Swiggy Instamart basket.

WHY THIS IS A SEPARATE FILE FROM grocery/ucp/cart.py, AND WHY IT IS THE MORE
DANGEROUS OF THE TWO.

Zepto's UCP endpoint is safe by ABSENCE: it offers nine tools and not one of
them can buy anything, which `UcpCartClient.assert_cannot_order()` verifies by
looking. Swiggy is not like that. A live `tools/list` on 2026-10-12 returned
sixteen tools, and `checkout` sits directly beside `update_cart`:

    check_payment_status, checkout, clear_cart, confirm_order, create_address,
    delete_address, get_addresses, get_cart, get_delivery_status, get_orders,
    get_payment_options, report_error, search_products, track_order,
    update_cart, your_go_to_items

`checkout` is described by Swiggy as "Place and confirm Swiggy Instamart
grocery order. Creates order and confirms payment in a single operation." One
tool call, on a token this repo already holds, and money has moved. There is no
read-only scope to hide behind either — Swiggy issues `mcp:tools`, read+write,
so `oauth.store()`'s ScopeTooBroad check does nothing here by construction.

So the guarantee on this surface is NOT absence, it is this client's own
positive allowlist, and that makes `_ALLOWED` load-bearing in a way the UCP
equivalent is not. `safety_report()` therefore reports a different fact than
Zepto's does: not "the surface cannot buy" — it plainly can — but "this client
will not call the things that buy". Do not weaken _ALLOWED, and do not add a
convenience method that reaches past _call().

UNITS. Swiggy quotes RUPEES ({"mrp": 17, "offerPrice": 17} for a 200ml tetra
pack); Zepto quotes PAISE (3400 for a ₹34 pack). This is why `rupees()` below
exists instead of importing `money()` from grocery/ucp/cart.py — that helper
divides by 100 and would have turned every Swiggy total into a hundredth of
itself. The ₹3,400-for-250ml bug was this mistake in the other direction.

UPDATE_CART REPLACES THE WHOLE CART. Swiggy's own description: "Update the cart
with items. Replaces the entire cart." There is no add-one-item call. So
building a basket here DESTROYS whatever was in the cart already, which may be
a real shopping list somebody assembled by hand. `build()` reads the cart first
and refuses rather than silently overwriting it.

THE BILL BREAKDOWN IS THE PRIZE. `get_cart` returns "cart with all items and
bill breakdown", meaning delivery and GST are READABLE here. Zepto's cart stage
has neither (it has not implemented UCP's checkout capability), which is why
every Zepto proposal has to admit `fees_known=false`. Swiggy can give a true
landed total.

ITS SHAPE IS PINNED, against the live account and against a screenshot of the
same cart in the app — both said ₹451. See tests/adapters/test_swiggy_bill_shape.py
for the verbatim payload, and the comment above _TOTAL_KEYS for the three
things the first, GUESSED mapping got wrong. Every one of them would have
refused every build, silently and for a plausible-sounding reason. If the shape
changes, `totals_of()` reports `found=False` and `build()` refuses rather than
inventing a number — re-pin it with

    python -m grocery.carts.swiggy --probe

THE CART IS THE APP'S CART. Measured, not assumed: `get_cart` returned the two
items, the ₹448 item total, the ₹3.01 handling fee and the ₹451 "To Pay" that
were on Gourab's phone at that moment, down to the `cartId`. So a basket built
here appears in the app, and — the reason the refusal in build() matters — a
basket already in the app is destroyed by writing over it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grocery.mcp.client import McpClient, payload_of

MERCHANT_ID = "swiggy"
MCP_ENDPOINT = "https://mcp.swiggy.com/im"

# POSITIVE ALLOWLIST. Everything else on the surface is refused, including the
# four tools that can spend money (checkout, confirm_order, check_payment_status,
# get_payment_options) and the three that mutate the address book (create_address,
# delete_address, update_drop_zone) — a cart builder has no business editing
# where things get delivered.
_ALLOWED = frozenset({"search_products", "get_addresses", "get_cart",
                      "update_cart", "clear_cart"})

# Belt to the allowlist's braces, same pattern as the read adapter: anything
# whose NAME reads like buying is refused even if it somehow entered _ALLOWED.
_ORDERING_SHAPED = ("checkout", "confirm", "order", "pay", "payment",
                    "purchase", "track")


class SwiggyToolRefused(PermissionError):
    """This client builds baskets; it does not buy them."""


class SwiggyCartError(RuntimeError):
    pass


@dataclass
class SwiggyTotals:
    total_inr: float
    subtotal_inr: float = 0.0
    fees_inr: float = 0.0
    taxes_inr: float = 0.0
    currency: str = "INR"
    found: bool = False
    raw_rows: tuple[tuple[str, float], ...] = ()


def rupees(value: Any) -> float:
    """Swiggy money is a bare number of RUPEES. Never divide by 100 here —
    see the UNITS note in the module docstring."""
    if isinstance(value, dict):
        for key in ("amount", "value", "total", "offerPrice", "price"):
            if key in value:
                return rupees(value[key])
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("₹", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


class SwiggyCartClient:
    """A cart session at Swiggy Instamart.

    CONSTRUCTED ONLY INSIDE console/. It can write to a real Swiggy account and
    the surface it talks to can place orders; worker/ is the process this
    architecture allows to be wrong, and an AST test keeps this out of there.
    """

    def __init__(self, *, token_provider, endpoint: str = MCP_ENDPOINT) -> None:
        self._endpoint = endpoint
        self._client = McpClient(endpoint, client_name="grocery-agent",
                                 token_provider=token_provider)
        self._connected = False

    def connect(self) -> dict[str, Any]:
        info = self._client.connect()
        self._connected = True
        return info

    def safety_report(self) -> dict[str, Any]:
        """What this surface offers, and what this client will touch.

        Deliberately NOT called `assert_cannot_order` — the Zepto equivalent
        asserts the surface has no ordering tool, and claiming that here would
        be a lie. Swiggy's `checkout` exists. The honest statement is that it
        is not reachable through _call().
        """
        if not self._connected:
            self.connect()
        offered = sorted(t.get("name", "") for t in self._client.list_tools())
        ordering = [n for n in offered
                    if any(m in n.lower() for m in _ORDERING_SHAPED)]
        return {
            "offered": offered,
            "ordering_shaped": ordering,
            "reachable": sorted(n for n in offered if n in _ALLOWED),
            # The surface CAN order. This client cannot reach the tools that do.
            "surface_can_order": bool(ordering),
            "client_can_order": False,
        }

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in _ALLOWED:
            raise SwiggyToolRefused(
                f"{tool!r} is not one of this client's cart tools "
                f"({sorted(_ALLOWED)}). This client builds baskets; it does "
                f"not buy them."
            )
        if any(m in tool.lower() for m in _ORDERING_SHAPED):
            raise SwiggyToolRefused(
                f"{tool!r} reads like an ordering tool and is refused outright, "
                f"allowlist or not"
            )
        if not self._connected:
            self.connect()
        return payload_of(self._client.call_tool(tool, arguments))

    # ---- addresses -----------------------------------------------------

    def addresses(self) -> list[dict[str, Any]]:
        payload = self._call("get_addresses", {}) or {}
        rows = payload.get("addresses") or []
        return [r for r in rows if isinstance(r, dict) and r.get("id")]

    # ---- the cart ------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        """The cart as Swiggy sees it. Takes no arguments — the cart is bound
        to the account, not addressed by id."""
        return self._call("get_cart", {}) or {}

    def replace(self, line_items: list[tuple[str, str, int]], *,
                address_id: str) -> dict[str, Any]:
        """Put exactly these items in the cart, replacing what was there.

        `line_items` is (spin_id, sku_id, quantity). spin_id is required;
        sku_id may be empty and is omitted when it is, because Swiggy's schema
        marks it optional and sending "" is not the same as sending nothing.
        """
        items: list[dict[str, Any]] = []
        for spin_id, sku_id, quantity in line_items:
            item: dict[str, Any] = {"spinId": spin_id, "quantity": int(quantity)}
            if sku_id:
                item["skuId"] = sku_id
            items.append(item)
        return self._call("update_cart", {
            "selectedAddressId": address_id, "items": items,
        }) or {}

    def clear(self) -> Any:
        """Take back a cart nobody wants. Safe to call when already empty."""
        return self._call("clear_cart", {})


# MEASURED 2026-10-12 against the live account, and cross-checked against a
# screenshot of the same cart in the Swiggy app (both said ₹451):
#
#   "billBreakdown": {
#     "lineItems": [{"label": "Item Total",           "value": "₹448.00"},
#                   {"label": "Handling Fee",         "value": "₹3.01"},
#                   {"label": "Delivery Partner Tip", "value": "Add a tip"},
#                   {"label": "Delivery Partner Fee", "value": "FREE"}],
#     "toPay": {"label": "To Pay", "value": "₹451"}}
#
# Three things the first, guessed mapping got wrong, each of which alone would
# have refused every build:
#
#   1. The total is under `toPay`, not any of "total"/"grandTotal"/"payable".
#   2. billBreakdown is a MAPPING holding a list, not a list of typed rows — so
#      iterating it yielded ("lineItems", <list>) and nothing numeric.
#   3. Fee labels are prose ("Delivery Partner Fee"), not keys, so they have to
#      be matched as substrings. Exact matching found none of them.
#
# And the values are DISPLAY STRINGS: "₹448.00", but also "FREE" and "Add a
# tip". rupees() reads the first two as 448.0 and 0.0; "Add a tip" is not a
# charge and must not become one, which is what _is_money() guards.
_TOTAL_KEYS = ("topay", "tobepaid", "total", "grandtotal", "payable",
               "finaltotal", "ordertotal", "totalpayable", "billtotal")
_SUBTOTAL_KEYS = ("itemtotal", "subtotal", "itemstotal", "mrptotal", "cartsubtotal")
# Substrings, matched against the normalised label. "delivery" catches
# "Delivery Partner Fee"; the tip is excluded explicitly below because a tip is
# a choice, not a charge.
_FEE_MARKERS = ("delivery", "handling", "surge", "smallcart", "smallorder",
                "packaging", "convenience", "service", "ratecard", "platform")
_TAX_MARKERS = ("tax", "gst", "cgst", "sgst", "vat")
_NOT_A_CHARGE = ("tip", "saving", "saved", "discount", "youpay")

_MONEY_RE = __import__("re").compile(r"-?[\d,]+(?:\.\d+)?")


def _norm(label: Any) -> str:
    return "".join(ch for ch in str(label).lower() if ch.isalnum())


def _is_money(value: Any) -> bool:
    """"₹3.01" and "FREE" are amounts; "Add a tip" is an invitation."""
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if text.upper() in ("FREE", "₹0", "0"):
        return True
    return bool(_MONEY_RE.search(text)) and "add" not in text.lower()


def _amount(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.upper() == "FREE":
        return 0.0
    match = _MONEY_RE.search(text)
    return float(match.group(0).replace(",", "")) if match else 0.0


def _rows_of(cart: dict[str, Any]) -> list[tuple[str, float]]:
    """Flatten Swiggy's bill into (normalised label, rupees) pairs.

    Handles the measured shape first, then the two other shapes a bill
    plausibly arrives in, so a future change degrades to `found=False` and a
    refusal rather than to a confident wrong number.
    """
    body = cart.get("cart", cart) if isinstance(cart, dict) else {}
    if not isinstance(body, dict):
        return []
    out: list[tuple[str, float]] = []

    for container in ("billBreakdown", "bill", "billDetails", "priceBreakup",
                      "totals", "charges", "breakdown", "billInfo"):
        node = body.get(container)
        if isinstance(node, dict):
            # The measured shape: {"lineItems": [...], "toPay": {...}}.
            rows = node.get("lineItems")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and _is_money(row.get("value")):
                        out.append((_norm(row.get("label")), _amount(row.get("value"))))
            for key, value in node.items():
                if key == "lineItems":
                    continue
                if isinstance(value, dict) and _is_money(value.get("value")):
                    # {"toPay": {"label": "To Pay", "value": "₹451"}} — keyed by
                    # the FIELD name, because the display label is prose.
                    out.append((_norm(key), _amount(value.get("value"))))
                elif _is_money(value):
                    out.append((_norm(key), _amount(value)))
        elif isinstance(node, list):
            for row in node:
                if not isinstance(row, dict):
                    continue
                label = (row.get("type") or row.get("key") or row.get("label")
                         or row.get("name") or row.get("title") or "")
                value = next((row[k] for k in ("amount", "value", "price")
                              if k in row), None)
                if label and _is_money(value):
                    out.append((_norm(label), _amount(value)))

    if not out:
        for label, value in body.items():
            if _is_money(value) and not isinstance(value, bool):
                out.append((_norm(label), _amount(value)))
    return out


def totals_of(cart: dict[str, Any]) -> SwiggyTotals:
    """Swiggy's own bill, in rupees. `found` is False when no total could be
    read — which build() treats as a refusal, because a cart whose cost is
    unknown is not something to hand a human for approval."""
    rows = _rows_of(cart)

    def exact(keys: tuple[str, ...]) -> float:
        return sum(amount for label, amount in rows if label in keys)

    def marked(markers: tuple[str, ...]) -> float:
        return sum(amount for label, amount in rows
                   if any(m in label for m in markers)
                   and not any(n in label for n in _NOT_A_CHARGE)
                   and label not in _TOTAL_KEYS and label not in _SUBTOTAL_KEYS)

    total = exact(_TOTAL_KEYS)
    return SwiggyTotals(
        total_inr=total,
        subtotal_inr=exact(_SUBTOTAL_KEYS),
        fees_inr=marked(_FEE_MARKERS),
        taxes_inr=marked(_TAX_MARKERS),
        found=total > 0,
        raw_rows=tuple(rows),
    )


def split_sku(sku: str) -> tuple[str, str]:
    """"spinId:skuId" -> (spinId, skuId), as the read adapter writes it.

    No lookup and no name matching: the line that was approved already names
    the thing to cart. Searching by product TITLE is what carted the wrong
    item on order_efaa1edd24a1.
    """
    spin_id, _, sku_id = str(sku).partition(":")
    return spin_id.strip(), sku_id.strip()


if __name__ == "__main__":  # pragma: no cover - diagnostic only
    import argparse
    import json as _json

    from grocery.adapters.swiggy_mcp import _access_token

    parser = argparse.ArgumentParser(description="Swiggy cart diagnostics (read-only)")
    parser.add_argument("--probe", action="store_true",
                        help="print the real get_cart shape so totals_of() can be pinned")
    parser.add_argument("--tools", action="store_true",
                        help="print the live tool surface and what this client will touch")
    args = parser.parse_args()

    client = SwiggyCartClient(token_provider=_access_token)
    if args.tools:
        print(_json.dumps(client.safety_report(), indent=2))
    if args.probe:
        try:
            cart = client.fetch()
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
            raise SystemExit(f"get_cart failed: {exc}")
        print("get_cart, verbatim — pin totals_of() against this:")
        print(_json.dumps(cart, indent=2)[:4000])
        print("\nparsed rows:", totals_of(cart))
