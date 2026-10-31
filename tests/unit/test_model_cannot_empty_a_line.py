"""When the model refuses a whole set of candidates, the human must learn why.

This file changed its mind once, and the record of that is the useful part.

Gourab asked for butter across three revisions and it never arrived. The store
had "Amul Pasteurised Butter 100 g"; the model answered "none match the
requested 250g size" and the item vanished. The first fix was to make that
refusal advisory — keep the closest match anyway. That was wrong, and the very
next live run proved it: with the refusal downgraded, the basket accepted "Amul
Fresh Butter Cookies" for "amul butter". Biscuits.

The real defect was never that the model could refuse. Measured against the live
catalogue it refuses correctly in both directions — it picks the 100g butter for
a 250g request (size is not identity) and rejects a list of only cookies and
buttermilk. The defect was that a refusal removed the item with no reason a human
could read. So the refusal stands, and the reason travels with it.
"""
from __future__ import annotations

from grocery.adapters.base import Listing


def _l(name, price, qty=100.0, unit="g"):
    return Listing(merchant="jiomart", sku=name[:8], name=name, brand="Amul",
                   pack_qty=qty, pack_unit=unit, price_inr=price, in_stock=True,
                   category="dairy", price_source="test", asof=0.0)


def test_a_refusal_removes_the_item_and_records_the_reason(isolated_data_dir, monkeypatch):
    import worker.run as wr
    from worker.llm import shopping

    monkeypatch.setattr(shopping, "choose", lambda q, s: {
        "index": None, "acceptable": [],
        "why": "the results are only cookies and buttermilk"})

    best, rejected = wr._pick_best_from_listings(
        [_l("Amul Fresh Butter Cookies", 85.0, 200.0),
         _l("Amul Masti Buttermilk", 70.0, 500.0, "ml")],
        query="amul butter 250g", use_model=True)

    assert best is None, "cookies must not be sold as butter"
    assert any("cookies and buttermilk" in r["why"] for r in rejected)
    assert wr._MODEL_REFUSALS.get("amul butter 250g"), (
        "the reason must be kept so the narrative can explain the absence"
    )


def test_the_model_is_consulted_even_when_the_signals_agree(isolated_data_dir, monkeypatch):
    """The gate that caused the cookies. The model used to be asked only when
    the cheapest pick differed from the store's top result — but "Amul Fresh
    Butter Cookies" was both, so the two signals agreed with each other and were
    both wrong, and nothing was ever asked."""
    import worker.run as wr
    from worker.llm import shopping

    asked = []
    monkeypatch.setattr(shopping, "choose",
                        lambda q, s: asked.append(q) or {"index": 0, "acceptable": [], "why": "ok"})

    cheapest_is_also_first = [_l("Amul Fresh Butter Cookies", 20.0, 200.0),
                              _l("Amul Pasteurised Butter 100 g", 62.0, 100.0)]
    wr._pick_best_from_listings(cheapest_is_also_first, query="amul butter 250g",
                                use_model=True)
    assert asked == ["amul butter 250g"]


def test_the_model_can_reorder_against_price(isolated_data_dir, monkeypatch):
    """Why it is in this path at all: the right product over the cheaper one."""
    import worker.run as wr
    from worker.llm import shopping

    monkeypatch.setattr(shopping, "choose", lambda q, s: {
        "index": next(i for i, l in enumerate(s) if "Pasteurised" in l.name),
        "acceptable": [], "why": "the request named Amul butter"})

    best, _ = wr._pick_best_from_listings(
        [_l("Amul Pasteurised Butter 100 g", 62.0, 100.0),
         _l("Amul Butter Tin 500 g", 99.0, 500.0)],
        query="amul butter 250g", use_model=True)
    assert best.name == "Amul Pasteurised Butter 100 g"


def test_with_no_model_the_deterministic_pick_stands(isolated_data_dir):
    """LLM_BACKEND=off must still produce a basket."""
    import worker.run as wr

    best, _ = wr._pick_best_from_listings(
        [_l("Amul Pasteurised Butter 100 g", 62.0, 100.0)], query="amul butter 250g")
    assert best is not None


def test_an_auditor_rejection_still_stands_alone(isolated_data_dir):
    """Ghee is not butter, and that needs no model to notice."""
    import worker.run as wr

    best, _ = wr._pick_best_from_listings(
        [_l("Amul High Aroma Cow Ghee 200 ml", 156.0, 200.0, "ml")],
        query="amul butter 250g")
    assert best is None
