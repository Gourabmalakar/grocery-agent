"""
Live-network smoke test for the real JioMart adapter — hits the actual
storefront API. Skipped by default, matching every other suite here; opt in
with RUN_LIVE_ADAPTER_TESTS=1.

The mapping itself is covered offline in test_jiomart_mapping.py against a
captured real response. What only a live run can tell you is whether the
pincode gate still works the way it does today and whether the API still
answers at all — the two things that break without any code changing.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_ADAPTER_TESTS") != "1",
    reason="hits the real JioMart API; set RUN_LIVE_ADAPTER_TESTS=1 to run",
)


def test_search_returns_priced_listings(isolated_data_dir):
    from grocery import location
    from grocery.adapters.base import Listing
    from grocery.adapters.jiomart_api import JioMartApiAdapter

    location.set_location(pincode="560001")
    results = JioMartApiAdapter().search("amul gold milk 1l")

    assert results, "JioMart returned nothing for a staple query — the gate or the API moved"
    assert all(isinstance(r, Listing) for r in results)
    assert all(r.price_inr > 0 for r in results), "a listing arrived without a real price"
    assert all(r.price_source == "jiomart_api" and r.asof > 0 for r in results)
    assert any("amul" in r.brand.lower() or "amul" in r.name.lower() for r in results)


def test_pack_sizes_normalise_into_a_comparable_base_kind(isolated_data_dir):
    """A listing whose unit does not map to a base kind silently drops out of
    cross-merchant comparison instead of erroring — which is exactly the bug
    parse_pack_size() was written to fix. Assert it here against real titles,
    because that is where the odd spellings actually live."""
    from grocery import location
    from grocery.adapters.jiomart_api import JioMartApiAdapter
    from grocery.compare.normalise import normalise

    location.set_location(pincode="560001")
    results = JioMartApiAdapter().search("amul milk")

    kinds = {normalise(r.pack_qty, r.pack_unit, r.price_inr).base_kind for r in results}
    assert kinds and "unknown" not in kinds, f"unparseable pack units in {kinds}"


def test_a_malformed_pincode_is_rejected_by_the_api(isolated_data_dir):
    from grocery import location
    from grocery.adapters.jiomart_api import AdapterBlocked, JioMartApiAdapter

    location.set_location(pincode="100000")
    with pytest.raises(AdapterBlocked):
        JioMartApiAdapter().search("milk")


@pytest.mark.parametrize("pincode", ["999999", "111111", "110001"])
def test_an_unserviceable_pincode_is_refused_not_silently_substituted(isolated_data_dir, pincode):
    """THE important live test.

    JioMart answers an unserviceable pincode with HTTP 200 and a full page of
    nationally-shipped marketplace products, not an error. Returning those as
    if they were 10-minute local stock is the worst failure this adapter can
    have: real prices, real sources, real timestamps, entirely wrong basket.

    110001 is in this list deliberately — it is a real central-Delhi pincode
    that JioMart quick commerce does not reach, and it was briefly misread as
    "Delhi has no fresh milk" before the fallback was understood.
    """
    from grocery import location
    from grocery.adapters.jiomart_api import JioMartApiAdapter, NotServiceable

    location.set_location(pincode=pincode)
    with pytest.raises(NotServiceable):
        JioMartApiAdapter().search("amul milk")
