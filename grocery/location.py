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

A pincode and a coordinate are public facts about a place, not credentials.
They are stored in the clear, like the rest of DATA.
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


class InvalidLocation(ValueError):
    pass


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
        """Whether this location carries what a given merchant needs."""
        return self.has_coordinates if merchant == "blinkit" else bool(self.pincode)

    def missing_for(self, merchant: str) -> str:
        if self.supports(merchant):
            return ""
        return (
            f"{merchant} needs a latitude/longitude for the delivery address and none "
            "is set. Add coordinates on the Control page, or this merchant is left out "
            "of the comparison rather than shopped at the wrong place."
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _default() -> DeliveryLocation:
    return DeliveryLocation(
        pincode=str(config.ADAPTER_LOCATION_PINCODE),
        lat=config.ADAPTER_LOCATION_LAT,
        lon=config.ADAPTER_LOCATION_LON,
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
    location = DeliveryLocation(
        pincode=pincode, lat=lat, lon=lon, label=label.strip(), updated_at=time.time()
    )
    _PATH.write_text(json.dumps(location.to_json(), indent=2, sort_keys=True))
    return location
