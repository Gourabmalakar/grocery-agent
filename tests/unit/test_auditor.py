"""The cart auditor, tested against the order that caused it.

order_6a45f91aaa2e asked for "english oven whole wheat bread, pilsbury
multigrain atta 5kg, 30 eggs, 500g oranges, 250 gm mausambi" and was offered
salt, an onion and a potato. Every fixture below is a real (request, product)
pair from that order's decision log or from the live merchant responses in the
same session — none is invented, because the bugs this module has already had
were all cases where invented examples looked fine.
"""
from __future__ import annotations

import pytest

from worker import auditor
from worker.auditor import MISMATCH, OK, SUSPECT


# ---- what actually went wrong ------------------------------------------

@pytest.mark.parametrize("query,name,brand", [
    ("30 eggs", "Tata Iodised Salt 1 kg", "Tata"),
    ("500g orhanges", "Onion (Approx. 900 g - 1000 g)", "reliance"),
    ("250 gm mausambi", "Potato (Approx. 900 g - 1000 g)", "reliance"),
])
def test_the_three_wrong_picks_are_rejected(query, name, brand):
    assert auditor.satisfies(query, name=name, brand=brand).verdict == MISMATCH


@pytest.mark.parametrize("query,name", [
    ("30 eggs", "Fresh Eggs - Fresh Export Eggs Pack of 30"),
    ("30 eggs", "Nutri Hatch White Eggs"),
    ("500g orhanges", "Nagpur Orange (Kittale Hannu)"),
    ("250 gm mausambi", "Daily Juice Sweet Lime (Mosambi)"),
])
def test_the_right_products_from_the_same_search_are_accepted(query, name):
    """The correct answers were in the results all along, at other merchants."""
    assert auditor.satisfies(query, name=name).verdict != MISMATCH


def test_a_named_brand_being_swapped_is_a_rejection_not_a_note():
    """'english oven whole wheat bread' answered by Mr. Bakewell matches on the
    word bread and nothing else. Treating that as a mild note is how a named
    brand gets quietly substituted."""
    v = auditor.satisfies("Get me english oven whole wheat bread",
                          name="Mr. Bakewell Premium Multigrain Bread 400 g", brand="Mr Bakewell")
    assert v.verdict == MISMATCH


def test_the_right_brand_in_the_wrong_variant_is_kept_but_flagged():
    """English Oven WHITE bread against a request for whole wheat is the right
    brand and the wrong loaf. Rejecting it would leave the shelf empty when a
    near-miss is genuinely useful; passing it silently is the complaint."""
    v = auditor.satisfies("english oven whole wheat bread",
                          name="English Oven Premium White Bread 350 g", brand="English Oven")
    assert v.verdict == SUSPECT
    assert "whole" in v.why and "wheat" in v.why


def test_curd_does_not_answer_a_request_for_milk():
    """Same brand, same aisle, different product — from an earlier real run."""
    assert auditor.satisfies("amul milk 1l", name="Amul Creamy Tasty Curd 900 g",
                             brand="amul").verdict == MISMATCH


# ---- the parsing that had to be right first ----------------------------

def test_a_count_does_not_eat_the_noun():
    """An earlier quantity stripper reduced '30 eggs' to a stray 's', leaving the
    request with no describable item and every product passing."""
    assert auditor.content_words("30 eggs") == ["eggs"]


@pytest.mark.parametrize("text,expected", [
    ("Get me english oven whole wheat bread", ["english", "oven", "whole", "wheat", "bread"]),
    ("500g orhanges", ["orhanges"]),
    ("250 gm mausambi", ["mausambi"]),
    ("pilsbury multigrain atta 5kg", ["pilsbury", "multigrain", "atta"]),
])
def test_content_words_keeps_identity_and_drops_quantity_and_politeness(text, expected):
    assert auditor.content_words(text) == expected


def test_a_typo_still_matches():
    """'orhanges' is what was actually typed."""
    assert auditor.satisfies("500g orhanges", name="Fresh Oranges 1 kg").verdict == OK


def test_a_transliteration_still_matches():
    """mausambi / mosambi / sweet lime are one fruit with three names."""
    assert auditor.satisfies("250 gm mausambi", name="Organically Grown Sweet Lime").verdict == OK
    assert auditor.satisfies("250 gm mausambi", name="Daily Juice Sweet Lime (Mosambi)").verdict == OK


def test_the_merchants_own_category_counts_as_evidence():
    """'Britannia Good Day' is biscuits and says so nowhere in its name."""
    assert auditor.satisfies("biscuits", name="Britannia Good Day 500g", brand="Britannia",
                             category="snacks biscuits britannia biscuits").verdict == OK
    assert auditor.satisfies("biscuits", name="Britannia Good Day 500g",
                             brand="Britannia").verdict == MISMATCH


