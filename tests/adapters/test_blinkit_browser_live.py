"""
Live-network smoke test for the real Blinkit browser adapter — hits the
actual site. Skipped by default, matching every other suite in this repo's
zero-network-dependency norm; opt in with RUN_LIVE_ADAPTER_TESTS=1 when you
want to confirm the adapter still works against the real site (e.g. after
noticing Blinkit changed its markup).

This is a stopgap for the plan's Suite I ("Live-site regression... recorded
HTML fixtures... refreshed weekly... a fixture diff rather than a failed
order") — that's the more disciplined version of this same idea, not yet
built. In the meantime, grocery.sentinel.checks.check_adapter_health asks a
version of this same question continuously, from real order history rather
than on demand.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_ADAPTER_TESTS") != "1",
    reason="hits the real network; opt in with RUN_LIVE_ADAPTER_TESTS=1",
)


def test_blinkit_search_returns_real_listings():
    from grocery.adapters.blinkit_browser import BlinkitBrowserAdapter
    adapter = BlinkitBrowserAdapter()
    results = adapter.search("milk")
    assert len(results) > 3, "a real, common query should return several real listings"
    for r in results:
        assert r.price_inr > 0
        assert r.pack_unit
        assert r.price_source == "blinkit_live"


def test_blinkit_get_item_matches_a_search_hit():
    from grocery.adapters.blinkit_browser import BlinkitBrowserAdapter
    adapter = BlinkitBrowserAdapter()
    hit = next(r for r in adapter.search("milk") if r.in_stock)
    fetched = adapter.get_item(hit.sku)
    assert fetched.sku == hit.sku
    assert fetched.name == hit.name
    assert fetched.price_inr > 0
