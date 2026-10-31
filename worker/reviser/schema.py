"""The closed set of changes a revision may make, and the code that checks them.

THE ONE STRUCTURAL IDEA: THE REVISER EMITS A REVISED SHOPPING LIST, NEVER A CART
AND NEVER A PRICE. Gourab's own framing — "read the revision and pass it as a
revised order, which the worker will take back and run the reasoning with" — is
also this repo's standing rule that feedback amends the intent and never edits
the cart. So the model rewrites a list of things to buy; every price still comes
from a real search() and every selection from the existing picker. A fully
prompt-injected model cannot invent a price or a product, because there is no
field here for either.

EVERY OP IS VALIDATED AGAINST DATA THE MODEL WAS GIVEN, not against the prompt's
instructions. `target` must be verbatim one of the current items; a quantity
must re-parse; an added item is searched for like any other and cap-checked like
any other. Anything that fails is DROPPED with a decision record naming the raw
op — never applied, never silently ignored.

WHY reject_product EXISTS, since the plan did not have it. On
order_6a45f91aaa2e Gourab wrote "remove onion". There was no onion in his
shopping list: onion was the product the picker had chosen for "500g orhanges".
The old regex matched the word, found no item containing "onion", and changed
nothing. Humans talk about the cart in front of them; the system stores the
list behind it, and something has to translate. reject_product is that
translation — it keeps the item and refuses the product.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from grocery.compare import normalise

SET_QUANTITY = "set_quantity"
SWAP_BRAND = "swap_brand"
DROP = "drop"
ADD = "add"
REJECT_PRODUCT = "reject_product"
REPLACE_ITEM = "replace_item"
ASK = "ask"
CANNOT_DO = "cannot_do"

_KNOWN_OPS = frozenset({SET_QUANTITY, SWAP_BRAND, DROP, ADD, REJECT_PRODUCT,
                        REPLACE_ITEM, ASK, CANNOT_DO})

# A brand is a name, not a sentence. "any multigrain bread" arrived as a
# swap_brand value on a real order (order_b837482ed7ee) and silently emptied the
# item: the picker filters on listing.brand == value, no brand is called "any
# multigrain bread", so every candidate was eliminated and the bread vanished
# from the cart while the narrative claimed everything had been found. A
# substitution is replace_item; swap_brand is for "Amul", "Britannia", "Tata".
_MAX_BRAND_WORDS = 3
_NOT_A_BRAND = frozenset({"any", "available", "other", "another", "cheaper",
                          "bread", "milk", "eggs", "atta", "flour", "rice",
                          "something", "anything", "alternative", "substitute"})

# A revision may not widen the basket without limit, whatever it says.
MAX_ADDED_ITEMS = 5


@dataclass(frozen=True)
class RevisionOp:
    op: str
    target: str = ""   # verbatim one of the current items (unused by add/ask/cannot_do)
    value: str = ""    # new quantity text, brand, item text, question, or reason


@dataclass
class RevisionPlan:
    ops: list[RevisionOp] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)   # ops dropped, with why
    unavailable_reason: str = ""                          # set => nothing was understood

    @property
    def unavailable(self) -> bool:
        return bool(self.unavailable_reason)

    @property
    def actionable(self) -> bool:
        return any(o.op in (SET_QUANTITY, SWAP_BRAND, DROP, ADD, REJECT_PRODUCT,
                            REPLACE_ITEM) for o in self.ops)

    def questions(self) -> list[str]:
        return [o.value for o in self.ops if o.op == ASK and o.value]

    def refusals(self) -> list[str]:
        return [o.value for o in self.ops if o.op == CANNOT_DO and o.value]


def validate(raw_ops: list[dict], *, items: list[str],
             cart_line_names: list[str] | None = None) -> RevisionPlan:
    """Turn whatever the model said into ops this system is willing to perform.

    `items` is the current shopping list. `cart_line_names` is what the human can
    actually see, which is how a target naming a product ("onion") is resolved
    back to the list entry it came from — that mapping is supplied by the caller,
    which knows it; this function only checks that a target is one of the two.
    """
    plan = RevisionPlan()
    added = 0
    for raw in raw_ops or []:
        if not isinstance(raw, dict):
            plan.rejected.append({"raw": str(raw)[:200], "why": "not an object"})
            continue
        op = str(raw.get("op") or "")
        target = str(raw.get("target") or "").strip()
        value = str(raw.get("value") or "").strip()

        if op not in _KNOWN_OPS:
            plan.rejected.append({"raw": str(raw)[:200],
                                  "why": f"unknown op {op!r} — this system performs only "
                                         f"{sorted(_KNOWN_OPS)}"})
            continue

        if op in (ASK, CANNOT_DO):
            if not value:
                plan.rejected.append({"raw": str(raw)[:200], "why": f"{op} with nothing to say"})
                continue
            plan.ops.append(RevisionOp(op, value=value))
            continue

        if op == ADD:
            text = value or target
            if not text:
                plan.rejected.append({"raw": str(raw)[:200], "why": "add with no item text"})
                continue
            added += 1
            if added > MAX_ADDED_ITEMS:
                plan.rejected.append({"raw": str(raw)[:200],
                                      "why": f"a single revision may add at most "
                                             f"{MAX_ADDED_ITEMS} items"})
                continue
            plan.ops.append(RevisionOp(ADD, value=text[:120]))
            continue

        # Everything below names something that must already exist. A model
        # that invents an item to modify cannot touch the cart.
        if target not in items:
            known = list(cart_line_names or [])
            if target not in known:
                plan.rejected.append({
                    "raw": str(raw)[:200],
                    "why": f"target {target!r} is not one of the current items or cart lines",
                })
                continue

        if op == SET_QUANTITY:
            if not value:
                plan.rejected.append({"raw": str(raw)[:200], "why": "set_quantity with no quantity"})
                continue
            # The rewritten text has to be something the rest of the pipeline can
            # actually read, or the change is cosmetic and the cart silently
            # keeps the old amount.
            rewritten = _rewrite_quantity(target, value)
            if normalise.parse_quantity_phrase(rewritten) is None:
                plan.rejected.append({"raw": str(raw)[:200],
                                      "why": f"{value!r} does not read as a quantity"})
                continue
            plan.ops.append(RevisionOp(SET_QUANTITY, target=target, value=value[:60]))
            continue

        if op == SWAP_BRAND:
            if not value:
                plan.rejected.append({"raw": str(raw)[:200], "why": "swap_brand with no brand"})
                continue
            words = value.lower().split()
            if len(words) > _MAX_BRAND_WORDS or any(w in _NOT_A_BRAND for w in words):
                plan.rejected.append({
                    "raw": str(raw)[:200],
                    "why": f"{value!r} is a description, not a brand — a swap_brand value is "
                           "matched against the merchant's brand field, so a phrase matches "
                           "nothing and would silently remove the item",
                })
                continue

        if op == REPLACE_ITEM and not value:
            plan.rejected.append({"raw": str(raw)[:200], "why": "replace_item with no new item"})
            continue

        plan.ops.append(RevisionOp(op, target=target, value=value[:120]))
    return plan


def _rewrite_quantity(item: str, new_quantity: str) -> str:
    """"500g paneer" + "250g" -> "250g paneer".

    Leading, because parse_quantity_phrase anchors on a leading number — and
    because the understanding layer reads the amount wherever it sits, this
    shape is the one both of them handle.
    """
    stripped = normalise.parse_pack_size(item)
    body = item
    if stripped:
        import re
        body = re.sub(r"\d+(?:\.\d+)?\s*[a-zA-Z]*\s*", " ", item, count=1).strip() or item
    return f"{new_quantity} {body}".strip()


def apply(plan: RevisionPlan, *, items: list[str],
          line_to_item: dict[str, str] | None = None) -> dict:
    """Fold the ops into a revised list plus the hints the planner needs.

    Returns revised_items, forced_brands and rejected_products. Nothing here
    touches a cart: the caller re-runs the whole sourcing and selection pass
    over the revised list, so a revision cannot produce a price that did not
    come from a live search.
    """
    line_to_item = line_to_item or {}
    revised = list(items)
    forced_brands: dict[str, str] = {}
    rejected_products: dict[str, list[str]] = {}

    def resolve(target: str) -> str | None:
        """A target may name a list entry or a product the human can see."""
        if target in revised:
            return target
        owner = line_to_item.get(target)
        return owner if owner in revised else None

    for op in plan.ops:
        if op.op == ADD:
            if op.value not in revised:
                revised.append(op.value)
            continue
        item = resolve(op.target)
        if item is None:
            continue
        if op.op == DROP:
            revised = [i for i in revised if i != item]
            forced_brands.pop(item, None)
            rejected_products.pop(item, None)
        elif op.op == SET_QUANTITY:
            rewritten = _rewrite_quantity(item, op.value)
            revised = [rewritten if i == item else i for i in revised]
            if item in forced_brands:
                forced_brands[rewritten] = forced_brands.pop(item)
            if item in rejected_products:
                rejected_products[rewritten] = rejected_products.pop(item)
        elif op.op == REPLACE_ITEM:
            # "instead of X, get Y" — the list entry itself changes, so the next
            # pass searches for the new thing and the auditor judges against it.
            revised = [op.value if i == item else i for i in revised]
            forced_brands.pop(item, None)
            rejected_products.pop(item, None)
        elif op.op == SWAP_BRAND:
            forced_brands[item] = op.value
        elif op.op == REJECT_PRODUCT:
            rejected_products.setdefault(item, []).append(op.target)
    return {"items": revised, "forced_brands": forced_brands,
            "rejected_products": rejected_products}
