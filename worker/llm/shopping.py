"""The shopping judgements that are language, not arithmetic.

Two of them, chosen because they are where the deterministic code measurably
underperformed on order_6a45f91aaa2e:

  understand(item)  — what is actually being asked for, and what should be
                      typed into a merchant's search box.
  choose(...)       — which of these real products is that thing.

WHAT THE MODEL IS NEVER ALLOWED TO DO, enforced in code below rather than asked
for in the prompt:

  * It never names a product. choose() returns an INDEX into the list it was
    given, validated against that list's length. A model that hallucinates a
    product cannot put one in the cart, because there is no field for it.
  * It never produces a price. The chosen listing's price is read from the
    Listing we already captured, so `unsourced_price` stays structurally
    impossible even if the model writes a number in its reasoning.
  * It never sees raw_description — see privacy.candidate_view.
  * It never changes the budget, the quantity cap, or the item list. understand()
    returns a search phrase and a quantity for ONE item it was given; it cannot
    add a second item, and the caller ignores anything beyond the fields below.

Everything here returns None when the model is unavailable, and every caller
falls back to the deterministic path and says so.
"""
from __future__ import annotations

from worker.llm import client, privacy

_MAX_CANDIDATES = 25  # keeps a prompt small, cheap, and inside the free tier


# ---- what did the human actually ask for? ------------------------------

_UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "search_phrase": {"type": "string"},
        "product": {"type": "string"},
        "brand": {"type": "string"},
        "quantity_value": {"type": "number"},
        "quantity_unit": {"type": "string",
                          "enum": ["g", "kg", "ml", "l", "count", "pack", "unspecified"]},
        "must_have": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_phrase", "product", "quantity_unit"],
}

_UNDERSTAND_PROMPT = """You are helping an Indian grocery shopping assistant read one line of a shopping list.

Shopping list line: {item!r}

Return JSON describing what was asked for:
- search_phrase: what to type into an Indian quick-commerce search box to find this. It MUST NOT CONTAIN ANY NUMBER OR UNIT. Drop politeness, CORRECT OBVIOUS TYPOS, keep a named brand, and use the word an Indian grocery site would use. Short — two or three words.
- product: the bare product, no brand, no quantity. e.g. "bread", "eggs", "oranges".
- brand: the brand if one was named, else "".
- quantity_value and quantity_unit: how much was asked for. "30 eggs" is 30 count. "atta 5kg" is 5 kg. "a dozen eggs" is 12 count. "250 gm mausambi" is 250 g. If no amount was named, use "unspecified".
- must_have: the words that a correct product MUST satisfy, such as a brand or a variety ("whole wheat", "multigrain", "toned"). Omit generic words.

Reply with JSON only."""


def understand(item: str) -> dict | None:
    """One shopping-list line, read as a human would read it.

    This is the single highest-value call in the system. Measured against live
    merchants: JioMart searched for "250 gm mausambi" returns onions, potatoes
    and cardamom, while the same catalogue searched for "mosambi" returns
    "Mosambi 6 pcs (Pack)" as the first result. The catalogue was never missing
    the fruit — the query was hiding it. Likewise "500g orhanges" returns orange
    marmalade and orange candy; "oranges" returns oranges.
    """
    parsed = client.ask_json(
        _UNDERSTAND_PROMPT.format(item=item),
        schema=_UNDERSTAND_SCHEMA, purpose="understand_item", max_output_tokens=600,
    )
    if not isinstance(parsed, dict):
        return None
    phrase = str(parsed.get("search_phrase") or "").strip()
    if not phrase:
        return None
    unit = str(parsed.get("quantity_unit") or "unspecified")
    value = parsed.get("quantity_value")
    return {
        "search_phrase": phrase[:120],
        "product": str(parsed.get("product") or "")[:60],
        "brand": str(parsed.get("brand") or "")[:60],
        "quantity_value": float(value) if isinstance(value, (int, float)) and value > 0 else None,
        "quantity_unit": unit if unit != "unspecified" else None,
        "must_have": [str(w)[:40] for w in (parsed.get("must_have") or [])][:6],
    }


# ---- which of these real products is it? -------------------------------

_CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "best_index": {"type": "integer"},
        "acceptable_indices": {"type": "array", "items": {"type": "integer"}},
        "why": {"type": "string"},
        "nothing_matches": {"type": "boolean"},
    },
    "required": ["best_index", "why", "nothing_matches"],
}

