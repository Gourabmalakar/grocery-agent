"""
The delivery location — one place, editable from the Console, read by every
adapter.

WHY THIS EXISTS. Every real merchant gates its catalogue on where you are,
and each wants it in a different shape: Blinkit takes latitude/longitude
cookies, JioMart takes a pincode header, Zepto takes a store resolved from
coordinates. Before this module those lived as ADAPTER_LOCATION_* constants
in .env, which made the delivery address a deployment setting rather than a
thing the person shopping can change — and a wrong one is not a cosmetic
problem. Measured directly: searching "amul milk" on JioMart returns twelve
Amul products in 560001 and none at all in 110001, where the quick-commerce
store simply is not serviceable. The address silently decides what can be
bought and at what price.

So the location is data, not configuration: stored under DATA, changed from
the UI, audit-logged when it changes, and with the .env values acting only
as the first-run default.

LAT/LON IS OPTIONAL, AND ITS ABSENCE IS DISCLOSED, NOT GUESSED. A pincode
does not imply a coordinate. Rather than derive a plausible-looking lat/lon
from a pincode and hand it to Blinkit — which would quietly shop the wrong
neighbourhood while looking perfectly healthy — an adapter that needs
coordinates it does not have reports itself unavailable, and the comparison
says so. Same rule as an unsourced price (CLAUDE.md rule 3).

WHAT THIS MODULE DELIBERATELY DOES NOT HOLD — read this before adding a field.

Gourab's concern, in his words: he does not want to be doxxed, and does not
want an agent or anyone else able to misuse his address. The strongest answer
available is not to encrypt the identifying part but to NOT HOLD IT AT ALL,
because data that does not exist cannot leak, cannot reach a prompt, and cannot
be sent to a merchant by a bug. Nothing in this system needs it yet:

  * There is NO flat number, building name, landmark, or contact number here.
    Those are needed to DELIVER, and delivery is Phase 3/4 — build_cart, quote
    and place_order raise unconditionally on every real adapter. When that
    changes, the full address is a passphrase-sealed thing designed then, with
    the payment gate, and not a field quietly added to this dataclass now.
  * COORDINATES ARE STORED COARSE — rounded to COORD_DECIMALS (~1.1 km) on the
    way in, so a precise point never lands on disk even if one is typed. A
    quick-commerce store catchment is 2-3 km, so ~1 km of fuzz picks the same
    store and the same prices (verified against Blinkit) while being useless
    for finding a doorstep.
  * `label` NEVER LEAVES THIS MACHINE. It is a nickname for a human reading the
    Control page. It used to be sent to blinkit.com as the gr_1_locality cookie,
    which meant a real address typed into that box was transmitted to a merchant
    on every search. Adapters get shopping_locus(), which has no label field at
    all — not "they are asked not to read it", they cannot.

ADAPTERS CALL shopping_locus(), NEVER current(). current() returns the human's
view, label included, and belongs to the Console. That split is what makes
CLAUDE.md rule 1 — "the worker has no delivery address" — true rather than
aspirational: a pincode plus a kilometre-scale point is a shopping area, not an
address. tests/unit/test_location_privacy.py enforces all of this.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from grocery import config

_PATH = config.DATA / "location.json"
_PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")  # Indian PIN: 6 digits, never leading zero

# 2 decimal places is ~1.1 km at this latitude. Chosen as the coarsest fuzz that
# still resolves the same quick-commerce store: catchments run 2-3 km, so the
# prices are unchanged while the point no longer identifies a building. Raising
# this number makes prices less reliable; lowering it makes the stored point
# closer to a doorstep. Do not lower it without a reason better than precision
# for its own sake.
COORD_DECIMALS = 2


def _coarsen(value: float | None) -> float | None:
    return None if value is None else round(float(value), COORD_DECIMALS)


# Merchants whose catalogue cannot be reached with a pincode alone. Kept as
# data rather than an `if merchant == ...` chain because it is now two and will
# be more, and because missing_for() has to agree with supports() exactly.
_NEEDS_COORDINATES = frozenset({"blinkit", "swiggy"})


class InvalidLocation(ValueError):
    pass


@dataclass(frozen=True)
class ShoppingLocus:
    """WHERE TO SHOP — the only location view any adapter is given.

    Has no label, no street, no contact and a coordinate already rounded to
    ~1 km. Everything a merchant needs to resolve a store and price a basket,
    and nothing that identifies a home. Frozen so a caller cannot decorate it
    with something more identifying on the way past.
    """
    pincode: str
    lat: float | None = None
    lon: float | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None

    def supports(self, merchant: str) -> bool:
        if merchant in _NEEDS_COORDINATES:
            return self.has_coordinates
        return bool(self.pincode)

    def missing_for(self, merchant: str) -> str:
        return "" if self.supports(merchant) else _missing_coordinates_message(merchant)


@dataclass
class DeliveryLocation:
    pincode: str
    lat: float | None = None
    lon: float | None = None
    label: str = ""
    updated_at: float = 0.0

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None

    def supports(self, merchant: str) -> bool:
        """Whether this location carries what a given merchant needs.

        Blinkit takes coordinates as cookies; Swiggy Instamart takes them as
        "guest mode" arguments to search_products. JioMart needs only the
        pincode. Mock merchants need nothing.
        """
        if merchant in _NEEDS_COORDINATES:
            return self.has_coordinates
        return bool(self.pincode)

    def missing_for(self, merchant: str) -> str:
        return "" if self.supports(merchant) else _missing_coordinates_message(merchant)

    def shopping_locus(self) -> ShoppingLocus:
        """The narrowed view adapters get. Drops the label by construction."""
        return ShoppingLocus(pincode=self.pincode, lat=_coarsen(self.lat),
                             lon=_coarsen(self.lon))

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _missing_coordinates_message(merchant: str) -> str:
    return (
        f"{merchant} needs a latitude/longitude for the delivery area and none is set. "
        "Add coordinates on the Control page — they are stored rounded to about a "
        "kilometre — or this merchant is left out of the comparison rather than "
        "shopped at the wrong place."
    )


def shopping_locus() -> ShoppingLocus:
    """THE ADAPTER ENTRY POINT. Every real adapter calls this and not current().

    Exists so that "an adapter cannot see the delivery label" is a fact about
    the type it is handed, rather than a rule someone has to remember.
    """
    return current().shopping_locus()


def _default() -> DeliveryLocation:
    return DeliveryLocation(
        pincode=str(config.ADAPTER_LOCATION_PINCODE),
        lat=_coarsen(config.ADAPTER_LOCATION_LAT),
        lon=_coarsen(config.ADAPTER_LOCATION_LON),
        label=config.ADAPTER_LOCATION_LABEL,
        updated_at=0.0,
    )


def current() -> DeliveryLocation:
    """The address in force. Falls back to the .env defaults until someone
    sets one in the UI — never raises, because every adapter calls this."""
    if not _PATH.exists():
        return _default()
    try:
        data = json.loads(_PATH.read_text())
        return DeliveryLocation(
            pincode=str(data.get("pincode") or _default().pincode),
            lat=data.get("lat"),
            lon=data.get("lon"),
            label=str(data.get("label") or ""),
            updated_at=float(data.get("updated_at") or 0.0),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # A corrupt location file must not take the console down; the
        # default is wrong-but-visible, which beats a 500 on every page.
        return _default()


def set_location(*, pincode: str, lat: float | None = None, lon: float | None = None,
                 label: str = "") -> DeliveryLocation:
    pincode = (pincode or "").strip()
    if not _PINCODE_RE.match(pincode):
        raise InvalidLocation(f"{pincode!r} is not a 6-digit Indian pincode")
    if (lat is None) != (lon is None):
        raise InvalidLocation("give both latitude and longitude, or neither")
    if lat is not None:
        # India's bounding box, roughly. A transposed lat/lon (a classic
        # slip) lands outside it and is caught here rather than becoming a
        # silently empty Blinkit catalogue.
        if not (6.0 <= lat <= 37.5) or not (68.0 <= lon <= 97.5):
            raise InvalidLocation(
                f"({lat}, {lon}) is outside India — check they are not the wrong way round"
            )
    # COARSENED BEFORE IT IS STORED, not on the way out. A precise coordinate
    # that reaches disk has already been written somewhere it can be read from;
    # rounding at the point of entry means the file never holds one. Validation
    # above runs on the value as typed, so a transposed lat/lon is still caught.
    location = DeliveryLocation(
        pincode=pincode, lat=_coarsen(lat), lon=_coarsen(lon),
        label=label.strip(), updated_at=time.time(),
    )
    _PATH.write_text(json.dumps(location.to_json(), indent=2, sort_keys=True))
    _PATH.chmod(0o600)  # same treatment as the token vault
    return location