# ---- price sanity ------------------------------------------------------

def test_an_object_that_shares_a_word_with_the_food_is_caught_by_price():
    """'Cetlo 18 Grid Egg Storage Container' contains 'Egg' and passes every
    string test. Two other merchants answered '30 eggs' for ₹243 and ₹312."""
    flags = auditor.price_outliers(
        line_items=[{"sku": "x", "name": "Cetlo 18 Grid Egg Storage Container with Lid",
                     "line_total_inr": 6570.0}],
        line_queries={"x": "30 eggs"},
        peer_prices={"30 eggs": [243.0, 312.0]},
    )
    assert len(flags) == 1
    assert "27x" in flags[0].why


def test_a_normal_price_spread_is_not_flagged():
    """Quick-commerce prices differ a lot without meaning anything."""
    assert auditor.price_outliers(
        line_items=[{"sku": "x", "name": "Nutri Hatch White Eggs", "line_total_inr": 312.0}],
        line_queries={"x": "30 eggs"}, peer_prices={"30 eggs": [243.0]},
    ) == []


def test_one_merchant_alone_produces_no_price_opinion():
    """The check is a property of the comparison. With nothing to compare it
    says nothing, rather than inventing a threshold."""
    assert auditor.price_outliers(
        line_items=[{"sku": "x", "name": "Anything", "line_total_inr": 9999.0}],
        line_queries={"x": "30 eggs"}, peer_prices={},
    ) == []


# ---- the whole-cart verdict --------------------------------------------

def test_audit_reports_missing_items_and_mismatches_with_actionable_words():
    report = auditor.audit_cart(
        requested_items=["30 eggs", "500g orhanges", "250 gm mausambi"],
        line_items=[
            {"sku": "a", "name": "Tata Iodised Salt 1 kg", "brand": "Tata"},
            {"sku": "b", "name": "Nagpur Orange", "brand": ""},
        ],
        line_queries={"a": "30 eggs", "b": "500g orhanges"},
    )
    assert not report.ok
    assert [v.name for v in report.mismatches] == ["Tata Iodised Salt 1 kg"]
    assert report.missing == ["30 eggs", "250 gm mausambi"]

    feedback = " ".join(report.feedback())
    assert "does not answer" in feedback and "'30 eggs'" in feedback
    assert "250 gm mausambi" in feedback


def test_a_cart_that_answers_everything_passes_clean():
    report = auditor.audit_cart(
        requested_items=["30 eggs", "250 gm mausambi"],
        line_items=[
            {"sku": "a", "name": "Fresh Export Eggs Pack of 30", "brand": ""},
            {"sku": "b", "name": "Daily Juice Sweet Lime (Mosambi)", "brand": ""},
        ],
        line_queries={"a": "30 eggs", "b": "250 gm mausambi"},
    )
    assert report.ok
    assert report.feedback() == []


# ---- buttermilk is not butter, and is not milk -------------------------

@pytest.mark.parametrize("query", ["amul butter 250g", "milk 1l"])
def test_buttermilk_answers_neither_butter_nor_milk(query):
    """Gourab asked for amul butter across three revisions and was sold "Amul
    Masti Spiced Salted Buttermilk". The prefix rule in _close matched "butter"
    against "buttermilk" because both were long enough — the guard was word
    LENGTH when it needed to be length DIFFERENCE. This is also the exact
    failure the build plan predicted before any of it was written: "the
    cheapest-per-100ml picker will confidently buy buttermilk"."""
    verdict = auditor.satisfies(query, name="Amul Masti Spiced Salted Buttermilk",
                                brand="Amul")
    assert verdict.verdict == MISMATCH


@pytest.mark.parametrize("a,b,close", [
    ("egg", "eggs", True),
    ("orange", "oranges", True),
    ("tomato", "tomatoes", True),
    ("butter", "buttermilk", False),
    ("milk", "buttermilk", False),
    ("milk", "milkshake", False),
])
def test_prefix_matching_covers_inflection_and_stops_there(a, b, close):
    """Two characters is about what an English plural costs. Beyond that the
    extra letters are usually a different product."""
    assert auditor._close(a, b) is close


def test_real_butter_is_still_accepted():
    """The point is not to reject things — the shelf really does have butter."""
    for name in ("Amul Pasteurised Butter 100 g", "Amul Butter 500 g"):
        assert auditor.satisfies("amul butter 250g", name=name, brand="Amul").verdict == OK