_CHOOSE_PROMPT = """You are helping an Indian grocery shopping assistant pick ONE product.

The shopper asked for: {item!r}

These are the real products the store returned, in the store's own order of relevance:
{table}

Return JSON:
- nothing_matches: true ONLY if none of these is the KIND OF THING asked for. Be strict about identity: a storage container is not eggs, marmalade is not oranges, curd is not milk, ghee is not butter. If true, best_index must be -1.
  THE LIST ALREADY CONTAINS ONLY PRODUCTS A FIRST FILTER THOUGHT WERE PLAUSIBLE, so nothing_matches should be rare. Use it for a genuine category error, not for a near miss.
  PACK SIZE IS NEVER A REASON TO SAY NOTHING MATCHES. If they asked for 250g of butter and the store sells it in 100g packs, that IS the product — several packs are bought to cover the amount, and that is handled elsewhere. Judge WHAT the product is, never HOW MUCH is in the pack. Saying nothing matches because the size is different silently drops the item from their basket.
- best_index: the number of the single best product. Satisfy what was asked for FIRST — the right brand and the right variety matter more than the price. Among products that equally satisfy the request, prefer the cheaper one, and prefer a pack size close to what was asked for over a much larger one. A size that does not divide neatly is fine; pick the best product anyway.
- acceptable_indices: every product that would also be a correct answer.
- why: one short sentence, for a human, explaining the choice.

Only pick from the numbered list. Reply with JSON only."""


def choose(item: str, listings: list) -> dict | None:
    """Pick the listing that answers `item`, or report that none does.

    Candidates are passed in the MERCHANT'S OWN ORDER, which is information the
    deterministic picker threw away: Swiggy ranked "Pillsbury Multigrain Atta
    ₹305" first for "pilsbury multigrain atta 5kg" and the cheapest-per-100g
    rule bought "Pillsbury Chakki Fresh Atta ₹470" instead — the wrong product,
    and ₹165 more expensive in absolute terms.

    Returns {"index", "acceptable", "why"} or {"index": None, ...} for nothing
    matches. The caller re-reads price and sku from its own Listing.
    """
    candidates = listings[:_MAX_CANDIDATES]
    if not candidates:
        return None
    rows = []
    for i, listing in enumerate(candidates):
        view = privacy.candidate_view(listing)
        stock = "" if view["in_stock"] else "  (OUT OF STOCK)"
        rows.append(f'{i}. {view["brand"]} {view["name"]} — {view["pack"]}, '
                    f'Rs{view["price_inr"]:.2f}{stock}')

    parsed = client.ask_json(
        _CHOOSE_PROMPT.format(item=item, table="\n".join(rows)),
        schema=_CHOOSE_SCHEMA, purpose="choose_product", max_output_tokens=800,
    )
    if not isinstance(parsed, dict):
        return None

    if parsed.get("nothing_matches") is True:
        return {"index": None, "acceptable": [],
                "why": str(parsed.get("why") or "none of the results is the thing asked for")[:200]}

    index = parsed.get("best_index")
    # VALIDATED AGAINST THE LIST WE SENT. An index outside it is dropped rather
    # than clamped: a model pointing off the end of the table is not making a
    # near-miss we should round into a purchase.
    if not isinstance(index, int) or not (0 <= index < len(candidates)):
        return None
    acceptable = [i for i in (parsed.get("acceptable_indices") or [])
                  if isinstance(i, int) and 0 <= i < len(candidates)]
    return {"index": index, "acceptable": acceptable,
            "why": str(parsed.get("why") or "")[:200]}


# ---- the whole list in one call ----------------------------------------

_UNDERSTAND_ALL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "string"},
                    "search_phrase": {"type": "string"},
                    "product": {"type": "string"},
                    "brand": {"type": "string"},
                    "quantity_value": {"type": "number"},
                    "quantity_unit": {"type": "string",
                                      "enum": ["g", "kg", "ml", "l", "count", "pack", "unspecified"]},
                    "must_have": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["line", "search_phrase", "product", "quantity_unit"],
            },
        }
    },
    "required": ["items"],
}

_UNDERSTAND_ALL_PROMPT = """You are helping an Indian grocery shopping assistant read a shopping list.

Here are the lines of the list, one per line:
{lines}

For EACH line return an object with:
- line: the original line, copied exactly.
- search_phrase: what to type into an Indian quick-commerce search box (Zepto, Blinkit, Swiggy Instamart, JioMart) to find it. It MUST NOT CONTAIN ANY NUMBER OR UNIT — no "500g", no "2 kg", no "dozen", no "30". Searching "500g oranges" returns orange marmalade and orange candy; searching "oranges" returns oranges. Drop politeness ("get me", "please"). CORRECT OBVIOUS TYPOS. Keep a named brand. Use the word an Indian grocery site would use (for example "mosambi" rather than "mausambi"). Keep it short — two or three words is usually best.
- product: the bare product, no brand, no quantity. e.g. "bread", "eggs", "oranges".
- brand: the brand if one was named, else "".
- quantity_value and quantity_unit: how much. "30 eggs" is 30 count. "atta 5kg" is 5 kg. "a dozen eggs" is 12 count. "250 gm mausambi" is 250 g. If no amount was named use "unspecified".
- must_have: words a correct product MUST satisfy, such as a brand or variety ("whole wheat", "multigrain"). Omit generic words.

If one line names several different products, return one object per product, each with the same `line`.
Return every line. Reply with JSON only."""


