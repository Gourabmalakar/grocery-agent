"""Swiggy's search REQUIRES a saved addressId (its schema's latitude/longitude
"guest mode" is refused for an authenticated token — verified against the live
server on 2026-09-27). Which address gets used therefore decides which
neighbourhood is priced, and a wrong one is invisible: real products, real
prices, wrong city.

These run offline with a stubbed _call, because the decision being tested is
ours, not Swiggy's.
"""
from __future__ import annotations

import pytest


def _adapter(monkeypatch, payload, *, configured=""):
    from grocery.adapters import swiggy_mcp
    monkeypatch.setattr(swiggy_mcp.config, "SWIGGY_ADDRESS_ID", configured, raising=False)
    a = swiggy_mcp.SwiggyMCPAdapter()
    monkeypatch.setattr(a, "_call", lambda tool, args: payload)
    return a, swiggy_mcp


def test_sole_saved_address_is_used(isolated_data_dir, monkeypatch):
    a, _ = _adapter(monkeypatch, {"addresses": [{"id": "addr_1", "label": "Home"}]})
    assert a._delivery_args() == {"addressId": "addr_1"}


def test_address_list_is_fetched_once_not_per_query(isolated_data_dir, monkeypatch):
    from grocery.adapters import swiggy_mcp
    monkeypatch.setattr(swiggy_mcp.config, "SWIGGY_ADDRESS_ID", "", raising=False)
    a = swiggy_mcp.SwiggyMCPAdapter()
    calls = []

    def fake_call(tool, args):
        calls.append(tool)
        return {"addresses": [{"id": "addr_1", "label": "Home"}]}

    monkeypatch.setattr(a, "_call", fake_call)
    for _ in range(4):
        a._delivery_args()
    assert calls == ["get_addresses"], "one address lookup per process, not per query"


def test_no_saved_address_is_refused_with_the_fix_in_the_message(isolated_data_dir, monkeypatch):
    """Never an empty result: "no listings" and "we could not ask" look
    identical to the human, and only one of them is the shelf being empty."""
    a, mod = _adapter(monkeypatch, {"addresses": [], "total": 0})
    with pytest.raises(mod.SwiggyUnavailable, match="no saved delivery address"):
        a._delivery_args()


def test_several_addresses_refuses_to_choose(isolated_data_dir, monkeypatch):
    a, mod = _adapter(monkeypatch, {"addresses": [
        {"id": "addr_1", "label": "Home"}, {"id": "addr_2", "label": "Office"}]})
    with pytest.raises(mod.SwiggyUnavailable, match="SWIGGY_ADDRESS_ID"):
        a._delivery_args()


def test_configured_address_wins_and_asks_swiggy_nothing(isolated_data_dir, monkeypatch):
    from grocery.adapters import swiggy_mcp
    monkeypatch.setattr(swiggy_mcp.config, "SWIGGY_ADDRESS_ID", "addr_chosen", raising=False)
    a = swiggy_mcp.SwiggyMCPAdapter()

    def explode(tool, args):
        raise AssertionError("must not call get_addresses when one is configured")

    monkeypatch.setattr(a, "_call", explode)
    assert a._delivery_args() == {"addressId": "addr_chosen"}


def test_unrecognisable_address_shape_raises_with_its_keys(isolated_data_dir, monkeypatch):
    """The per-address field names were read from Swiggy's tool description, not
    from data — every live get_addresses call so far returned an empty list. So
    an unknown shape must say what it saw rather than shop an address it could
    not identify."""
    a, mod = _adapter(monkeypatch, {"addresses": [{"uuid": "x", "town": "y"}]})
    with pytest.raises(mod.SwiggySchemaUnknown, match="uuid"):
        a._delivery_args()


def test_pincode_disagreement_is_announced(isolated_data_dir, monkeypatch, capsys):
    """Blinkit is shopped at location.current()'s coordinates; Swiggy is shopped
    at Swiggy's saved address. If they are different places the comparison is
    meaningless, so the disagreement must be visible."""
    from grocery import location
    a, _ = _adapter(monkeypatch, {"addresses": [
        {"id": "addr_1", "label": "Home", "formatted": "12 Some Road, Delhi 110001"}]})
    monkeypatch.setattr(location, "current", lambda: location.DeliveryLocation(
        pincode="560001", lat=12.9767, lon=77.5993, label="Bengaluru"))
    assert a._delivery_args() == {"addressId": "addr_1"}
    out = capsys.readouterr().out
    assert "WARNING" in out and "110001" in out and "560001" in out


def test_matching_pincode_is_silent(isolated_data_dir, monkeypatch, capsys):
    from grocery import location
    a, _ = _adapter(monkeypatch, {"addresses": [
        {"id": "addr_1", "label": "Home", "formatted": "12 Some Road, Bengaluru 560001"}]})
    monkeypatch.setattr(location, "current", lambda: location.DeliveryLocation(
        pincode="560001", lat=12.9767, lon=77.5993, label="Bengaluru"))
    a._delivery_args()
    assert "WARNING" not in capsys.readouterr().out
