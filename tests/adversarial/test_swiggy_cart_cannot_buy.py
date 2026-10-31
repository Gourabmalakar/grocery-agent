"""Swiggy's cart surface can place orders. This client must not be able to.

The Zepto UCP tests assert safety by ABSENCE — that endpoint offers no ordering
tool at all. That argument is unavailable here. A live tools/list on 2026-10-12
returned `checkout` ("Creates order and confirms payment in a single
operation"), `confirm_order`, `check_payment_status` and `get_payment_options`
sitting beside `update_cart`, on a token this repo holds, with an `mcp:tools`
grant that is read+write. So on this surface the allowlist IS the guarantee,
and these tests are the thing that keeps it honest.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Verbatim from a live tools/list, 2026-10-12. Kept as data so a change in
# Swiggy's surface shows up as a test edit rather than as a surprise in prod.
_LIVE_SURFACE = ("check_payment_status", "checkout", "clear_cart",
                 "confirm_order", "create_address", "delete_address",
                 "get_addresses", "get_cart", "get_delivery_status",
                 "get_orders", "get_payment_options", "report_error",
                 "search_products", "track_order", "update_cart",
                 "your_go_to_items")


def _client():
    from grocery.carts.swiggy import SwiggyCartClient
    return SwiggyCartClient(token_provider=lambda: "t")


def test_the_client_refuses_every_tool_that_can_spend_money(isolated_data_dir):
    from grocery.carts.swiggy import SwiggyToolRefused

    client = _client()
    for tool in ("checkout", "confirm_order", "check_payment_status",
                 "get_payment_options", "track_order", "get_orders",
                 "something_added_next_month"):
        with pytest.raises(SwiggyToolRefused):
            client._call(tool, {})


def test_the_client_refuses_to_edit_the_address_book(isolated_data_dir):
    """A cart builder has no business changing where things get delivered —
    and this account holds exactly one address, so a delete is unrecoverable
    without the app."""
    from grocery.carts.swiggy import SwiggyToolRefused

    client = _client()
    for tool in ("create_address", "delete_address"):
        with pytest.raises(SwiggyToolRefused):
            client._call(tool, {})


def test_the_safety_report_admits_the_surface_can_order(isolated_data_dir, monkeypatch):
    """Claiming 'cannot order' here would be a lie, and a lie in a safety
    report is worse than no report."""
    client = _client()
    client._connected = True
    monkeypatch.setattr(client._client, "list_tools",
                        lambda: [{"name": n} for n in _LIVE_SURFACE])

    report = client.safety_report()
    assert report["surface_can_order"] is True
    assert "checkout" in report["ordering_shaped"]
    assert report["client_can_order"] is False
    # Only the five cart tools are reachable out of sixteen offered.
    assert report["reachable"] == ["clear_cart", "get_addresses", "get_cart",
                                   "search_products", "update_cart"]


def test_the_worker_never_imports_a_cart_builder(isolated_data_dir):
    """worker/ is the process this architecture allows to be wrong. Either cart
    builder can write to a real merchant account."""
    offenders = []
    for path in sorted((_REPO / "worker").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            module = (node.module if isinstance(node, ast.ImportFrom)
                      else node.names[0].name if isinstance(node, ast.Import) else None)
            if module and ("grocery.ucp" in module or "grocery.carts" in module):
                offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    assert not offenders, f"worker/ imports a cart builder: {offenders}"


def test_swiggy_money_is_rupees_not_paise(isolated_data_dir):
    """THE UNIT TRAP. Zepto quotes paise (3400 = ₹34), Swiggy quotes rupees
    (17 = ₹17). Reusing grocery/ucp/cart.py's money() here would report every
    Swiggy total as a hundredth of itself."""
    from grocery.carts.swiggy import rupees
    from grocery.ucp.cart import money

    assert rupees(131) == 131.0
    assert rupees("₹1,131.50") == 1131.5
    assert rupees({"amount": 131}) == 131.0
    # The helper that must NOT be used on Swiggy numbers, proving they differ.
    assert money(131) == 1.31


def test_a_sku_carries_both_ids_so_nothing_is_matched_by_name(isolated_data_dir):
    """Searching by product TITLE carted the wrong item on order_efaa1edd24a1.
    The approved line names the thing to cart, exactly."""
    from grocery.carts.swiggy import split_sku

    assert split_sku("31VQRTD5DH:M2SBFF9LIR") == ("31VQRTD5DH", "M2SBFF9LIR")
    assert split_sku("31VQRTD5DH") == ("31VQRTD5DH", "")
    assert split_sku("") == ("", "")


def test_an_optional_sku_id_is_omitted_rather_than_sent_empty(isolated_data_dir):
    """Swiggy marks skuId optional; sending "" is not the same as sending
    nothing, and a server is entitled to reject it."""
    sent = {}
    client = _client()
    client._connected = True
    client._call = lambda tool, args: sent.update({"tool": tool, **args})  # type: ignore[method-assign]

    client.replace([("SPIN1", "", 2), ("SPIN2", "SKU2", 1)], address_id="addr")
    assert sent["items"] == [{"spinId": "SPIN1", "quantity": 2},
                             {"spinId": "SPIN2", "quantity": 1, "skuId": "SKU2"}]
