"""The cart reviser's safety properties, tested with NO MODEL AND NO NETWORK.

The validator is where the safety lives, so it is tested by feeding it the JSON
a model might return — including the JSON a compromised or confused one would.
Whether the model reads English well is a different question from whether a bad
answer can hurt anything, and only the second one belongs in the default suite.

Two real failures drive most of these:
  order_59eaa8e03b2b — "no i need 2 kg atta" matched the old drop regex on
      "no i", produced drop=["i"], substring-matched "i" against "pilsbury" and
      "multigrain", and deleted the only item in the cart.
  order_6a45f91aaa2e — "remove onion" named a PRODUCT the picker had chosen for
      "500g orhanges". No list entry contained "onion", so nothing happened.
"""
from __future__ import annotations

import pytest

from worker import reviser

ITEMS = ["english oven whole wheat bread", "pilsbury multigrain atta 5kg",
         "30 eggs", "500g orhanges"]
CART_LINES = ["Mr. Bakewell Premium Multigrain Bread 400 g",
              "Pillsbury Superior Quality Wheat Chakki Fresh Atta 10 kg",
              "Tata Iodised Salt 1 kg", "Onion (Approx. 900 g - 1000 g)"]
LINE_TO_ITEM = dict(zip(CART_LINES, ITEMS))


def _plan(raw_ops):
    return reviser.validate(raw_ops, items=ITEMS, cart_line_names=CART_LINES)


# ---- a model cannot touch what it was not shown -------------------------

def test_an_invented_item_is_refused():
    """The op is DROPPED and recorded, never applied — a model that invents
    something to modify cannot reach the cart."""
    plan = _plan([{"op": "drop", "target": "caviar"}])
    assert plan.ops == []
    assert "not one of the current items" in plan.rejected[0]["why"]


def test_an_invented_op_is_refused():
    """A positive allowlist: an op this system has never heard of is refused by
    default rather than ignored silently."""
    plan = _plan([{"op": "set_price", "target": "30 eggs", "value": "1"}])
    assert plan.ops == []
    assert "unknown op" in plan.rejected[0]["why"]


def test_a_quantity_that_does_not_parse_is_refused():
    """A set_quantity whose rewritten text the rest of the pipeline cannot read
    would be cosmetic — the cart would silently keep the old amount."""
    plan = _plan([{"op": "set_quantity", "target": "30 eggs", "value": "loads"}])
    assert plan.ops == []
    assert "does not read as a quantity" in plan.rejected[0]["why"]


def test_a_revision_cannot_add_without_limit():
    plan = _plan([{"op": "add", "value": f"thing {i}"} for i in range(12)])
    assert len([o for o in plan.ops if o.op == reviser.ADD]) == reviser.schema.MAX_ADDED_ITEMS
    assert any("at most" in r["why"] for r in plan.rejected)


def test_the_model_cannot_set_a_price_or_a_budget():
    """There is no op for either, which is the point: the schema is closed, so
    these are unrepresentable rather than merely disallowed."""
    from worker.reviser import schema

    assert "set_price" not in schema._KNOWN_OPS
    assert "set_budget" not in schema._KNOWN_OPS
    for op in schema._KNOWN_OPS:
        assert "price" not in op and "budget" not in op and "pay" not in op


# ---- the failures that caused this module -------------------------------

def test_remove_onion_rejects_the_product_and_keeps_the_item():
    """order_6a45f91aaa2e. The human names a product; the list holds an entry.
    The oranges must survive — they still want oranges, just not an onion."""
    plan = _plan([{"op": "reject_product", "target": "Onion (Approx. 900 g - 1000 g)"}])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)

    assert "500g orhanges" in out["items"], "the item must not be dropped"
    assert out["rejected_products"]["500g orhanges"] == ["Onion (Approx. 900 g - 1000 g)"]


def test_a_quantity_correction_rewrites_the_item_and_keeps_the_rest():
    """order_59eaa8e03b2b: "no i need 2 kg atta". The old regex deleted the
    cart. The whole list must survive, with exactly one entry changed."""
    plan = _plan([{"op": "set_quantity", "target": "pilsbury multigrain atta 5kg",
                   "value": "2kg"}])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)

    assert len(out["items"]) == len(ITEMS), "a quantity change must not drop anything"
    assert "2kg pilsbury multigrain atta" in out["items"]
    assert "pilsbury multigrain atta 5kg" not in out["items"]
    for untouched in ("30 eggs", "500g orhanges", "english oven whole wheat bread"):
        assert untouched in out["items"]


