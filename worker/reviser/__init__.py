"""The cart reviser: one entry point, `revise`.

It lives in worker/ because that is the process that proposes and can never
approve (CLAUDE.md rule 1). A fully compromised reviser can produce a wrong
shopping list; it cannot approve a cart, move money, raise the spending cap, or
set a price — the cap is on the signed intent and nothing here writes it, and
every price still comes from a live search over the revised list.
"""
from __future__ import annotations

from worker.reviser.model import interpret
from worker.reviser.schema import (ADD, ASK, CANNOT_DO, DROP, REJECT_PRODUCT,
                                   REPLACE_ITEM, SET_QUANTITY, SWAP_BRAND, RevisionOp,
                                   RevisionPlan, apply, validate)

__all__ = ["interpret", "apply", "validate", "RevisionOp", "RevisionPlan",
           "ADD", "ASK", "CANNOT_DO", "DROP", "REJECT_PRODUCT", "REPLACE_ITEM",
           "SET_QUANTITY", "SWAP_BRAND"]


def revise(instruction_text: str, *, items: list[str], cart_lines: list[dict]) -> RevisionPlan:
    """What does this revision mean? Never raises; check `.unavailable`."""
    return interpret(instruction_text, items=items, cart_lines=cart_lines)
