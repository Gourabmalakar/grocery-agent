"""What may leave this machine for a model, and what may never.

THE RULE: a prompt is BUILT from a whitelist, then CHECKED against a denylist.
Either alone is insufficient. Building from a whitelist is the real defence —
you cannot leak a field you never read — but it decays the moment someone adds
a convenient f-string. The check is the thing that fails loudly when they do.

WHAT MUST NEVER REACH A MODEL, and why each one is listed:

  * The delivery address, label, pincode or coordinates. grocery/location.py
    goes to some length to hold as little of this as possible; shipping it to
    Google in a prompt would undo all of it in one line. No shopping decision
    needs it — "which bread is whole wheat" does not depend on where you live.
  * Any credential: the Gemini key itself, a merchant OAuth token, the
    passphrase. A prompt is logged, cached to disk, and sent to a third party.
  * Order, payment, mandate and cart identifiers, and signatures. These are the
    system's own control surface; a model has no use for them and an exfiltrated
    one is a real handle on a real order.
  * Listing.raw_description — untrusted merchant text (CLAUDE.md rule 2). The
    one thing in this pipeline an attacker can write is also the one thing we
    will not put in front of a model. This is the prompt-injection boundary.

A violation RAISES. It does not redact and continue: a prompt that contains
something it should not is a bug in the caller, and quietly scrubbing it would
hide the bug while teaching everyone that the guard handles it.
"""
from __future__ import annotations

import re

from grocery import config


class PromptLeak(RuntimeError):
    """A prompt carried something that must not leave this machine."""


_ID_PREFIXES = ("order_", "intent_", "cart_", "amend_", "pay_", "mandate_")
_COORD_RE = re.compile(r"\b\d{1,3}\.\d{3,}\b")          # a precise lat/lon
# A PAIR of decimals that both sit inside India's bounding box. This is what
# tells a location from money: "₹77.60" is a price and "12.98, 77.6" is a place,
# and no amount of staring at "77.6" alone can separate them. Catching the pair
# keeps even a coarse coordinate out of a prompt without refusing a tomato.
_COORD_PAIR_RE = re.compile(r"\b(\d{1,2}\.\d+)\s*,\s*(\d{2}\.\d+)\b")
_PINCODE_RE = re.compile(r"\b[1-9][0-9]{5}\b")           # an Indian PIN
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")  # keys, tokens, signatures


def _forbidden_strings() -> list[str]:
    """Live values, read at call time rather than imported once — the address
    can change between orders and a stale snapshot would check the wrong thing."""
    out: list[str] = []
    if config.GEMINI_API_KEY:
        out.append(config.GEMINI_API_KEY)
    try:
        from grocery import location
        here = location.current()
        # THE LABEL, ONLY WHEN IT COULD IDENTIFY ANYTHING. A nickname like
        # "Home" is four characters that appear inside "Homestyle Noodles", and
        # a guard that fires on that is a guard nobody can keep switched on.
        # A label worth protecting has several words, a digit, or real length.
        label = (here.label or "").strip()
        if label and (len(label) >= 10 or " " in label or any(c.isdigit() for c in label)):
            out.append(label)
    except Exception:  # noqa: BLE001 — the guard must never be the thing that breaks
        pass

    # COORDINATES ARE NOT CHECKED AS LITERAL STRINGS, and that is deliberate.
    # Gourab's longitude is 77.6; a tomato at ₹77.60 contains "77.6", so this
    # list used to match an ordinary price and refuse the call — then the
    # exception killed the worker mid-order (order_f884a496f8d7). A coordinate
    # rounded to ~1 km is the city, not the doorstep, and the thing actually
    # worth refusing is a PRECISE one — which _COORD_RE below catches by shape
    # (three or more decimal places) without colliding with money.
    #
    # Same for the pincode: _PINCODE_RE matches any six-digit number, so the
    # literal adds nothing and would fire on a product code.
    return [s for s in out if s and len(str(s)) > 2]


def assert_safe(prompt: str) -> None:
    """Raise unless this text is safe to hand to a third-party model."""
    for secret in _forbidden_strings():
        if str(secret) in prompt:
            # The message names WHAT KIND of thing leaked, never the value —
            # this exception text ends up in logs.
            raise PromptLeak(
                "prompt contains the delivery location or an API key; prompts are "
                "built from a whitelist of product fields and must not interpolate "
                "configuration or location"
            )
    if _COORD_RE.search(prompt):
        raise PromptLeak("prompt contains something shaped like a precise coordinate")
    for lat_text, lon_text in _COORD_PAIR_RE.findall(prompt):
        lat, lon = float(lat_text), float(lon_text)
        if 6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5:
            raise PromptLeak("prompt contains something shaped like a latitude/longitude pair")
    if _PINCODE_RE.search(prompt):
        raise PromptLeak("prompt contains something shaped like an Indian pincode")
    for prefix in _ID_PREFIXES:
        if prefix in prompt:
            raise PromptLeak(f"prompt contains an internal identifier ({prefix}…)")
    if _LONG_SECRET_RE.search(prompt):
        raise PromptLeak("prompt contains a long opaque string — a key, token or signature")


# ---- the whitelist: the only product fields a model is ever shown ------

CANDIDATE_FIELDS = ("name", "brand", "pack_qty", "pack_unit", "price_inr", "in_stock")


def candidate_view(listing) -> dict:
    """A listing as a model may see it.

    raw_description is ABSENT BY CONSTRUCTION, not filtered out. It is the only
    merchant-controlled free text in the pipeline, which makes it the injection
    surface; a model that never receives it cannot be instructed by it. sku is
    absent too — the model answers with an INDEX into the list it was given, so
    it never needs an identifier and can never invent one.
    """
    return {
        "name": str(getattr(listing, "name", ""))[:120],
        "brand": str(getattr(listing, "brand", ""))[:60],
        "pack": f"{getattr(listing, 'pack_qty', '')}{getattr(listing, 'pack_unit', '')}",
        "price_inr": float(getattr(listing, "price_inr", 0) or 0),
        "in_stock": bool(getattr(listing, "in_stock", True)),
    }
