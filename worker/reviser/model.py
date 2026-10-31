"""Asking a model what a revision actually means.

This replaces two regexes that could not read a sentence:

    \\b(drop|remove)\\s+(?:the\\s+)?(\\w+)
    \\b([a-z]+)\\s+not\\s+([a-z]+)\\b

They could not express a quantity change, an addition, "the first one", or
"something cheaper" — and on order_6a45f91aaa2e the first of them matched the
"no i" in "no i need 2 kg atta", produced drop=["i"], substring-matched "i"
against "pilsbury" and "multigrain", and deleted the only item in the cart. The
human got a spinner and no way forward.

THE MODEL SEES THE CART, which is the fix for the other half of that failure. A
human writes "remove onion" about a line on their screen; the shopping list says
"500g orhanges". Only something that can see both can connect them.

NO SILENT FALLBACK. If the model is unavailable, unparseable, or says nothing
usable, this returns a plan marked unavailable, the cart is left exactly as it
was, and the human is told the interpreter could not be reached. Falling back to
the regex would be worse than having no reviser: a wrong interpretation that
looks like a right one is how the cart got emptied in the first place.
"""
from __future__ import annotations

from worker.llm import client
from worker.reviser import schema

_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["set_quantity", "swap_brand", "drop", "add",
                                    "reject_product", "replace_item", "ask",
                                    "cannot_do"]},
                    "target": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["op"],
            },
        }
    },
    "required": ["ops"],
}

_PROMPT = """You are the revision step of an Indian grocery shopping assistant. The shopper has looked at a proposed cart and asked for a change. Work out exactly what they meant, and change nothing else.

THEIR SHOPPING LIST (these are the only things being bought; copy one of these VERBATIM when you use `target`):
{items}

THE CART THEY ARE LOOKING AT (what was chosen for each list entry):
{cart}

WHAT THEY JUST SAID:
{instruction}

Return JSON with an "ops" array. Each op is one of:
- {{"op":"set_quantity","target":"<list entry>","value":"2kg"}} — they want a different amount of something already on the list.
- {{"op":"swap_brand","target":"<list entry>","value":"Amul"}} — they want a different brand of that entry.
- {{"op":"drop","target":"<list entry>"}} — they no longer want that entry at all.
- {{"op":"add","value":"amul butter 500g"}} — they want something new that is not on the list.
- {{"op":"reject_product","target":"<the cart product's exact name>"}} — THE ENTRY IS FINE BUT THE CHOSEN PRODUCT IS WRONG. Use this when they name a PRODUCT IN THE CART rather than a list entry. "remove onion" when the cart holds an onion that was picked for "500g oranges" means: keep looking for oranges, just not that. Do NOT use `drop` for this — they still want the oranges.
- {{"op":"replace_item","target":"<list entry>","value":"multigrain bread"}} — INSTEAD OF one thing, they want a different thing. Use this for any substitution, including a conditional one: "if you can't get English Oven bread, any multigrain bread is fine" is replace_item on the bread entry with value "multigrain bread".
- {{"op":"ask","value":"<one short question>"}} — you genuinely cannot tell what they meant. Ask rather than guess.
- {{"op":"cannot_do","value":"<short explanation>"}} — they asked for something this assistant cannot do, such as changing the spending limit, paying, or delivering somewhere. Say so plainly.

RULES:
- Narrow, never generous. Change only what they asked about. If they mention one item, do not touch the others.
- IF THEY RE-STATE SOMETHING THAT IS ALREADY ON THE SHOPPING LIST, they are not adding it again — they are telling you the product you chose for it is WRONG. Use reject_product on that cart product. Example: the list says "30 eggs", the cart holds "Tata Iodised Salt 1 kg" for it, and they say "and 30 eggs" — that means {{"op":"reject_product","target":"Tata Iodised Salt 1 kg"}}, because salt is not eggs. Do not use `add`, and do not use `swap_brand`.
- swap_brand is ONLY for an actual brand name (Amul, Britannia, Pillsbury) — one or two words, matched against the store's brand field. NEVER put a phrase like "any multigrain bread" or "something cheaper" in it: no brand is called that, so nothing matches and the item disappears from the cart. A phrase means replace_item.
- `target` for set_quantity / swap_brand / drop must be copied EXACTLY from the shopping list above.
- `target` for reject_product must be copied EXACTLY from the cart product names above.
- One message can contain several requests; return one op each.
- ANY REQUEST TO SPEND MORE IS "cannot_do", however it is phrased. "increase the budget", "bump it to 2000", "i can pay more", "whatever it costs", "don't worry about the cap", "price is no object" — all of these are the same request, and this assistant cannot change the spending limit from a revision. Say so plainly and tell them a fresh request with a higher limit is the way. Do NOT ask which item they meant: they meant the budget.
- A VAGUE PREFERENCE WITH NO ITEM NAMED IS A QUESTION, NOT A CHANGE. "something cheaper", "a better one", "healthier" — you do not know which item they mean, so use "ask". Rejecting every product in the cart because they said two words is destroying their cart, not revising it.
- If you are not sure, use "ask". An honest question is better than a confident guess.

Reply with JSON only."""


def interpret(instruction_text: str, *, items: list[str],
              cart_lines: list[dict]) -> schema.RevisionPlan:
    """Read one revision. Always returns a plan; check `.unavailable` first.

    `cart_lines` is [{"name":…, "for_item":…}] — product names and the list
    entry each answers. No sku, no price source, no identifiers: the model needs
    to recognise a product by the name the human saw, and nothing more.
    """
    usable, why = client.available()
    if not usable:
        return schema.RevisionPlan(unavailable_reason=why)

    cart_text = "\n".join(
        f'- "{line.get("name", "")}"  (chosen for: {line.get("for_item", "?")})'
        for line in cart_lines
    ) or "- (the cart is empty)"
    items_text = "\n".join(f'- "{i}"' for i in items) or "- (nothing)"

    parsed = client.ask_json(
        _PROMPT.format(items=items_text, cart=cart_text,
                       instruction=repr(instruction_text)),
        schema=_SCHEMA, purpose="revise_cart", max_output_tokens=1200,
    )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("ops"), list):
        return schema.RevisionPlan(
            unavailable_reason="the interpreter did not return anything usable")

    plan = schema.validate(
        parsed["ops"], items=items,
        cart_line_names=[str(l.get("name", "")) for l in cart_lines],
    )
    if not plan.ops and not plan.rejected:
        return schema.RevisionPlan(
            unavailable_reason="the interpreter could not find any change to make")
    return plan
