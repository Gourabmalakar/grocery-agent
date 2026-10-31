"""The cart surface must not be able to buy, and the worker must not hold it.

Two separate claims, both checkable:

1. UCP's cart capability exposes search/create/update/get/cancel and no
   checkout or order tool. That is the reason for preferring it over Zepto's
   own MCP tools, where `update_cart` sits beside `create_order`,
   `create_wallet_order` and `create_upi_reserve_pay_order`. An absence is only
   worth something if somebody looks, so assert_cannot_order() looks.

2. The cart client can write to a real merchant account, so the worker — the
   process this architecture allows to be wrong — must never construct one.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def test_the_cart_client_refuses_anything_that_is_not_a_cart_tool(isolated_data_dir):
    from grocery.ucp.cart import UcpCartClient, UcpToolRefused

    client = UcpCartClient("https://example.invalid/ucp/mcp", token_provider=lambda: "t")
    for tool in ("checkout", "create_order", "confirm_order", "create_wallet_order",
                 "get_payment_options", "something_added_next_month"):
        with pytest.raises(UcpToolRefused):
            client._call(tool, {})


def test_an_ordering_tool_appearing_on_the_surface_is_reported(isolated_data_dir, monkeypatch):
    """If Zepto adds checkout to the UCP endpoint, the safety premise changes
    and we must find out by checking rather than by it being used."""
    from grocery.ucp import cart as cart_mod

    client = cart_mod.UcpCartClient("https://example.invalid/ucp/mcp", token_provider=lambda: "t")
    client._connected = True
    monkeypatch.setattr(client._client, "list_tools",
                        lambda: [{"name": "create_cart"}, {"name": "checkout"}])
    report = client.assert_cannot_order()
    assert report["cannot_order"] is False
    assert "checkout" in report["ordering_shaped"]


def test_a_clean_surface_reports_it_cannot_order(isolated_data_dir, monkeypatch):
    from grocery.ucp import cart as cart_mod

    client = cart_mod.UcpCartClient("https://example.invalid/ucp/mcp", token_provider=lambda: "t")
    client._connected = True
    monkeypatch.setattr(client._client, "list_tools", lambda: [
        {"name": n} for n in ("search_catalog", "create_cart", "update_cart",
                              "get_cart", "cancel_cart", "search_locations")])
    report = client.assert_cannot_order()
    assert report["cannot_order"] is True
    assert report["unknown"] == []


def test_the_worker_never_imports_the_cart_client(isolated_data_dir):
    """It can write to a real merchant account. worker/ is the process that is
    allowed to be wrong, so it must not be able to reach this."""
    offenders = []
    for path in sorted((_REPO / "worker").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            module = (node.module if isinstance(node, ast.ImportFrom)
                      else node.names[0].name if isinstance(node, ast.Import) else None)
            if module and "grocery.ucp" in module:
                offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    assert not offenders, f"worker/ imports the cart client: {offenders}"


def test_ucp_money_is_minor_units_with_a_stated_currency(isolated_data_dir):
    """Unlike Zepto's bare MCP integers, UCP states the currency — so there is
    nothing to guess, which is how the ₹3,400-for-a-250ml-pack bug happened."""
    from grocery.ucp.cart import money

    assert money({"amount": 8200, "currency": "INR"}) == 82.0
    assert money({"amount": 0, "currency": "INR"}) == 0.0


def test_the_merchants_total_is_what_is_reported(isolated_data_dir):
    """Our components are a check on theirs, never a substitute."""
    from grocery.ucp.cart import totals_of

    totals = totals_of({"cart": {"totals": {
        "total": {"amount": 63300}, "subtotal": {"amount": 60000},
        "tax": {"amount": 3300}}, "continue_url": "https://zeptonow.com/checkout/abc"}})
    assert totals.total_inr == 633.0
    assert totals.taxes_inr == 33.0
    assert totals.continue_url.endswith("/abc")
