"""
Suite B from the plan: one conformance suite every adapter must pass — mock
included. A new adapter (browser or, later, Swiggy MCP) is "working" when it
goes green against this, which is what keeps adapter correctness from being
re-argued per site.

Two tiers, because Phase 2's browser adapters are deliberately read-only
forever, not "not yet authorized" for writes:

- The full contract (search, get_item, and the build_cart/place_order
  write-gate that must raise PermissionError when authorized=False) — every
  write-capable adapter, MockAdapter today and Swiggy MCP later, must pass
  this in full.
- The read-only contract (search, get_item, and confirming the write
  methods refuse UNCONDITIONALLY, even when authorized=True) — a stricter,
  narrower promise appropriate for a browser adapter that must never place
  a real order, matching grocery/adapters/blinkit_browser.py's module
  docstring.

NB: every grocery/console import below happens INSIDE each test function,
never at module level — the `live` fixture purges grocery.*/console.* from
sys.modules before yielding so each test's imports bind fresh against the
env it just set (see tests/conftest.py's comment on `live`). A module-level
import here would bind to the pre-purge class objects, and objects built by
the post-purge MockAdapter would then fail isinstance() against them despite
being structurally identical — which is exactly the bug this docstring is
here to stop someone from reintroducing.
"""
from __future__ import annotations

import pytest


def _assert_valid_listing(listing) -> None:
    from grocery.adapters.base import Listing
    assert isinstance(listing, Listing)
    assert listing.sku
    assert listing.name
    assert listing.price_inr > 0
    assert listing.pack_qty > 0
    assert listing.pack_unit
    assert listing.price_source, "every listing must carry a price_source — CLAUDE.md rule 3"
    assert listing.asof > 0, "every listing must carry an asof timestamp"


def test_mock_adapter_search_returns_valid_listings(live):
    from grocery.adapters.mock import MockAdapter
    adapter = MockAdapter("freshmart")
    results = adapter.search("milk")
    assert results, "search for a known item must return at least one listing"
    for r in results:
        _assert_valid_listing(r)
        assert r.merchant == "freshmart"


def test_mock_adapter_get_item_matches_search(live):
    from grocery.adapters.mock import MockAdapter
    adapter = MockAdapter("freshmart")
    hit = adapter.search("milk")[0]
    fetched = adapter.get_item(hit.sku)
    _assert_valid_listing(fetched)
    assert fetched.sku == hit.sku
    assert fetched.price_inr == hit.price_inr


def test_mock_adapter_refuses_unauthorized_writes(live):
    from grocery.adapters.mock import MockAdapter
    from grocery.adapters.base import CartLine
    adapter = MockAdapter("freshmart")
    with pytest.raises(PermissionError):
        adapter.build_cart([CartLine(sku="x", qty=1)], authorized=False)
    with pytest.raises(PermissionError):
        adapter.place_order("cart_ref", "payment_ref", authorized=False)


def test_mock_adapter_full_cycle_when_authorized(live):
    from grocery.adapters.mock import MockAdapter
    from grocery.adapters.base import CartLine, Quote
    # dailygrocer, not freshmart: freshmart has a real ₹199 minimum order
    # and a single 1x milk line legitimately can't clear it — that's
    # mockstore behaving correctly, not a bug (see tests/e2e's happy-path
    # comment on the same fact). dailygrocer's ₹0 minimum is what makes a
    # single-item authorized write cycle actually exercisable here.
    adapter = MockAdapter("dailygrocer")
    hit = adapter.search("milk")[0]
    cart_ref = adapter.build_cart([CartLine(sku=hit.sku, qty=1)], authorized=True)
    assert cart_ref
    quote = adapter.quote(cart_ref)
    assert isinstance(quote, Quote)
    assert quote.landed_total_inr > 0
    order_id = adapter.place_order(cart_ref, "pay_test", authorized=True)
    assert order_id


def test_blinkit_browser_adapter_never_writes_even_when_authorized():
    """Doesn't touch the network — this only checks the write-gate, which
    must hold regardless of whether the real site is even reachable. A
    read-only adapter that quietly started accepting authorized=True would
    be a much bigger problem than one that's merely offline."""
    from grocery.adapters.blinkit_browser import BlinkitBrowserAdapter
    from grocery.adapters.base import CartLine
    adapter = BlinkitBrowserAdapter()
    with pytest.raises(NotImplementedError):
        adapter.build_cart([CartLine(sku="x", qty=1)], authorized=True)
    with pytest.raises(NotImplementedError):
        adapter.quote("whatever")
    with pytest.raises(NotImplementedError):
        adapter.place_order("cart", "pay", authorized=True)