def test_a_brand_swap_touches_only_the_item_it_names():
    """The over-interpretation guard, now structural rather than heuristic: the
    reviser names its target, so "Amul not Britannia" cannot blanket-filter
    every item at every merchant."""
    plan = _plan([{"op": "swap_brand", "target": "30 eggs", "value": "Amul"}])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)

    assert out["forced_brands"] == {"30 eggs": "Amul"}
    assert out["items"] == ITEMS


def test_a_drop_removes_exactly_one_entry():
    plan = _plan([{"op": "drop", "target": "english oven whole wheat bread"}])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)
    assert out["items"] == ITEMS[1:]


def test_several_changes_in_one_message():
    plan = _plan([
        {"op": "set_quantity", "target": "pilsbury multigrain atta 5kg", "value": "2kg"},
        {"op": "drop", "target": "english oven whole wheat bread"},
        {"op": "add", "value": "amul butter 500g"},
    ])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)
    assert "english oven whole wheat bread" not in out["items"]
    assert "2kg pilsbury multigrain atta" in out["items"]
    assert "amul butter 500g" in out["items"]


# ---- questions and refusals are answers, not failures -------------------

def test_a_question_is_carried_and_changes_nothing():
    plan = _plan([{"op": "ask", "value": "Which item did you want cheaper?"}])
    assert not plan.actionable
    assert plan.questions() == ["Which item did you want cheaper?"]
    assert reviser.apply(plan, items=ITEMS)["items"] == ITEMS


def test_a_refusal_is_carried_and_changes_nothing():
    plan = _plan([{"op": "cannot_do", "value": "I cannot change the spending limit."}])
    assert not plan.actionable
    assert plan.refusals() == ["I cannot change the spending limit."]
    assert reviser.apply(plan, items=ITEMS)["items"] == ITEMS


# ---- no model, no guessing ----------------------------------------------

def test_with_no_model_the_plan_is_unavailable_and_never_a_guess(isolated_data_dir):
    """LLM_BACKEND=off. The reviser must say it could not read the sentence —
    NOT fall back to the pattern matching that emptied a cart. An admitted
    failure is survivable; a confident wrong one is not."""
    # Imported INSIDE the test: isolated_data_dir purges worker/ so the client
    # rebinds to the reloaded config. The module-level import at the top of this
    # file was captured before the fixture ran, and still carries .env's
    # LLM_BACKEND=gemini — which is how this test made a real billed API call
    # the first time it was written.
    from worker import reviser as fresh_reviser

    plan = fresh_reviser.revise("no i need 2 kg atta", items=ITEMS, cart_lines=[])

    assert plan.unavailable
    assert "switched off" in plan.unavailable_reason
    assert plan.ops == []
    assert fresh_reviser.apply(plan, items=ITEMS)["items"] == ITEMS


def test_the_old_regex_failure_cannot_recur(isolated_data_dir):
    """The specific bug: "no i" -> drop "i" -> substring match against
    "pilsbury"/"multigrain" -> the only cart line deleted. Targets are now
    matched verbatim against the list, so a stray letter matches nothing."""
    plan = _plan([{"op": "drop", "target": "i"}])
    assert plan.ops == []
    assert reviser.apply(plan, items=ITEMS)["items"] == ITEMS


# ---- order_b837482ed7ee: a substitution request -------------------------

def test_a_phrase_is_refused_as_a_brand():
    """The real op from that order: swap_brand with value "any multigrain
    bread". The picker matches a forced brand against the store's brand field,
    so a phrase matches NOTHING — every candidate was eliminated and the bread
    silently left the cart while the narrative said everything was found."""
    plan = _plan([{"op": "swap_brand", "target": "english oven whole wheat bread",
                   "value": "any multigrain bread"}])
    assert plan.ops == []
    assert "not a brand" in plan.rejected[0]["why"]


def test_a_real_brand_is_still_accepted():
    plan = _plan([{"op": "swap_brand", "target": "30 eggs", "value": "Amul"}])
    assert [o.op for o in plan.ops] == [reviser.SWAP_BRAND]


def test_replace_item_expresses_a_substitution():
    """"if you can't get English Oven bread, any multigrain bread is fine" —
    a substitution, which no op could express before. The list entry changes, so
    the next pass searches for the new thing."""
    plan = _plan([{"op": "replace_item", "target": "english oven whole wheat bread",
                   "value": "multigrain bread"}])
    out = reviser.apply(plan, items=ITEMS, line_to_item=LINE_TO_ITEM)

    assert "multigrain bread" in out["items"]
    assert "english oven whole wheat bread" not in out["items"]
    assert len(out["items"]) == len(ITEMS), "a substitution replaces, it does not remove"
    assert out["forced_brands"] == {}, "a replacement must not leave a brand filter behind"
