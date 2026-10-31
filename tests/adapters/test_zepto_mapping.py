"""Zepto's response mapping, pinned against real observed output.

Two findings from 2026-10-07, both of which made Zepto unusable or wrong:

1. ZEPTO QUOTES PAISE. `"price": 3400` on a 250 ml pack is ₹34.00. Read as
   rupees it became ₹3,400, and at 100x Zepto could never win a comparison —
   so it would have sat in every basket looking merely expensive and never been
   chosen. A silently wrong merchant is worse than a visibly broken one.

2. A BROKEN ACKNOWLEDGEMENT IS NOT A FAILED OPERATION. select_saved_address
   drops its SSE stream, which `requests` reports as "Response ended
   prematurely"; the store selection lands anyway. Raising on that made Zepto
   permanently unavailable over a cosmetic defect in its response handling.
"""
from __future__ import annotations

import pytest

# Verbatim from `python -m grocery.adapters.zepto_mcp --probe "amul milk 1l"`.
REAL_ROW = {
    "id": "556c3d3e-f376-4dc4-b98a-e452d14f81d8",
    "productVariantId": "556c3d3e-f376-4dc4-b98a-e452d14f81d8",
    "storeProductId": "8eed6c64-cc8b-5f60-9e62-6c0b14434a89",
    "name": "Milky Mist Lactose Free Milk",
    "price": 3400,
    "mrp": 4000,
    "packSize": "1 pack (250 ml)",
    "availableQuantity": 3,
}


def test_the_sku_is_the_ucp_variant_id(isolated_data_dir):
    """Zepto's UCP cart API addresses a purchasable thing as
    "{productVariantId}:{storeProductId}", and the MCP search returns both
    halves. Keeping only the first meant an approved line could not be put in a
    real basket, and the fallback — searching by the product's own title —
    failed on names like "Coca-Cola PET| Cola Sparkling Soft Drink | The
    Coca-Cola Company" (order_efaa1edd24a1)."""
    from grocery.adapters.zepto_mcp import _listing_from

    listing = _listing_from(REAL_ROW, strict=False)
    assert listing.sku == ("556c3d3e-f376-4dc4-b98a-e452d14f81d8"
                           ":8eed6c64-cc8b-5f60-9e62-6c0b14434a89")


def test_a_row_without_a_store_product_id_still_yields_a_sku(isolated_data_dir):
    """Half an id is better than none — the cart path has a fallback for it."""
    from grocery.adapters.zepto_mcp import _listing_from

    row = {k: v for k, v in REAL_ROW.items() if k != "storeProductId"}
    assert _listing_from(row, strict=False).sku == "556c3d3e-f376-4dc4-b98a-e452d14f81d8"


def test_paise_become_rupees(isolated_data_dir):
    from grocery.adapters.zepto_mcp import _listing_from

    listing = _listing_from(REAL_ROW, strict=False)
    assert listing.price_inr == 34.0, "3400 paise is ₹34, not ₹3,400"


def test_the_pack_size_is_read_from_zeptos_own_phrasing(isolated_data_dir):
    """"1 pack (250 ml)" — the quantity is inside parentheses after a count."""
    from grocery.adapters.zepto_mcp import _listing_from

    listing = _listing_from(REAL_ROW, strict=False)
    assert (listing.pack_qty, listing.pack_unit) == (250.0, "ml")


def test_the_cross_merchant_check_that_caught_it(isolated_data_dir):
    """Zepto quoted 7700 for a litre of Amul Taaza on the same day Blinkit
    charged ₹77.00. Two independent readings agreeing to the paisa is what
    turned this from a hunch into a measurement."""
    from grocery.adapters.zepto_mcp import _listing_from

    row = dict(REAL_ROW, name="Amul Taaza Homogenised Toned Milk (Tetra Pack)",
               price=7700, packSize="1 pack (1 l)")
    assert _listing_from(row, strict=False).price_inr == 77.0


def test_a_rupee_string_is_not_converted_twice(isolated_data_dir):
    """Only the numeric API fields are paise. A "₹72" string is already rupees,
    and dividing it too would be the same bug in the other direction."""
    from grocery.adapters.zepto_mcp import _as_price

    assert _as_price("₹72") == 72.0
    assert _as_price(7200) == 72.0


def test_a_dropped_ack_on_address_selection_does_not_disable_zepto(isolated_data_dir, monkeypatch, capsys):
    """The adapter must continue and let the search be the test."""
    from grocery.adapters import zepto_mcp
    from grocery.mcp.client import McpError

    adapter = zepto_mcp.ZeptoMCPAdapter()
    calls = []

    def fake_call(tool, args):
        calls.append(tool)
        if tool == "get_user_details":
            return "User Profile\nName: Gourab Malakar\nRegistered: Yes"
        if tool == "list_saved_addresses":
            return {"addresses": [{"id": "addr-1", "label": "home"}]}
        if tool == "select_saved_address":
            raise McpError("empty response to tools/call")
        raise AssertionError(tool)

    monkeypatch.setattr(adapter, "_call", fake_call)
    adapter._register_and_select_address()          # must not raise

    assert "select_saved_address" in calls
    assert "dropped its response stream" in capsys.readouterr().out


def test_a_real_error_on_address_selection_still_raises(isolated_data_dir, monkeypatch):
    """Tolerating a dropped stream must not become tolerating everything."""
    from grocery.adapters import zepto_mcp
    from grocery.mcp.client import McpError

    adapter = zepto_mcp.ZeptoMCPAdapter()

    def fake_call(tool, args):
        if tool == "get_user_details":
            return "Registered: Yes"
        if tool == "list_saved_addresses":
            return {"addresses": [{"id": "addr-1"}]}
        raise McpError("tool select_saved_address reported an error: address not found")

    monkeypatch.setattr(adapter, "_call", fake_call)
    with pytest.raises(McpError, match="address not found"):
        adapter._register_and_select_address()
