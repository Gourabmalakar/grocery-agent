"""Finding the address to build a cart for.

order_efaa1edd24a1 was told "zepto has no saved delivery address... Add one in
the app" by a system looking at the wrong list. UCP's location search returns
BOTH `locations` (places you could collect from) and `buyer_address_locations`
(places the buyer saves to deliver to). Zepto's pickup list is always empty and
says why: "Zepto fulfils every order by delivery from a dark store."

An empty list read as an absent one, so a real address became "you have none".
"""
from __future__ import annotations

import pytest

# Verbatim from the live endpoint, 2026-10-11, address id kept, street removed.
REAL_RESPONSE = {
    "ucp": {"version": "2026-08-25", "status": "success"},
    "locations": [],
    "buyer_address_locations": [
        {"id": "884d3039-3012-4f5b-9307-028465239054", "name": "home",
         "address": {"postal_code": "560001", "address_locality": "Bengaluru"}}
    ],
    "messages": [{"type": "info", "code": "no_pickup_locations_stored",
                  "content": "Zepto fulfils every order by delivery from a dark store, "
                             "so there are no pickup locations."}],
}


class _Client:
    def __init__(self, payload):
        self._payload = payload

    def _call(self, tool, args):
        assert tool == "search_locations"
        return self._payload


def test_the_delivery_address_is_found_despite_an_empty_pickup_list(isolated_data_dir, monkeypatch):
    from console import ordering

    monkeypatch.setattr(ordering.config, "ZEPTO_ADDRESS_ID", "", raising=False)
    assert ordering._address_id(_Client(REAL_RESPONSE), "zepto") == \
        "884d3039-3012-4f5b-9307-028465239054"


def test_genuinely_having_no_address_still_says_so(isolated_data_dir, monkeypatch):
    """The message was right for the case it was written for — it was just
    being reached by the wrong route."""
    from console import ordering

    monkeypatch.setattr(ordering.config, "ZEPTO_ADDRESS_ID", "", raising=False)
    empty = dict(REAL_RESPONSE, buyer_address_locations=[])
    with pytest.raises(ordering.CannotBuild, match="no saved delivery address"):
        ordering._address_id(_Client(empty), "zepto")


def test_several_addresses_refuses_to_choose(isolated_data_dir, monkeypatch):
    """Picking one would deliver somebody's groceries to the wrong flat."""
    from console import ordering

    monkeypatch.setattr(ordering.config, "ZEPTO_ADDRESS_ID", "", raising=False)
    two = dict(REAL_RESPONSE, buyer_address_locations=[
        {"id": "a", "name": "home"}, {"id": "b", "name": "office"}])
    with pytest.raises(ordering.CannotBuild, match="ZEPTO_ADDRESS_ID"):
        ordering._address_id(_Client(two), "zepto")


def test_a_configured_address_wins_without_asking(isolated_data_dir, monkeypatch):
    from console import ordering

    monkeypatch.setattr(ordering.config, "ZEPTO_ADDRESS_ID", "chosen-one", raising=False)

    class _Explode:
        def _call(self, *a, **k):
            raise AssertionError("should not look it up when it is configured")

    assert ordering._address_id(_Explode(), "zepto") == "chosen-one"
