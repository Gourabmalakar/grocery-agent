"""
Offline regression tests for Blinkit's search-card parser.

These exist because of a real failure. The only Blinkit coverage in the
repo was tests/adapters/test_blinkit_browser_live.py, which is opt-in
behind RUN_LIVE_ADAPTER_TESTS=1 — so when Blinkit dropped the per-card
delivery-ETA line ("9 MINS"), the parser's positional indexing (name at
lines[1], size at lines[2]) stopped matching every card, search() returned
an empty list with no exception, and nothing in the default suite noticed.
A live smoke test you have to remember to run is not a regression test.

Every card string below is REAL innerText captured from blinkit.com at
pincode 560001, with the pre- and post-change shapes kept side by side so
the parser has to handle both — the ETA line may well come back.

Imports are inside the test functions, matching the convention the rest of
tests/ follows.
"""
from __future__ import annotations


# Captured 2026-09-21 — the current shape, with no ETA line.
CARD_PLAIN = "Amul Gold Milk\n1 ltr\n₹83\nADD"
CARD_DISCOUNTED = (
    "16% OFF\nCountry Delight 25 g High Protein Milk\n450 ml\n₹49\n₹59\nADD"
)
CARD_MULTIPACK = "Amul Taaza Toned Milk - Pack of 2\n2 x 200 ml\n₹34\nADD"

# Captured 2026-09-20 — the shape that used to be the only one handled.
CARD_WITH_ETA = "9 MINS\nAmul Gold Milk\n1 ltr\n₹83\nADD"
CARD_WITH_ETA_AND_DISCOUNT = (
    "16% OFF\n9 MINS\nCountry Delight 25 g High Protein Milk\n450 ml\n₹49\n₹59\nADD"
)


def test_parses_a_card_with_no_delivery_estimate():
    """The shape that broke it: name, size, price, cta — nothing above the name."""
    from grocery.adapters.blinkit_browser import _parse_card_text

    listing = _parse_card_text("179", CARD_PLAIN)
    assert listing is not None, "a plain in-stock card must parse"
    assert listing.name == "Amul Gold Milk"
    assert (listing.pack_qty, listing.pack_unit) == (1.0, "ltr")
    assert listing.price_inr == 83.0
    assert listing.in_stock is True
    assert listing.sku == "179"
    assert listing.price_source == "blinkit_live"


def test_still_parses_a_card_that_has_a_delivery_estimate():
    """Blinkit may put the ETA back. Both shapes must work, which is the
    whole reason the parser anchors on the size line instead of counting."""
    from grocery.adapters.blinkit_browser import _parse_card_text

    listing = _parse_card_text("179", CARD_WITH_ETA)
    assert listing is not None
    assert listing.name == "Amul Gold Milk"
    assert (listing.pack_qty, listing.pack_unit) == (1.0, "ltr")
    assert listing.price_inr == 83.0


def test_a_delivery_estimate_is_never_read_as_a_pack_size():
    """"9 MINS" has exactly the shape of a pack size. Accepting it would
    make the ETA the size and the "% OFF" line the product name — wrong
    data rather than no data, which is the worse failure."""
    from grocery.adapters.blinkit_browser import _parse_size

    assert _parse_size("9 MINS") is None
    assert _parse_size("12 MINS") is None
    assert _parse_size("1 ltr") == (1.0, "ltr")
    assert _parse_size("450 ml") == (450.0, "ml")
    assert _parse_size("500 g") == (500.0, "g")


def test_discount_line_does_not_shift_the_name():
    """The name here contains "25 g", so a looser size-finder would read the
    NAME as the size line and the "16% OFF" banner as the product name."""
    from grocery.adapters.blinkit_browser import _parse_card_text

    listing = _parse_card_text("735586", CARD_DISCOUNTED)
    assert listing is not None
    assert listing.name == "Country Delight 25 g High Protein Milk"
    assert (listing.pack_qty, listing.pack_unit) == (450.0, "ml")
    assert listing.price_inr == 49.0, "the selling price, not the ₹59 struck-through MRP"


def test_discount_and_eta_together():
    from grocery.adapters.blinkit_browser import _parse_card_text

    listing = _parse_card_text("735586", CARD_WITH_ETA_AND_DISCOUNT)
    assert listing is not None
    assert listing.name == "Country Delight 25 g High Protein Milk"
    assert (listing.pack_qty, listing.pack_unit) == (450.0, "ml")
    assert listing.price_inr == 49.0


def test_a_multipack_is_dropped_rather_than_guessed():
    """"2 x 200 ml" is a pack size we cannot normalise into a single
    (qty, unit) pair. Dropping the listing keeps it out of the comparison;
    guessing 200 ml or 400 ml would put a wrong unit price into a ranking.
    If multipacks ever matter, the fix is to teach normalise() about them —
    not to loosen this parser."""
    from grocery.adapters.blinkit_browser import _parse_card_text

    assert _parse_card_text("34778", CARD_MULTIPACK) is None


def test_out_of_stock_cards_are_marked_not_in_stock():
    from grocery.adapters.blinkit_browser import _parse_card_text

    listing = _parse_card_text("999", "Amul Gold Milk\n1 ltr\n₹83\nSOLD OUT")
    assert listing is not None
    assert listing.in_stock is False


def test_junk_returns_none_rather_than_a_listing():
    from grocery.adapters.blinkit_browser import _parse_card_text

    assert _parse_card_text("1", "") is None
    assert _parse_card_text("1", "My Cart") is None
    assert _parse_card_text("1", "1 ltr\n₹83") is None, "no line above the size to be a name"
    assert _parse_card_text("1", "Amul Gold Milk\n1 ltr\nADD") is None, "no price"


def test_every_pack_unit_the_parser_accepts_can_be_normalised():
    """The guard against the two vocabularies drifting apart: a unit this
    parser lets through must be one normalise() can actually compare."""
    from grocery.adapters.blinkit_browser import KNOWN_PACK_UNITS
    from grocery.compare.normalise import normalise

    for unit in KNOWN_PACK_UNITS:
        assert normalise(1.0, unit, 100.0).base_kind != "unknown", unit
