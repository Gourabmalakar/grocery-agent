"""Turning an approved cart into a real cart at the merchant.

THE CREDENTIAL BOUNDARY. This runs in the Console, never the worker. It holds a
client that can write to Gourab's real Zepto account; worker/ is the process
this architecture allows to be wrong, and an AST test keeps it out of there.

WHAT IT CANNOT DO. The UCP cart surface has no checkout tool and no order tool —
`assert_cannot_order()` verifies that at connect time rather than trusting the
docs. So the worst this module can do is leave a basket in somebody's account,
and `cancel()` takes that back.

THE ID MAPPING, which is the part worth reading. Our cart lines carry Zepto's
MCP product id (`ef4831c2-…`); UCP's create_cart wants a composite variant id
(`ef4831c2-…:86ef5e75-…`). Measured 2026-10-11, the UCP id is exactly
`{mcp_product_id}:{variant_id}` — so the line we approved can be found by PREFIX,
deterministically, with no name matching and no guessing.

That matters because the alternative was fuzzy-matching product names, which
would occasionally cart the wrong thing with total confidence. The price is
checked as well: two independent signals agreeing is what makes this a lookup
rather than a hope. If either fails, the whole build is refused — a cart that is
half what was approved is worse than no cart, because the human has already said
yes to a number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from grocery import config
from grocery.adapters.swiggy_mcp import _access_token as _swiggy_token
from grocery.adapters.zepto_mcp import _access_token as _zepto_token
from grocery.carts import swiggy as swiggy_cart
from grocery.ucp.cart import CartTotals, UcpCartClient, money, totals_of

# Merchant -> its UCP endpoint. Only merchants whose cart surface we have
# actually exercised appear here; `config.CART_CAPABLE_MERCHANTS` is the
# user-facing list and this is the implementation of it.
#
# ZEPTO WAS REMOVED 2026-10-12, and the reason is worth keeping because its cart
# surface WORKS — which is exactly what made this hard to see. Three measurements:
#
#   1. The cart is real and persists. get_cart returned the basket a day later,
#      2 line items, ₹133, status success.
#   2. It can never appear in the Zepto app. update_cart requires `deviceId` —
#      "a fallback cart key when a transportSessionId is not available" — and
#      "persists cart state for the session". The cart belongs to the DEVICE
#      that built it. This agent is not his phone. (Swiggy keys on
#      selectedAddressId, which is why Swiggy's cart IS the app's cart.)
#   3. The hand-off link does not resolve. `continue_url` is
#      https://www.zepto.com/cart?id=<cart_id>, and that URL discards both the
#      path and the id and lands on the Zepto homepage. Confirmed twice: by
#      Gourab signed in on his Mac, and by fetching it here.
#
# So Zepto built baskets that were genuinely real and reachable by nobody, while
# reporting success. A merchant that cannot hand a cart back to the human is not
# cart-capable, whatever its API returns. It stays in the price comparison,
# where it is useful and honest.
#
# To re-enable: verify (2) or (3) has changed — a cart built here must be
# openable by him — then put the endpoint back. Do not re-enable on the strength
# of create_cart returning an id, which it always did.
_UCP_ENDPOINTS: dict[str, str] = {}

# How far a UCP price may differ from the one the human approved before we stop
# believing it is the same product. A paisa of rounding is fine; anything more
# means the catalogue moved under us and the approval was for a different thing.
_PRICE_TOLERANCE_INR = 0.01


class CannotBuild(RuntimeError):
    """The real cart was not built, and the message says why. Always refuse
    rather than build something approximately right."""


@dataclass
class BuiltCart:
    merchant: str
    cart_ref: str
    continue_url: str
    # CartTotals (Zepto/UCP) or swiggy.SwiggyTotals — same field names, and
    # deliberately NOT a shared base class: the two merchants quote different
    # units (paise vs rupees) and conflating them is how a total ends up a
    # hundredth of itself.
    totals: Any
    lines: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant, "cart_ref": self.cart_ref,
            "continue_url": self.continue_url, "lines": self.lines,
            "total_inr": self.totals.total_inr,
            "subtotal_inr": self.totals.subtotal_inr,
            "fees_inr": self.totals.fees_inr, "taxes_inr": self.totals.taxes_inr,
            # Stated rather than implied: a cart-stage total from Zepto has no
            # delivery and no GST in it, because those arrive at checkout and
            # Zepto has not implemented UCP's checkout capability.
            "fees_known": bool(self.totals.fees_inr or self.totals.taxes_inr),
        }


# Merchants with a cart builder of their own, outside the UCP map above.
# Swiggy speaks its own MCP, not UCP — see grocery/carts/swiggy.py for why that
# surface needs a stricter client than Zepto's does.
_NATIVE_BUILDERS = ("swiggy",)


def can_build(merchant: str) -> bool:
    m = merchant.lower()
    return m in _UCP_ENDPOINTS or m in _NATIVE_BUILDERS


def _client(merchant: str) -> UcpCartClient:
    endpoint = _UCP_ENDPOINTS.get(merchant.lower())
    if not endpoint:
        raise CannotBuild(
            f"there is no cart API for {merchant!r} — this system can read its "
            f"prices and cannot put anything in a basket there"
        )
    client = UcpCartClient(endpoint, token_provider=_zepto_token)
    client.connect()
    report = client.assert_cannot_order()
    if not report["cannot_order"]:
        # The whole reason for preferring this surface is that it cannot buy.
        # If that stops being true we stop using it rather than rely on our own
        # restraint.
        raise CannotBuild(
            f"{merchant}'s cart endpoint is now offering ordering tools "
            f"({report['ordering_shaped']}) — refusing to use it until that is "
            f"understood"
        )
    return client


def _address_id(client: UcpCartClient, merchant: str) -> str:
    if config.ZEPTO_ADDRESS_ID and merchant.lower() == "zepto":
        return config.ZEPTO_ADDRESS_ID
    payload = client._call("search_locations", {}) or {}

    # TWO LISTS, AND ONLY ONE OF THEM IS THE DELIVERY ADDRESS. UCP's location
    # search returns `locations` (places you could collect from) and
    # `buyer_address_locations` (places the buyer has saved to deliver to).
    # Zepto's `locations` is ALWAYS empty and says so in its own message:
    # "Zepto fulfils every order by delivery from a dark store, so there are no
    # pickup locations."
    #
    # Reading the pickup list first turned a perfectly good saved address into
    # "zepto has no saved delivery address, so there is nowhere to build a cart
    # for. Add one in the app." — told to a man who had one (order_efaa1edd24a1).
    # An empty list was indistinguishable from an absent one the way this was
    # written, which is why it read as nothing rather than as the wrong thing.
    saved = payload.get("buyer_address_locations") or payload.get("addresses") or []
    ids = [str(l.get("id")) for l in saved if isinstance(l, dict) and l.get("id")]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise CannotBuild(
            f"{merchant} has no saved delivery address, so there is nowhere to "
            f"build a cart for. Add one in the app."
        )
    raise CannotBuild(
        f"{merchant} has {len(ids)} saved addresses and nothing says which to use. "
        f"Set ZEPTO_ADDRESS_ID to the one you want."
    )


def _variant_for(client: UcpCartClient, line: dict[str, Any], address_id: str) -> str:
    """The UCP variant id for a line we already approved.

    USUALLY NO LOOKUP AT ALL. The Zepto adapter now stores the sku as
    "{productVariantId}:{storeProductId}", which IS the UCP variant id — so the
    approved line already names the thing to cart, exactly and without
    inference.

    The fallback below exists for lines approved before that change, and it is
    a prefix match on the product half rather than a name match. Searching by
    product TITLE is what failed on order_efaa1edd24a1: "Coca-Cola PET| Cola
    Sparkling Soft Drink | The Coca-Cola Company" is a label, not a query.
    """
    sku = str(line.get("sku", ""))
    name = str(line.get("name", ""))
    if not sku:
        raise CannotBuild(f"the approved line {name!r} has no sku to look up")

    if ":" in sku:
        return sku

    # Legacy line: find the variant whose id starts with this product id.
    for candidate in client.search(name, address_id=address_id):
        if candidate["id"].startswith(f"{sku}:"):
            return candidate["id"]
    raise CannotBuild(
        f"{name!r} was priced before carting existed and cannot be matched to a "
        f"basket entry — send the list again to get a fresh basket"
    )


def _verify_price(client: UcpCartClient, variant_id: str, line: dict[str, Any],
                  address_id: str) -> None:
    """Check the merchant still charges what was approved.

    The cart is built from an id, so nothing stops us carting a product whose
    price moved since he said yes. Two independent readings agreeing is the
    whole basis for calling this safe.
    """
    approved = float(line.get("unit_price_inr") or 0)
    if not approved:
        return
    payload = client._call("get_product", {
        "id": variant_id,
        "catalog": {"id": variant_id,
                    "context": {"buyer_address_location": address_id}},
    }) or {}
    product = payload.get("product") or payload
    variants = product.get("variants") or []
    current = None
    for variant in variants:
        if variant.get("id") == variant_id:
            current = money(variant.get("price"))
            break
    if current is None or current <= 0:
        return                      # no opinion rather than a guessed refusal
    if abs(current - approved) > _PRICE_TOLERANCE_INR:
        raise CannotBuild(
            f"{line.get('name', 'an item')!r} is ₹{current:.2f} at the merchant now "
            f"but ₹{approved:.2f} was approved — refusing to cart a price you did "
            f"not agree to"
        )


def build(order_cart: dict[str, Any]) -> BuiltCart:
    """Build the approved cart for real. Raises CannotBuild; never half-builds."""
    merchant = str(order_cart.get("merchant", ""))
    if merchant.lower() in _NATIVE_BUILDERS:
        return _build_swiggy(order_cart)
    client = _client(merchant)
    address_id = _address_id(client, merchant)

    # EVERY line is resolved BEFORE anything is created, so a failure on the
    # last item does not leave three of five sitting in a real basket.
    wanted: list[tuple[str, int]] = []
    resolved: list[dict[str, Any]] = []
    for line in order_cart.get("line_items", []):
        variant_id = _variant_for(client, line, address_id)
        _verify_price(client, variant_id, line, address_id)
        quantity = int(line.get("qty", 1))
        wanted.append((variant_id, quantity))
        resolved.append({"name": line.get("name"), "qty": quantity,
                         "variant_id": variant_id,
                         "approved_inr": line.get("line_total_inr")})

    if not wanted:
        raise CannotBuild("the approved cart has no lines")

    created = client.create(wanted, address_id=address_id)
    body = created.get("cart", created)
    cart_ref = str(body.get("id") or created.get("id") or "")
    if not cart_ref:
        raise CannotBuild(f"{merchant} created a cart without returning an id")

    return BuiltCart(merchant=merchant, cart_ref=cart_ref,
                     continue_url=totals_of(created).continue_url,
                     totals=totals_of(created), lines=resolved)


def cancel(merchant: str, cart_ref: str) -> None:
    """Take back a cart nobody wants. Safe to call twice."""
    try:
        if merchant.lower() in _NATIVE_BUILDERS:
            _swiggy_client().clear()
            return
        _client(merchant).cancel(cart_ref)
    except Exception as exc:  # noqa: BLE001 — cancelling must never raise onward
        print(f"[ordering] could not cancel {cart_ref} at {merchant}: {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# Swiggy Instamart. A different surface, and a stricter client — see
# grocery/carts/swiggy.py for why.
# ---------------------------------------------------------------------------

def _swiggy_client() -> swiggy_cart.SwiggyCartClient:
    client = swiggy_cart.SwiggyCartClient(token_provider=_swiggy_token)
    client.connect()
    report = client.safety_report()

    # Unlike Zepto's UCP endpoint, this surface CAN place orders — `checkout`
    # creates and pays in one call. So the check here is not "the surface is
    # harmless" (it isn't) but "the tools we need are present and the ones that
    # spend are not reachable through this client".
    missing = [t for t in ("get_cart", "update_cart", "clear_cart") if t not in report["reachable"]]
    if missing:
        raise CannotBuild(
            f"swiggy's cart tools {missing} are not on its surface any more — "
            f"refusing to guess at a replacement"
        )
    if report["client_can_order"]:
        raise CannotBuild("the swiggy cart client claims it can order; refusing to use it")
    return client


def _swiggy_address_id(client: swiggy_cart.SwiggyCartClient) -> str:
    if config.SWIGGY_ADDRESS_ID:
        return config.SWIGGY_ADDRESS_ID
    ids = [str(a["id"]) for a in client.addresses()]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise CannotBuild(
            "swiggy has no saved delivery address, so there is nowhere to build "
            "a cart for. Add one in the app."
        )
    raise CannotBuild(
        f"swiggy has {len(ids)} saved addresses and nothing says which to use. "
        f"Set SWIGGY_ADDRESS_ID to the one you want."
    )


def _build_swiggy(order_cart: dict[str, Any]) -> BuiltCart:
    """Build the approved basket at Swiggy Instamart.

    THE ORDER OF OPERATIONS IS THE SAFETY ARGUMENT, and it differs from the UCP
    path because Swiggy's `update_cart` REPLACES the entire cart rather than
    adding to it.

    1. Refuse outright if the cart already has something in it. Overwriting is
       the only way Swiggy lets us write, so a cart somebody assembled by hand
       would be destroyed without being asked. Loud refusal beats silent loss.
    2. Resolve every line BEFORE writing anything, so a bad last item does not
       leave a partial basket.
    3. Write, then verify against the merchant's own cart, then `clear()` and
       refuse on any disagreement. Verifying after the write is acceptable here
       — and better, because it checks Swiggy's authoritative number rather than
       a search result — precisely because step 1 guaranteed the cart was empty,
       so clearing it restores exactly the state we found.
    """
    merchant = str(order_cart.get("merchant", "swiggy"))
    client = _swiggy_client()
    address_id = _swiggy_address_id(client)

    existing = _swiggy_fetch(client, "read your Swiggy cart")
    if _swiggy_item_count(existing) > 0:
        raise CannotBuild(
            "your Swiggy cart already has items in it, and Swiggy only lets us "
            "replace a cart wholesale — not add to it. Empty the cart in the "
            "app and ask again, and nothing of yours gets thrown away."
        )

    wanted: list[tuple[str, str, int]] = []
    resolved: list[dict[str, Any]] = []
    for line in order_cart.get("line_items", []):
        name = str(line.get("name", "an item"))
        spin_id, sku_id = swiggy_cart.split_sku(str(line.get("sku", "")))
        if not spin_id:
            raise CannotBuild(
                f"{name!r} has no Swiggy product id to cart — send the list "
                f"again to get a fresh basket"
            )
        quantity = int(line.get("qty", 1))
        wanted.append((spin_id, sku_id, quantity))
        resolved.append({"name": line.get("name"), "qty": quantity,
                         "variant_id": spin_id,
                         "approved_inr": line.get("line_total_inr")})

    if not wanted:
        raise CannotBuild("the approved cart has no lines")

    client.replace(wanted, address_id=address_id)

    # Read back the merchant's own cart. This is the number anything would be
    # paid against, so it is the number that has to agree with the approval.
    built = _swiggy_fetch(client, "read back the cart it just built")
    totals = swiggy_cart.totals_of(built)
    if not totals.found:
        client.clear()
        raise CannotBuild(
            "Swiggy built the cart but did not report a total we could read, so "
            "there is no trustworthy number to approve. The cart has been "
            f"cleared. Bill rows seen: {list(totals.raw_rows) or 'none'}"
        )

    approved_total = sum(float(l.get("line_total_inr") or 0)
                         for l in order_cart.get("line_items", []))
    if approved_total and totals.subtotal_inr:
        drift = abs(totals.subtotal_inr - approved_total)
        if drift > _PRICE_TOLERANCE_INR:
            client.clear()
            raise CannotBuild(
                f"Swiggy's item total is ₹{totals.subtotal_inr:.2f} but "
                f"₹{approved_total:.2f} was approved — refusing a cart you did "
                f"not agree to. The cart has been cleared."
            )

    return BuiltCart(
        merchant=merchant,
        cart_ref=_swiggy_cart_ref(built),
        continue_url=_swiggy_continue_url(built),
        totals=totals,
        lines=resolved,
    )


# Swiggy's cart tools are TIME-GATED in a way its search is not. At 01:00 IST
# `search_products` happily returned 11 priced products while `get_cart`
# answered "The store is unavailable at the moment" — Instamart was shut. So a
# basket can be priced at an hour when it cannot be built, and the raw MCP
# string is not something to show a human at 1am.
_STORE_CLOSED = ("store is unavailable", "store unavailable", "not serviceable",
                 "currently closed", "store is closed")


def _swiggy_fetch(client: Any, what: str) -> dict[str, Any]:
    try:
        return client.fetch()
    except Exception as exc:  # noqa: BLE001 - translated, then re-raised as a refusal
        message = str(exc).lower()
        if any(marker in message for marker in _STORE_CLOSED):
            raise CannotBuild(
                "Swiggy Instamart is closed right now, so it will not open a "
                "cart — its search still works, which is why the basket priced "
                "fine. Try again once the store is open."
            ) from exc
        raise CannotBuild(f"could not {what}: {str(exc)[:200]}") from exc


def _swiggy_item_count(cart: dict[str, Any]) -> int:
    body = cart.get("cart", cart) if isinstance(cart, dict) else {}
    if not isinstance(body, dict):
        return 0
    for key in ("totalItems", "itemCount", "count"):
        value = body.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    items = body.get("items")
    return len(items) if isinstance(items, list) else 0


def _swiggy_cart_ref(cart: dict[str, Any]) -> str:
    """Swiggy's cart is bound to the account rather than addressed by id, so
    there may be no id to record. `swiggy:account-cart` is honest about that —
    an invented id would imply a handle we do not have."""
    body = cart.get("cart", cart) if isinstance(cart, dict) else {}
    if isinstance(body, dict):
        for key in ("cartId", "id", "orderId"):
            value = body.get(key)
            if value:
                return str(value)
    return "swiggy:account-cart"


def _swiggy_continue_url(cart: dict[str, Any]) -> str:
    body = cart.get("cart", cart) if isinstance(cart, dict) else {}
    if isinstance(body, dict):
        for key in ("continue_url", "continueUrl", "checkoutUrl", "url", "deeplink"):
            value = body.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    return ""
