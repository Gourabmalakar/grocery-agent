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

import re
from dataclasses import dataclass

_MASS_TO_GRAMS = {"g": 1, "gm": 1, "gram": 1, "grams": 1, "kg": 1000, "kilogram": 1000, "kilograms": 1000}
_VOLUME_TO_ML = {"ml": 1, "millilitre": 1, "millilitres": 1, "l": 1000, "litre": 1000, "litres": 1000,
                  "ltr": 1000}  # Blinkit's real product cards say "1 ltr", not "1 l" — same "gm" lesson as mass
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


def parse_quantity_phrase(text: str) -> tuple[float, str] | None:
    """Extract a leading requested quantity from a free-text item phrase —
    "30 eggs" -> (30, "count"), "2 kg atta" -> (2000, "mass_g"), "500gm
    grapes" -> (500, "mass_g"). Returns None when the phrase carries no
    explicit leading number (plain "milk"), in which case the caller should
    default to a single pack.

    This exists because, before it did, every item was silently bought at
    qty=1 regardless of what number the request actually named — "30 eggs"
    and "eggs" produced an identical cart line. That is a correctness bug
    (bought quantity silently wrong), not a cosmetic one; see worker/run.py's
    use of this function to decide how many packs of the chosen listing to
    buy, and the note in CHANGELOG-style comments there about why it applies
    at first-ask time, not only during revision."""
    t = text.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]*)", t)
    if not m:
        return None
    amount = float(m.group(1))
    unit_word = m.group(2)
    if not unit_word:
        return amount, "count"  # a bare leading number before a noun means a count: "30 eggs"
    base_kind, multiplier = _parse_unit(unit_word)
    if base_kind == "unknown":
        return amount, "count"  # e.g. "1 pack" — treat the named unit itself as one count
    return amount * multiplier, base_kind


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


# Units we can actually normalise, spelled the way product titles spell them.
# Built from the tables above so the two can never drift apart: a unit that
# parse_pack_size() accepts is by construction one _parse_unit() understands.
_TITLE_UNITS = sorted(
    set(_MASS_TO_GRAMS) | set(_VOLUME_TO_ML) | set(_COUNT_UNITS),
    key=len, reverse=True,   # longest-first so "kg" wins over "g"
)
_PACK_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(_TITLE_UNITS) + r")\b", re.IGNORECASE
)

# The same vocabulary, as a membership test for callers that have already
# isolated a candidate unit and need to know whether it is a pack unit at
# all. Exported because a scraper classifying a line as "a size" by shape
# alone will read a delivery estimate ("9 MINS") as 9 units of "mins" — see
# grocery/adapters/blinkit_browser.py::_parse_size.
KNOWN_PACK_UNITS = frozenset(_TITLE_UNITS)


def parse_pack_size(text: str) -> tuple[float, str] | None:
    """Extract a PACK SIZE from a product title: "Amul Gold Homogenised
    Standardised Milk 1 L" -> (1.0, "l"), "Amul Taaza Toned Milk 500 ml" ->
    (500.0, "ml").

    NOT the same job as parse_quantity_phrase(), and conflating them is a
    real bug rather than a style choice. That function reads what a HUMAN
    ASKED FOR ("30 eggs", "2 kg atta"): it anchors on a LEADING number and
    returns a base kind like "volume_ml". This one reads what a MERCHANT IS
    SELLING, where the size sits at the END of a sentence of marketing, and
    must return a pack unit ("ml") because that is what normalise() expects.
    Feeding "volume_ml" back into normalise() yields base_kind="unknown",
    which silently drops the listing out of cross-merchant comparison — it
    does not raise, it just stops being comparable. Caught by
    tests/adapters/test_jiomart_mapping.py.

    Takes the LAST match, because titles trail their size ("... Milk 1 L")
    while any earlier number is usually part of a brand or a variant name.

    Known limitation, deliberately not guessed at: a multipack ("1 L, Pack of
    2") returns the unit size, not the total. Adapters that can read a
    declared pack count should prefer it — see jiomart_api._pack_of, which
    uses JioMart's own net-quantity fields first and only falls back here.
    """
    matches = _PACK_SIZE_RE.findall(text or "")
    if not matches:
        return None
    amount, unit = matches[-1]
    return float(amount), unit.lower()