_QTY_IN_PHRASE_RE = __import__("re").compile(
    r"\b\d+(?:\.\d+)?\s*(?:g|gm|gms|kg|ml|l|ltr|litre|litres|pc|pcs|piece|pieces|"
    r"pack|packs|packet|dozen)?\b", __import__("re").IGNORECASE)


def _strip_quantity(phrase: str) -> str:
    """Belt to the prompt's braces. The instruction says no numbers; a model
    that ignores it would otherwise reintroduce the exact failure this call
    exists to fix, and a search phrase is cheap to clean in code."""
    cleaned = _QTY_IN_PHRASE_RE.sub(" ", phrase)
    cleaned = " ".join(cleaned.split())
    return cleaned or phrase


def understand_all(items: list[str]) -> dict[str, dict] | None:
    """Every line of the list in ONE call, keyed by the original line.

    Batched for a reason that is about money, not elegance: a five-item order
    costs one request here instead of five, which is most of what keeps this
    inside a free tier. It also lets the model see the list as a list — "milk
    and eggs" on one line is visibly two products, which is a judgement the
    comma-splitter in ask.html cannot make.

    Returns None (not a partial map) when the model is unavailable, so the
    caller takes the deterministic path for the whole order rather than
    silently mixing two different levels of understanding within one cart.
    """
    if not items:
        return None
    parsed = client.ask_json(
        _UNDERSTAND_ALL_PROMPT.format(lines="\n".join(f"- {i}" for i in items)),
        schema=_UNDERSTAND_ALL_SCHEMA, purpose="understand_list",
        max_output_tokens=2048,
    )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        return None

    out: dict[str, dict] = {}
    for row in parsed["items"]:
        if not isinstance(row, dict):
            continue
        # The line is matched back to one WE SENT. A model that rewrites the
        # line it was given cannot thereby introduce an item: an unrecognised
        # line is dropped, and anything we sent that is missing from the reply
        # keeps its original text and the deterministic path.
        line = str(row.get("line") or "")
        if line not in items:
            continue
        phrase = str(row.get("search_phrase") or "").strip()
        if not phrase:
            continue
        unit = str(row.get("quantity_unit") or "unspecified")
        value = row.get("quantity_value")
        out.setdefault(line, {
            "search_phrase": _strip_quantity(phrase)[:120],
            "product": str(row.get("product") or "")[:60],
            "brand": str(row.get("brand") or "")[:60],
            "quantity_value": float(value) if isinstance(value, (int, float)) and value > 0 else None,
            "quantity_unit": unit if unit != "unspecified" else None,
            "must_have": [str(w)[:40] for w in (row.get("must_have") or [])][:6],
        })
    return out or None


# ---- turning one sentence into a shopping list -------------------------

_SPLIT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}

_SPLIT_PROMPT = """Split this grocery request into separate shopping-list items.

Request: {request!r}

Return JSON with "items": one entry per DISTINCT PRODUCT the shopper wants.

- Keep each item's own quantity and brand with it: "buy 5kg pilsbury atta and 2kg sona masoori rice" is TWO items — "5kg pilsbury atta" and "2kg sona masoori rice".
- Drop the instruction words: "buy", "get me", "please", "i need".
- Do NOT correct spelling, do NOT normalise units, do NOT add anything they did not ask for. This step only separates; something later reads each item properly.
- A compound product name stays one item: "salt and pepper grinder" is one thing, not two.
- If it is already one product, return one item.

Reply with JSON only."""


def split_request(text: str) -> list[str] | None:
    """One free-text request -> a list of items, or None if the model is off.

    THE COMMA SPLIT IS NOT ENOUGH, and the failure is silent rather than loud.
    "buy 5kg pilusbury multigrain atta and 2kg sona masoori rice" has no comma
    in it, so it became a single item, was searched verbatim at four merchants,
    matched nothing anywhere, and the order died with no cart — observed as
    order_80bd9e8f2635. Punctuation is not where the item boundaries are.

    Deliberately NOT doing any correcting here. Typos, units and search
    phrasing are understand_all's job one step later, and a step that both
    splits and rewrites makes it impossible to tell which of the two got it
    wrong when something comes back odd.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    parsed = client.ask_json(
        _SPLIT_PROMPT.format(request=cleaned),
        schema=_SPLIT_SCHEMA, purpose="split_request", max_output_tokens=600,
    )
    if not isinstance(parsed, dict):
        return None
    items = [str(i).strip() for i in (parsed.get("items") or []) if str(i).strip()]
    return items[:_MAX_ITEMS_PER_REQUEST] or None


# A basket this long is a mistake or a paste, not a shopping list, and every
# item costs a search at every merchant.
_MAX_ITEMS_PER_REQUEST = 20
