"""The address-privacy contract, enforced rather than documented.

Gourab's requirement: he must not be doxxable through this system, and neither
an agent nor anyone with access to the machine should be able to misuse his
address. grocery/location.py's answer is to hold as little as possible and to
narrow what adapters can see to a type that has no address in it. These tests
are what stops that decaying — every one of them corresponds to something that
was either true and wrong, or easy to reintroduce.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


# ---- what the narrowed view cannot carry -------------------------------

def test_shopping_locus_has_no_address_fields(isolated_data_dir):
    """Not "adapters are asked not to read the label" — there is no label."""
    from grocery.location import ShoppingLocus

    fields = set(ShoppingLocus.__dataclass_fields__)
    assert fields == {"pincode", "lat", "lon"}
    for forbidden in ("label", "street", "flat", "building", "landmark",
                      "contact", "phone", "name"):
        assert forbidden not in fields


def test_shopping_locus_is_frozen(isolated_data_dir):
    """So nothing can decorate it with something identifying on the way past."""
    from grocery.location import ShoppingLocus

    locus = ShoppingLocus(pincode="560001", lat=12.98, lon=77.6)
    with pytest.raises(Exception):
        locus.pincode = "110001"  # type: ignore[misc]


def test_delivery_location_holds_no_flat_or_contact_details(isolated_data_dir):
    """Delivery needs these; delivery is Phase 3/4 and blocked by design. Data
    we do not hold cannot leak, so the field must not exist until the sealed
    design that carries it exists too."""
    from grocery.location import DeliveryLocation

    for forbidden in ("flat", "flat_details", "building", "building_name",
                      "landmark", "contact_name", "contact_number", "phone"):
        assert forbidden not in DeliveryLocation.__dataclass_fields__


# ---- coarse coordinates ------------------------------------------------

@pytest.mark.parametrize("lat,lon,expected", [
    (12.976712345, 77.599312345, (12.98, 77.6)),
    (12.9749, 77.6051, (12.97, 77.61)),
])
def test_a_precise_coordinate_never_reaches_disk(isolated_data_dir, lat, lon, expected):
    from grocery import location

    location.set_location(pincode="560001", lat=lat, lon=lon, label="Home")
    on_disk = json.loads((isolated_data_dir.DATA / "location.json").read_text())
    assert (on_disk["lat"], on_disk["lon"]) == expected
    # and the precise value is nowhere in the file, not even in another field
    assert str(lat) not in (isolated_data_dir.DATA / "location.json").read_text()


def test_the_locus_is_coarse_even_if_the_file_was_hand_edited(isolated_data_dir):
    """Coarsening on write is the primary defence; coarsening on read covers a
    file someone edited by hand or a stale one written before this rule."""
    from grocery import location

    (isolated_data_dir.DATA).mkdir(parents=True, exist_ok=True)
    (isolated_data_dir.DATA / "location.json").write_text(json.dumps(
        {"pincode": "560001", "lat": 12.976712345, "lon": 77.599312345,
         "label": "Flat 402", "updated_at": 1.0}))
    locus = location.shopping_locus()
    assert (locus.lat, locus.lon) == (12.98, 77.6)


def test_location_file_is_not_world_readable(isolated_data_dir):
    from grocery import location

    location.set_location(pincode="560001", lat=12.98, lon=77.6, label="Home")
    mode = (isolated_data_dir.DATA / "location.json").stat().st_mode & 0o777
    assert mode == 0o600, f"location.json is mode {oct(mode)}"


# ---- the label never leaves the machine --------------------------------

def test_blinkit_sends_the_pincode_not_the_label(isolated_data_dir):
    """THE LEAK THIS FILE EXISTS FOR. gr_1_locality carried `label or pincode`,
    so an address typed into the Control page was sent to blinkit.com as a
    cookie on every search."""
    from grocery import location
    from grocery.adapters import blinkit_browser

    secret = "Flat 402 Prestige Somewhere Hoodi"
    location.set_location(pincode="560001", lat=12.98, lon=77.6, label=secret)
    cookies = blinkit_browser._location_cookies()

    blob = json.dumps(cookies)
    assert secret not in blob
    assert "Flat" not in blob and "Prestige" not in blob
    locality = next(c for c in cookies if c["name"] == "gr_1_locality")
    assert locality["value"] == "560001"


def test_no_adapter_reads_the_human_view(isolated_data_dir):
    """Static check across every adapter: location.current() returns the label
    and belongs to the Console. An adapter that wants a location calls
    shopping_locus(). Asserted by walking the AST rather than grepping, so a
    mention inside a comment or docstring does not fail the test and a real call
    cannot hide behind one."""
    offenders = []
    for path in sorted((_REPO / "grocery" / "adapters").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "current"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "location"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"adapters calling location.current(): {offenders}"


def test_the_worker_does_not_read_the_human_view(isolated_data_dir):
    """CLAUDE.md rule 1: the worker holds no delivery address. It used to call
    location.current()."""
    offenders = []
    for path in sorted((_REPO / "worker").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "current"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "location"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"worker modules calling location.current(): {offenders}"


# ---- the address never appears in anything the system says -------------

def test_the_address_appears_in_no_narrative_or_decision_record(isolated_data_dir):
    """A full order's worth of output — the narrative the human reads, every
    decision record, and the audit log — scanned for the label and the
    coordinate. This is where a privacy leak would actually surface, and it is
    the criterion the eval suite calls privacy_preservation."""
    from grocery import location
    from grocery.audit import decisions as decision_log, log as audit_log

    secret_label = "Flat 402 Prestige Lakeside Hoodi"
    location.set_location(pincode="560001", lat=12.9767, lon=77.5993, label=secret_label)

    from grocery import narrator
    cart = {
        "merchant": "jiomart", "landed_total_inr": 69.0,
        "fees": {"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        "line_items": [{"name": "Amul Taaza Toned Milk", "brand": "Amul", "qty": 1,
                        "line_total_inr": 52.0}],
    }
    comparison = [{"merchant": "jiomart", "landed_total_inr": 69.0, "rank": 1},
                  {"merchant": "blinkit", "landed_total_inr": 73.0, "rank": 2}]
    text = narrator.narrate_proposal(cart=cart, comparison=comparison,
                                     max_total_inr=500, fees_known=False,
                                     missing_items=[], not_found_items=[])

    haystacks = [text]
    haystacks += [json.dumps(r) for r in decision_log.read_all()]
    haystacks += [json.dumps(e) for e in audit_log.read_all()]

    for hay in haystacks:
        assert secret_label not in hay
        assert "Flat 402" not in hay
        assert "12.9767" not in hay, "a precise coordinate reached an output"
