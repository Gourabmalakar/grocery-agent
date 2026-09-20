"""
Mapping tests for the JioMart adapter, run against a REAL captured response
(tests/fixtures/jiomart_search_amul_gold_milk.json, Bengaluru 560001) rather
than a hand-written dict. A fixture invented to match the parser proves only
that the parser matches itself; this one was captured from the live API, so
when JioMart changes shape the fixture can be re-captured and the diff is
the story.

No network: these exercise the mapping, not the site. The live smoke test
lives in test_jiomart_live.py behind RUN_LIVE_ADAPTER_TESTS=1.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "jiomart_search_amul_gold_milk.json"


def _items():
    return json.loads(_FIXTURE.read_text())["items"]


def test_maps_a_real_row_to_a_listing(isolated_data_dir):
    from grocery.adapters.jiomart_api import _listing_from

    row = next(i for i in _items() if i["name"] == "Amul Gold Homogenised Standardised Milk 1 L")
    listing = _listing_from(row)

    assert listing.merchant == "jiomart"
    assert listing.sku == "7528514"
    assert listing.brand == "amul"
    assert listing.price_inr == 82.0           # price.effective.min, the amount charged
    assert (listing.pack_qty, listing.pack_unit) == (1.0, "l")
    assert listing.in_stock is True
    assert listing.price_source == "jiomart_api"
    assert listing.asof > 0


def test_takes_the_effective_price_not_the_marked_one(isolated_data_dir):
    """marked is MRP (83), effective is what you pay (82). Comparing on MRP
    would rank every merchant by a number nobody is charged."""
    from grocery.adapters.jiomart_api import _listing_from

    row = next(i for i in _items() if i["name"] == "Amul Gold Homogenised Standardised Milk 1 L")
    assert row["price"]["marked"]["min"] == 83
    assert _listing_from(row).price_inr == 82.0


def test_prefers_declared_pack_size_over_parsing_the_title(isolated_data_dir):
    """JioMart declares net-quantity-value/unit. A declared quantity beats
    one inferred from a product name, which is how "Amul Kool Gold Classic
    Badam Flavoured Milk 180 ml, Pack of 6" gets mis-sized."""
    from grocery.adapters.jiomart_api import _listing_from

    row = dict(next(iter(_items())))
    row["name"] = "Something With 500 ml In The Title"
    row["net-quantity-value"], row["net-quantity-unit"] = 2.0, "kg"
    listing = _listing_from(row)
    assert (listing.pack_qty, listing.pack_unit) == (2.0, "kg")


def test_falls_back_to_the_title_when_quantity_is_not_declared(isolated_data_dir):
    from grocery.adapters.jiomart_api import _listing_from

    row = {k: v for k, v in next(iter(_items())).items()
           if k not in ("net-quantity-value", "net-quantity-unit")}
    row["name"] = "Amul Taaza Toned Milk 500 ml"
    listing = _listing_from(row)
    assert (listing.pack_qty, listing.pack_unit) == (500.0, "ml")


def test_a_row_with_no_price_is_dropped_not_zeroed(isolated_data_dir):
    """An unsourced price is worse than a missing one (CLAUDE.md rule 3).
    A row we cannot price must not arrive downstream as free."""
    from grocery.adapters.jiomart_api import _listing_from

    row = dict(next(iter(_items())))
    row["price"] = {}
    assert _listing_from(row) is None


def test_never_carries_merchant_prose(isolated_data_dir):
    """raw_description is untrusted merchant text (CLAUDE.md rule 2). The
    safest place for it is nowhere, so it never reaches a planner prompt or
    a judge prompt."""
    from grocery.adapters.jiomart_api import _listing_from

    assert _listing_from(next(iter(_items()))).raw_description == ""


@pytest.mark.parametrize("method,args", [
    ("build_cart", ([],)),
    ("place_order", ("cart_1", "pay_1")),
])
def test_write_methods_raise_even_when_authorized(isolated_data_dir, method, args):
    from grocery.adapters.jiomart_api import JioMartApiAdapter

    adapter = JioMartApiAdapter()
    with pytest.raises(NotImplementedError):
        getattr(adapter, method)(*args, authorized=True)


def test_quote_raises(isolated_data_dir):
    from grocery.adapters.jiomart_api import JioMartApiAdapter

    with pytest.raises(NotImplementedError):
        JioMartApiAdapter().quote("cart_1")


def test_location_reaches_the_request_header(isolated_data_dir):
    """The pincode gate is a header. If this stops being wired, every search
    comes back 400 'invalid pincode' — better to catch it here."""
    from grocery import location
    from grocery.adapters.jiomart_api import _headers

    location.set_location(pincode="400001")
    assert json.loads(_headers()["x-location-detail"])["pincode"] == "400001"
