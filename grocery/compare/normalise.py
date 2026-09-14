"""
The real analytical work, not plumbing (see the plan). Grocery comparison is
only meaningful per unit — 500g vs 1kg vs "pack of 2" vs 6x200ml — and every
merchant packs the same logical item differently. This module converts any
(pack_qty, pack_unit) into a canonical per-100-base-unit price so listings
become genuinely comparable.

Canonical bases: grams for mass, millilitres for volume, count (bare) for
countable items (eggs, biscuit packs sold by count).
"""
from __future__ import annotations

from dataclasses import dataclass

_MASS_TO_GRAMS = {"g": 1, "gram": 1, "grams": 1, "kg": 1000, "kilogram": 1000, "kilograms": 1000}
_VOLUME_TO_ML = {"ml": 1, "millilitre": 1, "millilitres": 1, "l": 1000, "litre": 1000, "litres": 1000}
_COUNT_UNITS = {"count", "unit", "units", "pcs", "piece", "pieces"}


def _parse_unit(pack_unit: str) -> tuple[str, float]:
    """Returns (base_kind, multiplier_to_base) for a unit string, handling the
    catalogue's "500ml" (a pack_unit that IS the multi-count size, e.g. mock's
    QuickBasket milk "Pack of 2 x 500ml" represented as pack_qty=2,
    pack_unit='500ml') by splitting a leading numeric size off the unit."""
    u = pack_unit.strip().lower()
    numeric_prefix = ""
    i = 0
    while i < len(u) and (u[i].isdigit() or u[i] == "."):
        numeric_prefix += u[i]
        i += 1
    unit_part = u[i:]
    size_multiplier = float(numeric_prefix) if numeric_prefix else 1.0

    if unit_part in _MASS_TO_GRAMS:
        return "mass_g", size_multiplier * _MASS_TO_GRAMS[unit_part]
    if unit_part in _VOLUME_TO_ML:
        return "volume_ml", size_multiplier * _VOLUME_TO_ML[unit_part]
    if unit_part in _COUNT_UNITS:
        return "count", size_multiplier
    # Unknown unit: treat as a count of 1 so the item is still comparable
    # within itself, but flag it via base_kind="unknown" so callers can
    # surface a warning rather than silently comparing apples to litres.
    return "unknown", size_multiplier


@dataclass
class NormalisedPrice:
    base_kind: str          # "mass_g" | "volume_ml" | "count" | "unknown"
    base_quantity: float    # total grams / ml / count in the pack
    price_per_base_unit: float   # ₹ per gram / per ml / per count
    price_per_100_base: float    # ₹ per 100g / 100ml / 100 count — the display figure


def normalise(pack_qty: float, pack_unit: str, price_inr: float) -> NormalisedPrice:
    base_kind, per_unit_multiplier = _parse_unit(pack_unit)
    total_base = pack_qty * per_unit_multiplier
    if total_base <= 0:
        raise ValueError(f"non-positive normalised quantity from pack_qty={pack_qty}, pack_unit={pack_unit!r}")
    per_base = price_inr / total_base
    return NormalisedPrice(
        base_kind=base_kind, base_quantity=total_base,
        price_per_base_unit=per_base, price_per_100_base=per_base * 100,
    )
