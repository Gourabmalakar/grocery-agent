"""
The delivery address decides what is on the shelves. Measured on JioMart:
"amul milk" returns twelve Amul products in 560001 and none in 110001, where
quick commerce is not serviceable. So these are not cosmetic validations —
a bad address produces an empty catalogue that looks exactly like a broken
adapter, and a silently-defaulted one produces real prices for the wrong
neighbourhood.

Imports are inside each test because `isolated_data_dir` purges grocery.*
from sys.modules and reloads config; a module-level import would bind to the
pre-purge module and write to the real data directory.
"""
from __future__ import annotations

import json

import pytest


def test_defaults_to_config_when_nothing_is_set(isolated_data_dir):
    from grocery import location

    here = location.current()
    assert here.pincode == isolated_data_dir.ADAPTER_LOCATION_PINCODE
    assert here.updated_at == 0.0  # never explicitly chosen by a human


def test_round_trips_a_saved_location(isolated_data_dir):
    from grocery import location

    location.set_location(pincode="560001", lat=12.9767, lon=77.5993, label="Home")
    here = location.current()
    assert (here.pincode, here.lat, here.lon, here.label) == ("560001", 12.9767, 77.5993, "Home")
    assert here.updated_at > 0


@pytest.mark.parametrize("bad", ["", "99", "5600011", "abcdef", "056001"])
def test_rejects_anything_that_is_not_an_indian_pincode(isolated_data_dir, bad):
    from grocery import location

    with pytest.raises(location.InvalidLocation):
        location.set_location(pincode=bad)


def test_rejects_half_a_coordinate(isolated_data_dir):
    from grocery import location

    with pytest.raises(location.InvalidLocation):
        location.set_location(pincode="560001", lat=12.9767)


def test_rejects_transposed_coordinates(isolated_data_dir):
    """(77.59, 12.97) is Bengaluru with lat and lon swapped — a classic slip
    that would otherwise land the shopper in the Indian Ocean and return an
    empty catalogue with no error anywhere."""
    from grocery import location

    with pytest.raises(location.InvalidLocation, match="outside India"):
        location.set_location(pincode="560001", lat=77.5993, lon=12.9767)


def test_pincode_only_is_valid_but_does_not_serve_blinkit(isolated_data_dir):
    from grocery import location

    here = location.set_location(pincode="560001")
    assert here.supports("jiomart") and here.supports("zepto")
    assert not here.supports("blinkit")
    # The absence has to be explainable, not just false — this string is what
    # the comparison shows instead of quietly dropping a merchant.
    assert "latitude" in here.missing_for("blinkit")
    assert here.missing_for("jiomart") == ""


def test_a_corrupt_location_file_falls_back_instead_of_exploding(isolated_data_dir):
    """Every page read goes through current(). A half-written file must not
    500 the console — wrong-but-visible beats down."""
    from grocery import location

    (isolated_data_dir.DATA / "location.json").write_text("{not json")
    assert location.current().pincode == isolated_data_dir.ADAPTER_LOCATION_PINCODE


def test_saved_file_is_plain_readable_json(isolated_data_dir):
    """A pincode is a public fact about a place, not a credential; it is
    stored in the clear like the rest of DATA, and that is deliberate."""
    from grocery import location

    location.set_location(pincode="560001", lat=12.9767, lon=77.5993)
    data = json.loads((isolated_data_dir.DATA / "location.json").read_text())
    assert data["pincode"] == "560001"
