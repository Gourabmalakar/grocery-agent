"""The cart auditor: does this cart actually answer what was asked?

WHY THIS EXISTS, from the order that caused it (order_6a45f91aaa2e). The ask was
"english oven whole wheat bread, pilsbury multigrain atta 5kg, 30 eggs, 500g
oranges, 250 gm mausambi". The proposed cart was bread of a different brand, a
10 kg atta, table salt, an onion and a potato. Nothing was hallucinated — the
decision log records it exactly:

    price-scout  select_item  jiomart: chose Tata Iodised Salt 1 kg for '30 eggs'
    price-scout  select_item  jiomart: chose Onion  for '500g orhanges'
    price-scout  select_item  jiomart: chose Potato for '250 gm mausambi'

JioMart's catalogue returned no eggs, no oranges and no mausambi for those
phrases, and a cheapest-per-normalised-unit picker faithfully bought the
cheapest thing in front of it. Every mechanical check passed: real SKU, real
price, real price_source, clean lineage, honest reasoning string. The cart was
wrong in the only way that matters and the system was serenely confident.

THE ONE IDEA: a price is only comparable once the two things are the same thing.
Selection was answering "what is cheapest here?" when the question is "what here
is what he asked for, and of those, what is cheapest?". This module answers the
first half, and it runs BEFORE price is allowed to decide anything.

IT IS DELIBERATELY NOT A MODEL. CLAUDE.md keeps models out of grocery/; this
lives in worker/ where a model would be allowed, and still does not use one —
because the failures above are not subtle. "Tata Iodised Salt" and "30 eggs"
share no word at all. A model is the right tool for the residue (is "Sandwich
Bread" whole wheat?), and plugs in at satisfies() when one is available; it is
not needed to catch a potato sold as a mausambi, and waiting for one would have
left this broken.

WHAT IT REFUSES TO DECIDE. When a product neither clearly matches nor clearly
contradicts the request, the verdict is SUSPECT, not a rejection. A suspect line
is kept and SAID OUT LOUD to the human, because the alternative — silently
dropping "Organically Grown Sweet Lime" because it does not contain the letters
"mausambi" — replaces buying the wrong thing with refusing to buy the right one.
Rule 3 again: visibly uncertain beats invisibly wrong.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# Words that carry no product identity. Split into two groups because they are
# wrong for different reasons: the first is how people talk to an assistant, the
# second is how quantities are spelled.
_FILLER = frozenset("""
get me please i want need buy order add some a an the of and or for with to my
kindly also pls plz grab pick up from fresh
""".split())

_UNITS = frozenset("""
g gm gms gram grams kg kgs kilo kilos ml l ltr litre litres liter liters
pc pcs piece pieces pack packs packet packets box boxes dozen no nos unit units
""".split())

_TOKEN_RE = re.compile(r"[a-z]+")
# "500g", "250gm", "1l" — a number welded to a unit, which tokenising on letters
# alone would turn into a bare unit word and then discard as noise. The unit
# alternation is NOT optional, and that is the whole point: an earlier
# `\d+\s*[a-z]*` ate "30 eggs" down to a stray "s" and left "30 eggs" with no
# describable item in it at all. A number followed by a word that is not a unit
# is a count of that word, and the word has to survive.
_NUMBER_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:g|gm|gms|gram|grams|kg|kgs|ml|l|ltr|litre|litres|"
    r"liter|liters|pc|pcs|piece|pieces|pack|packs|packet|packets|dozen)\b"
)
# Bare numbers go too, once the number+unit pairs above are gone: "30 eggs" is
# about eggs, and the 30 is a quantity the picker handles, not an identity.
_BARE_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# Close enough to be the same word: covers the typos people actually make
# ("orhanges" for oranges, "pilsbury" for Pillsbury) and the transliteration
# spread on Indian produce ("mausambi" / "mosambi"). Tuned against real observed
# pairs, not picked for roundness — see tests/unit/test_auditor.py.
_FUZZY_RATIO = 0.78

# A DELIBERATELY TINY synonym table, and the reason it is tiny matters. Indian
# produce carries several names at once, and no amount of string distance gets
# from "mausambi" to "sweet lime" — they share no letters in that order. Without
# this, a correct Blinkit pick ("Organically Grown Sweet Lime") is rejected for a
# request it actually answers, which trades buying the wrong thing for refusing
# the right one.
#
# This table is NOT the answer to synonyms in general and must not be grown into
# one — a hand-maintained thesaurus is wrong the day someone searches a word
# nobody added. It holds only pairs observed failing on real merchant output, and
# the real fix is the model layer at satisfies(). Every entry should be traceable
# to an order that went wrong.
_SYNONYMS: dict[str, frozenset[str]] = {
    "mausambi": frozenset({"mosambi", "musambi", "sweetlime"}),
    "mosambi": frozenset({"mausambi", "musambi", "sweetlime"}),
    "atta": frozenset({"flour"}),
    "curd": frozenset({"dahi", "yoghurt", "yogurt"}),
    "brinjal": frozenset({"eggplant", "aubergine"}),
    "capsicum": frozenset({"bellpepper"}),
    "coriander": frozenset({"dhania", "cilantro"}),
}


OK = "ok"
SUSPECT = "suspect"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class LineVerdict:
    query: str
    name: str
    verdict: str
    why: str

    @property
    def is_rejection(self) -> bool:
        return self.verdict == MISMATCH


@dataclass
class AuditReport:
    """What the human is owed about this cart, whether or not it is good."""
    mismatches: list[LineVerdict] = field(default_factory=list)
    suspects: list[LineVerdict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.missing

    def feedback(self) -> list[str]:
        """Sentences addressed to whatever is going to try again — plain enough
        to show a human verbatim, specific enough to act on."""
        out = []
        for verdict in self.mismatches:
            out.append(f"{verdict.name!r} does not answer {verdict.query!r} — {verdict.why}.")
        for item in self.missing:
            out.append(f"{item!r} was asked for and is not in the cart.")
        for verdict in self.suspects:
            out.append(f"{verdict.name!r} may not be what {verdict.query!r} meant — {verdict.why}.")
        return out


def content_words(text: str) -> list[str]:
    """The words that say WHAT the thing is, with the words that say how much,
    and the words that are just politeness, removed.

    "Get me english oven whole wheat bread" -> [english, oven, whole, wheat, bread]
    "500g orhanges" -> [orhanges]
    "30 eggs" -> [eggs]
    """
    lowered = (text or "").lower()
    lowered = _NUMBER_UNIT_RE.sub(" ", lowered)
    lowered = _BARE_NUMBER_RE.sub(" ", lowered)
    words = [w for w in _TOKEN_RE.findall(lowered) if len(w) > 1]
    return [w for w in words if w not in _FILLER and w not in _UNITS]


def _close(a: str, b: str) -> bool:
    if a == b:
        return True
    # Prefix containment catches plural and inflection ("orange"/"oranges",
    # "pack"/"packs") — AND NOTHING LONGER THAN THAT. The length guard is the
    # whole safeguard: requiring both words to be "substantial" was not enough,
    # because "butter" is substantial and so is "buttermilk", and the rule
    # happily matched them. Gourab asked for amul butter and was sold "Amul
    # Masti Spiced Salted Buttermilk".
    #
    # Two characters is about as much as an English plural or a verb ending
    # costs. Beyond that the extra letters are usually a different product:
    # butter/buttermilk (4), milk/milkshake (5), cream/creamer (2 — and caught
    # by the ratio test below only if it really is close).
    if (len(a) >= 4 and len(b) >= 4 and abs(len(a) - len(b)) <= 2
            and (a.startswith(b) or b.startswith(a))):
        return True
    if abs(len(a) - len(b)) > 3:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_RATIO


def _any_close(word: str, candidates: list[str]) -> bool:
    if any(_close(word, c) for c in candidates):
        return True
    # "sweet lime" arrives as two tokens; the synonym is one word. Compare
    # against the joined form too so a multi-word name can match a single-word
    # request.
    joined = "".join(candidates)
    for synonym in _SYNONYMS.get(word, ()):
        if any(_close(synonym, c) for c in candidates) or synonym in joined:
            return True
    return False


def satisfies(query: str, *, name: str, brand: str = "",
              category: str = "", required: list[str] | None = None) -> LineVerdict:
    """Could this product be what that phrase asked for?

    MISMATCH means: not one content word of the request appears in the product,
    even approximately. That is the salt-for-eggs case, and it is a statement
    about overlap, never about taste — this function has no opinion on whether
    toned milk answers "milk", only on whether a potato answers "mausambi".
    """
    # WORDS THE SHOPPER EXPLICITLY NAMED are not negotiable. These come from
    # the understanding layer's `must_have` — the brand and the variety it read
    # out of the request. Without this, "2kg pilsbury multigrain atta" accepted
    # "Pillsbury Fresh Chakki Atta (0% Maida)": two of three words matched, so
    # the majority rule called it merely suspicious, and because that pack was
    # cheaper per kilo it won — a product that is not multigrain, at ₹471
    # against the ₹309 multigrain sitting next to it in the same results.
    for word in (required or []):
        for part in content_words(word):
            if not _any_close(part, content_words(f"{brand} {name} {category}")):
                return LineVerdict(
                    query, name, MISMATCH,
                    f"it is not {part!r}, and the request said so explicitly",
                )

    wanted = content_words(query)
    if not wanted:
        # Nothing to check against. Saying OK here would be a guess dressed as a
        # pass, so it is surfaced instead.
        return LineVerdict(query, name, SUSPECT, "the request has no describable item in it")

    # NAME, BRAND AND THE MERCHANT'S OWN CLASSIFICATION. The category matters
    # because a product name often never says what kind of thing it is:
    # "Britannia Good Day" is biscuits and contains no word resembling
    # "biscuits", and rejecting it for a request that says so would trade buying
    # the wrong thing for refusing the right one. Where a merchant classifies its
    # own products, that is evidence and it is used.
    have = content_words(f"{brand} {name} {category}")
    matched = [w for w in wanted if _any_close(w, have)]

    if not matched:
        return LineVerdict(
            query, name, MISMATCH,
            f"nothing in it matches {', '.join(repr(w) for w in wanted)}",
        )
    unmatched = [w for w in wanted if w not in matched]
    # The head word — the last content word — is what the thing IS; the ones
    # before it are usually brand and attributes ("english oven whole wheat
    # BREAD"). Missing the head word is a different kind of wrong from missing a
    # qualifier, and only the qualifiers make something suspect rather than bad.
    head = wanted[-1]
    if head not in matched:
        return LineVerdict(
            query, name, MISMATCH,
            f"it is not {head!r}, which is what was actually asked for",
        )
    if len(unmatched) * 2 > len(wanted):
        # More of the request is missing than is present. "english oven whole
        # wheat bread" answered by "Mr. Bakewell Premium Multigrain Bread"
        # matches on the word `bread` and on nothing else — the brand asked for,
        # and both attributes, are simply absent. Calling that merely suspicious
        # is how a named brand gets quietly substituted, which is the complaint
        # that produced this module.
        return LineVerdict(
            query, name, MISMATCH,
            f"it matches only {matched[-1]!r} — the request also said "
            f"{', '.join(repr(w) for w in unmatched)}",
        )
    if unmatched:
        return LineVerdict(
            query, name, SUSPECT,
            f"the request also said {', '.join(repr(w) for w in unmatched)}, which this does not say",
        )
    return LineVerdict(query, name, OK, "matches the request")


# How many times the cheapest other merchant's price for the SAME request a line
# may cost before it stops looking like the same product. Three is deliberately
# loose: real price spreads between quick-commerce stores are large, and this is
# meant to catch a ₹6,570 egg-storage container quoted against a ₹243 tray of
# eggs, not to arbitrate a 40% difference.
_OUTLIER_MULTIPLE = 3.0


def price_outliers(*, line_items: list[dict], line_queries: dict[str, str],
                   peer_prices: dict[str, list[float]]) -> list[LineVerdict]:
    """Lines that cost wildly more than another merchant charged for the SAME
    request phrase.

    Word matching alone cannot tell a food from an object that shares its name.
    JioMart answered "30 eggs" with "Cetlo 18 Grid Egg Storage Container with
    Lid" at ₹6,570 — it contains the word "egg", so every string test passes it,
    and it is a plastic box. What gives it away is the comparison the system is
    already doing: two other merchants answered the same phrase for ₹243 and
    ₹312. A number twenty-seven times its peers is not a premium, it is a
    different kind of thing.

    This works only because several merchants are searched, which makes it a
    property of the comparison rather than of any one catalogue — and it says
    nothing at all when there is only one merchant, rather than guessing.
    """
    out = []
    for line in line_items:
        query = line_queries.get(str(line.get("sku", "")), "")
        peers = [p for p in peer_prices.get(query, []) if p > 0]
        mine = float(line.get("line_total_inr") or 0)
        if not peers or mine <= 0:
            continue
        cheapest_peer = min(peers)
        if mine > cheapest_peer * _OUTLIER_MULTIPLE:
            out.append(LineVerdict(
                query, str(line.get("name", "")), SUSPECT,
                f"it costs ₹{mine:.0f} where another store answered the same request for "
                f"₹{cheapest_peer:.0f} — that is {mine / cheapest_peer:.0f}x, which usually means "
                "it is not the same kind of thing",
            ))
    return out


def audit_cart(*, requested_items: list[str], line_items: list[dict],
               line_queries: dict[str, str],
               peer_prices: dict[str, list[float]] | None = None) -> AuditReport:
    """The whole-cart check that runs before anything is proposed.

    `line_queries` maps a line's sku to the request phrase it was chosen for, so
    every line is judged against what it was supposed to answer rather than
    against the basket as a whole.
    """
    report = AuditReport()
    answered: set[str] = set()

    for line in line_items:
        query = line_queries.get(str(line.get("sku", "")), "")
        verdict = satisfies(query, name=str(line.get("name", "")),
                            brand=str(line.get("brand", "")),
                            category=str(line.get("category", "")))
        if verdict.verdict == MISMATCH:
            report.mismatches.append(verdict)
        else:
            answered.add(query)
            if verdict.verdict == SUSPECT:
                report.suspects.append(verdict)

    report.missing = [item for item in requested_items if item not in answered]
    if peer_prices:
        known = {(v.query, v.name) for v in report.suspects}
        for verdict in price_outliers(line_items=line_items, line_queries=line_queries,
                                      peer_prices=peer_prices):
            if (verdict.query, verdict.name) not in known:
                report.suspects.append(verdict)
    return report
