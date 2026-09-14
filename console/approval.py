"""
The approval state machine. This is the actual security boundary: everything
in the plan about "the agent cannot go rogue" is a claim about what this file
enforces, not a claim about the worker's good behaviour.

States:
    draft -> proposed -> revising -> proposed  (loop, bounded)
                       -> approved -> executing -> done
                       -> aborted                          (terminal, from any pre-approved state)

Every mutating method re-checks the kill switch first (grocery.killswitch),
and cart-affecting methods re-verify cart_hash before trusting a cart's
content — see mandates/cart.py's hash_matches.

Persistence is a plain JSON-per-order file store under data/orders/ — no ORM
needed at this scale, and it keeps the state machine legible as a diff in
the audit log rather than hidden behind SQL.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from grocery import config, killswitch
from grocery.audit import log as audit_log
from grocery.ledger import budget, store as ledger_store
from grocery.mandates import cart as cart_mandate
from grocery.mandates import payment as payment_mandate
from grocery.mandates import intent as intent_mandate
from grocery.mandates import amendment as amendment_mandate
from grocery.adapters.base import CartLine as cart_mandate_CartLine

_ORDERS_DIR = config.DATA / "orders"
_ORDERS_DIR.mkdir(parents=True, exist_ok=True)


class State(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    REVISING = "revising"
    APPROVED = "approved"
    EXECUTING = "executing"
    DONE = "done"
    ABORTED = "aborted"
    REFUSED = "refused"  # terminal, non-money-moving failure (budget, hash mismatch, etc.)


_LEGAL_TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT: {State.PROPOSED, State.ABORTED},
    State.PROPOSED: {State.REVISING, State.APPROVED, State.ABORTED, State.REFUSED},
    State.REVISING: {State.PROPOSED, State.ABORTED, State.REFUSED},
    # REFUSED here covers a failed re-quote, a caught price drift, or a
    # budget-cap failure — all three are detected in approve_payment_and_
    # execute() while the order is still APPROVED, before EXECUTING is ever
    # entered (see that function's ordering: re-quote/drift/budget checks
    # all happen before the PaymentMandate is minted).
    State.APPROVED: {State.EXECUTING, State.ABORTED, State.REFUSED},
    State.EXECUTING: {State.DONE, State.REFUSED},
    State.DONE: set(),
    State.ABORTED: set(),
    State.REFUSED: set(),
}


class IllegalTransition(Exception):
    pass


class ApprovalRefused(Exception):
    pass


@dataclass
class Order:
    order_id: str
    intent: dict[str, Any]
    state: State = State.DRAFT
    cart: dict[str, Any] | None = None
    cart_version: int = 1
    comparison: list[dict[str, Any]] | None = None
    amendments: list[dict[str, Any]] = field(default_factory=list)
    revision_rounds: int = 0
    payment: dict[str, Any] | None = None
    merchant_order_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Order":
        d = dict(d)
        d["state"] = State(d["state"])
        return Order(**d)


def _path(order_id: str) -> Path:
    return _ORDERS_DIR / f"{order_id}.json"


def _save(order: Order) -> None:
    order.updated_at = time.time()
    _path(order.order_id).write_text(json.dumps(order.to_json(), indent=2, sort_keys=True))


def load(order_id: str) -> Order:
    p = _path(order_id)
    if not p.exists():
        raise KeyError(f"no such order {order_id}")
    return Order.from_json(json.loads(p.read_text()))


def list_orders() -> list[Order]:
    return [Order.from_json(json.loads(p.read_text())) for p in sorted(_ORDERS_DIR.glob("*.json"))]


def _transition(order: Order, to: State) -> None:
    if to not in _LEGAL_TRANSITIONS[order.state]:
        raise IllegalTransition(f"{order.order_id}: {order.state.value} -> {to.value} is not a legal transition")
    audit_log.append("order.transition", {
        "order_id": order.order_id, "from": order.state.value, "to": to.value,
    })
    order.state = to


# ---------------------------------------------------------------- create ---

def create_order(signed_intent: dict[str, Any]) -> Order:
    killswitch.require_not_killed("create_order")
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    order = Order(order_id=order_id, intent=signed_intent)
    audit_log.append("order.created", {"order_id": order_id, "intent_id": signed_intent["intent_id"]})
    _save(order)
    return order


def propose_cart(order_id: str, signed_cart: dict[str, Any], comparison: list[dict[str, Any]] | None = None) -> Order:
    """Called by the worker (via HTTP) once it has assembled a cart. Moves
    draft/revising -> proposed. The cart must trace to the current intent
    (or its latest amendment) — checked here, not trusted from the caller."""
    killswitch.require_not_killed("propose_cart")
    order = load(order_id)
    if order.state not in (State.DRAFT, State.REVISING):
        raise IllegalTransition(f"cannot propose a cart while order is {order.state.value}")
    if signed_cart["intent_id"] != order.intent["intent_id"]:
        raise ApprovalRefused("cart's intent_id does not match this order's intent")

    order.cart = signed_cart
    order.cart_version = signed_cart.get("cart_version", 1)
    order.comparison = comparison
    audit_log.append("order.cart_proposed", {
        "order_id": order_id, "cart_id": signed_cart["cart_id"],
        "cart_version": order.cart_version, "landed_total_inr": signed_cart["landed_total_inr"],
    })
    _transition(order, State.PROPOSED)
    _save(order)
    return order


# -------------------------------------------------------------- revision ---

def request_revision(order_id: str, *, instruction_text: str, requested_by: str = "gourab") -> Order:
    """Human feedback on a proposed cart. Amends the intent (never edits the
    cart in place — see grocery.mandates.amendment's module docstring), and
    moves proposed -> revising so the worker knows to re-plan.

    Bounded to config.MAX_REVISION_ROUNDS: a loop between a confused agent
    and a frustrated human must terminate rather than run forever.
    """
    killswitch.require_not_killed("request_revision")
    order = load(order_id)
    if order.state != State.PROPOSED:
        raise IllegalTransition(f"revision is only legal from 'proposed', order is {order.state.value}")
    if order.revision_rounds >= config.MAX_REVISION_ROUNDS:
        raise ApprovalRefused(
            f"this order has already had {order.revision_rounds} revision rounds "
            f"(max {config.MAX_REVISION_ROUNDS}) — please submit a fresh request"
        )

    prev_amendment = order.amendments[-1] if order.amendments else None
    # NOTE: `kind` and `detail` here are a placeholder classification; the
    # cart-reviser skill/agent is what actually interprets instruction_text
    # into a structured kind+detail in the full pipeline. This function's job
    # is only to mint and chain the signed amendment correctly regardless of
    # how it was classified.
    amendment = amendment_mandate.new_amendment(
        intent=order.intent, prev_amendment=prev_amendment,
        instruction_text=instruction_text, kind="other", detail={"raw": instruction_text},
        requested_by=requested_by,
    )
    amendment_mandate.persist(amendment)

    order.amendments.append(amendment)
    order.revision_rounds += 1
    audit_log.append("order.revision_requested", {
        "order_id": order_id, "amendment_id": amendment["amendment_id"],
        "instruction_text": instruction_text, "round": order.revision_rounds,
    })
    # Any revision invalidates the prior cart entirely — there is no path
    # where an approval (there isn't one yet, at this state) or a stale cart
    # survives into the next round.
    order.cart = None
    _transition(order, State.REVISING)
    _save(order)
    return order


def refuse_revision(order_id: str, *, reason: str) -> Order:
    """Used when a revision is itself illegal — e.g. it tries to raise the
    intent's price cap (revision_raised_cap) — rather than silently applying
    it or silently ignoring it. The refusal is as visible as an approval."""
    order = load(order_id)
    order.revision_rounds += 1
    audit_log.append("order.revision_refused", {"order_id": order_id, "reason": reason})
    _transition(order, State.REFUSED)
    _save(order)
    return order


# ---------------------------------------------------------------- approve ---

def approve_cart(order_id: str, *, approved_by: str, approval_factor: str) -> Order:
    killswitch.require_not_killed("approve_cart")
    order = load(order_id)
    if order.state != State.PROPOSED:
        raise IllegalTransition(f"cannot approve from {order.state.value}")
    if not order.cart:
        raise ApprovalRefused("no cart to approve")
    if not cart_mandate.hash_matches(order.cart):
        raise ApprovalRefused("cart_hash mismatch — refusing to approve a cart that has drifted")

    audit_log.append("order.cart_approved", {
        "order_id": order_id, "cart_id": order.cart["cart_id"],
        "cart_hash": order.cart["cart_hash"], "approved_by": approved_by,
        "approval_factor": approval_factor,
    })
    _transition(order, State.APPROVED)
    _save(order)
    return order


def approve_payment_and_execute(
    order_id: str, *, approved_by: str, approval_factor: str, rail, adapter,
) -> Order:
    """The single function that is allowed to move money. In order:
      1. kill switch,
      2. cart_hash has not drifted since cart-approval,
      3. a FRESH merchant-side cart is built and re-quoted right now — this is
         the actual price-drift check, not just a hash comparison, because a
         merchant can reprice between approval and payment even if nothing
         about our own record of the cart changed,
      4. budget caps, checked against the fresh quote, not the stale one,
      5. replay guard — this exact PaymentMandate has not already been spent.
    Fails closed on any of them — see tests/adversarial and tests/e2e#8."""
    killswitch.require_not_killed("approve_payment_and_execute")
    order = load(order_id)
    if order.state != State.APPROVED:
        raise IllegalTransition(f"cannot execute from {order.state.value}")
    if not cart_mandate.hash_matches(order.cart):
        raise ApprovalRefused("cart_hash mismatch at execute time — cart was mutated after approval")

    approved_total = order.cart["landed_total_inr"]

    # Re-build the real merchant cart now, at the last possible moment before
    # money moves, and re-quote it. This is where a surge-priced or
    # stocked-out item actually gets caught, not just a hash difference in
    # our own records.
    lines = [
        cart_mandate_CartLine(sku=li["sku"], qty=li["qty"])
        for li in order.cart["line_items"]
    ]
    try:
        cart_ref = adapter.build_cart(lines, authorized=True)
        fresh_quote = adapter.quote(cart_ref)
    except Exception as e:  # noqa: BLE001 — any merchant-side failure here must refuse, not proceed on stale data
        audit_log.append("order.requote_failed", {"order_id": order_id, "detail": str(e)})
        _transition(order, State.REFUSED)
        _save(order)
        raise ApprovalRefused(f"could not re-quote the cart at payment time: {e}")

    if abs(fresh_quote.landed_total_inr - approved_total) > 0.005:
        audit_log.append("order.price_drift_refused", {
            "order_id": order_id, "approved_total_inr": approved_total,
            "fresh_total_inr": fresh_quote.landed_total_inr,
        })
        _transition(order, State.REFUSED)
        _save(order)
        raise ApprovalRefused(
            f"price drifted since cart approval: was ₹{approved_total:.2f}, now "
            f"₹{fresh_quote.landed_total_inr:.2f} — refusing to charge a total you did not see"
        )

    amount = fresh_quote.landed_total_inr
    check = budget.check(amount)
    if not check.allowed:
        audit_log.append("order.execute_refused", {"order_id": order_id, "reason": check.reason})
        _transition(order, State.REFUSED)
        _save(order)
        raise ApprovalRefused(check.reason)

    pm = payment_mandate.new_payment_mandate(
        cart=order.cart, amount_inr=amount, rail=rail.rail_id,
        approved_by=approved_by, approval_factor=approval_factor,
    )
    payment_mandate.persist(pm)

    if ledger_store.already_spent(pm["payment_id"]):
        raise ApprovalRefused("replay detected — this PaymentMandate has already been spent")

    order.payment = pm
    audit_log.append("order.payment_approved", {
        "order_id": order_id, "payment_id": pm["payment_id"], "amount_inr": amount,
        "approved_by": approved_by, "approval_factor": approval_factor,
    })
    _transition(order, State.EXECUTING)
    _save(order)

    result = rail.pay(payment_mandate=pm, cart_ref=cart_ref, adapter=adapter)

    if not result.ok:
        audit_log.append("order.execute_failed", {"order_id": order_id, "detail": result.detail})
        _transition(order, State.REFUSED)
        _save(order)
        raise ApprovalRefused(result.detail)

    ledger_store.record_spend(payment_id=pm["payment_id"], cart_id=cart_ref, amount_inr=amount, rail=rail.rail_id)
    order.merchant_order_id = result.merchant_order_id
    audit_log.append("order.executed", {
        "order_id": order_id, "merchant_order_id": result.merchant_order_id, "amount_inr": amount,
    })
    _transition(order, State.DONE)
    _save(order)
    return order


# ------------------------------------------------------------------ abort ---

def abort(order_id: str, *, aborted_by: str, reason: str = "") -> Order:
    """Hard and terminal. Legal from any pre-approved state; once APPROVED,
    aborting still works (money hasn't moved yet) but once EXECUTING/DONE it
    does not — you cannot abort a payment that already happened."""
    killswitch.require_not_killed("abort")
    order = load(order_id)
    if order.state not in (State.DRAFT, State.PROPOSED, State.REVISING, State.APPROVED):
        raise IllegalTransition(f"cannot abort an order in state {order.state.value}")
    audit_log.append("order.aborted", {"order_id": order_id, "aborted_by": aborted_by, "reason": reason})
    _transition(order, State.ABORTED)
    _save(order)
    return order
