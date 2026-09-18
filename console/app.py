"""
The Console — FastAPI app that IS the broker. Holds the passphrase check,
owns approval.py's state machine, and is the only process with any path to
a payment rail. The worker (a separate OS process) only ever calls the
`/api/*` routes below; it never imports console.approval or console.rails
directly — see worker/tools.py, which enforces that separation on the
worker side too, so the boundary holds even if someone imports carelessly.

Run: python -m console.app   (serves on :8800)
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from grocery import config, killswitch, narrator
from grocery.audit import log as audit_log
from grocery.audit import decisions as decision_log
from grocery.audit import verify as audit_verify
from grocery.ledger import budget, store as ledger_store
from grocery.mandates import intent as intent_mandate
from grocery.mandates import cart as cart_mandate
from console import approval, revision
from console.auth import passphrase, session
from console.rails.mock_rail import MockRail
from grocery.adapters.mock import MockAdapter

app = FastAPI(title="grocery-console", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(config.ROOT / "console" / "templates"))

SESSION_COOKIE = "grocery_console_session"


def _require_session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not authenticated — visit / and enter the passphrase")
    data = session.read_session_token(token)
    if not data:
        raise HTTPException(401, "session expired — re-enter the passphrase")
    return data


def _require_live_window(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not session.is_within_live_window(token):
        raise HTTPException(
            403,
            "payment approval needs a fresh passphrase entry (within the last 30 minutes)",
        )


# ------------------------------------------------------------------ pages ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    first_run = not passphrase.is_set()
    return templates.TemplateResponse(request, "index.html", {"first_run": first_run})


@app.post("/auth/setup")
def auth_setup(passphrase_value: str = Form("")):
    if passphrase.is_set():
        raise HTTPException(409, "passphrase already set")
    if len(passphrase_value) < 8:
        raise HTTPException(422, "passphrase must be at least 8 characters")
    passphrase.set_passphrase(passphrase_value)
    audit_log.append("console.passphrase_set", {})
    token = session.make_session_token()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/auth/login")
def auth_login(passphrase_value: str = Form("")):
    if not passphrase.verify(passphrase_value):
        audit_log.append("console.login_failed", {})
        raise HTTPException(401, "incorrect passphrase")
    audit_log.append("console.login_ok", {})
    token = session.make_session_token()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    _require_session(request)
    ks = killswitch.state()
    return templates.TemplateResponse(request, "ask.html", {
        "mode": config.MODE,
        "kill_tier": ks.tier.value,
        "remaining_today": config.CAP_PER_DAY_INR - ledger_store.spend_today(),
        "remaining_month": config.CAP_PER_MONTH_INR - ledger_store.spend_this_month(),
    })


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_page(request: Request, order_id: str):
    _require_session(request)
    order = approval.load(order_id)
    trace = decision_log.trace(order_id)
    order_json = order.to_json()
    assistant_message = narrator.narrate_for_order(order_json)
    return templates.TemplateResponse(request, "order.html", {
        "order": order_json, "trace": trace, "assistant_message": assistant_message,
    })


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    _require_session(request)
    orders = [o.to_json() for o in approval.list_orders()]
    return templates.TemplateResponse(request, "orders.html", {"orders": orders})


@app.get("/control", response_class=HTMLResponse)
def control_page(request: Request):
    _require_session(request)
    ks = killswitch.state()
    a_ok, a_msg = audit_verify.verify_audit_chain()
    d_ok, d_msg = audit_verify.verify_decision_chain()
    return templates.TemplateResponse(request, "control.html", {
        "kill_tier": ks.tier.value, "kill_reason": ks.reason,
        "audit_ok": a_ok, "audit_msg": a_msg, "decision_ok": d_ok, "decision_msg": d_msg,
    })


# --------------------------------------------------------------- health ----

@app.get("/health")
def health():
    ks = killswitch.state()
    a_ok, _ = audit_verify.verify_audit_chain()
    d_ok, _ = audit_verify.verify_decision_chain()
    return {
        "ok": True, "mode": config.MODE, "kill_switch": ks.tier.value,
        "audit_chain_ok": a_ok, "decision_chain_ok": d_ok,
        "spend_today_inr": ledger_store.spend_today(), "spend_month_inr": ledger_store.spend_this_month(),
        "pending_orders": [o.order_id for o in approval.list_orders() if o.state.value in ("draft", "proposed", "revising", "approved", "executing")],
    }


# --------------------------------------------------------- worker-facing ---
# Everything under /api/* is what worker/run.py calls. Nothing here can move
# money — that only happens via /console/* routes below, which require an
# authenticated human session and (for payment) the live window.

class IntentIn(BaseModel):
    items: list[str]
    max_total_inr: int
    category_allowlist: list[str] | None = None


@app.post("/api/intent")
def api_new_intent(body: IntentIn):
    """Human-initiated only in the real flow (the Ask page calls this), but
    exposed here as the same endpoint so worker and console share one code
    path — the worker never calls this itself; it receives an order_id."""
    signed = intent_mandate.new_intent(items=body.items, max_total_inr=body.max_total_inr,
                                        category_allowlist=body.category_allowlist)
    intent_mandate.persist(signed)
    order = approval.create_order(signed)
    return {"order_id": order.order_id, "intent": signed}


@app.get("/api/order/{order_id}")
def api_get_order(order_id: str):
    try:
        order = approval.load(order_id)
    except KeyError:
        raise HTTPException(404, "no such order")
    return order.to_json()


class CartProposeIn(BaseModel):
    cart: dict[str, Any]
    comparison: list[dict[str, Any]] | None = None
    narrative: str | None = None


@app.post("/api/order/{order_id}/propose_cart")
def api_propose_cart(order_id: str, body: CartProposeIn):
    """The worker's ONLY write path — it can propose a cart, nothing else.
    No route here mints a PaymentMandate or calls a rail."""
    try:
        order = approval.propose_cart(order_id, body.cart, body.comparison, body.narrative)
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


class DecisionIn(BaseModel):
    trace_id: str
    actor: str
    action: str
    reasoning: str
    evidence: list[dict] = []
    alternatives_rejected: list[dict] = []
    confidence: str = "medium"
    mandate_ref: str | None = None
    parent_span_id: str | None = None


@app.post("/api/decisions")
def api_record_decision(body: DecisionIn):
    entry = decision_log.record(**body.model_dump())
    return {"span_id": entry["span_id"]}


@app.get("/api/killswitch")
def api_killswitch():
    ks = killswitch.state()
    return {"tier": ks.tier.value, "reason": ks.reason}


# ------------------------------------------------------------- human-only ---

class RevisionIn(BaseModel):
    instruction_text: str


@app.post("/console/order/{order_id}/revise")
def console_revise(request: Request, order_id: str, body: RevisionIn):
    _require_session(request)
    try:
        order = revision.submit_feedback(order_id, body.instruction_text)
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/order/{order_id}/approve_cart")
def console_approve_cart(request: Request, order_id: str):
    _require_session(request)
    try:
        order = approval.approve_cart(order_id, approved_by="gourab", approval_factor="passphrase")
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/order/{order_id}/approve_payment")
def console_approve_payment(request: Request, order_id: str):
    _require_session(request)
    _require_live_window(request)
    order = approval.load(order_id)
    rail = MockRail()
    adapter = MockAdapter(order.cart["merchant"])
    try:
        order = approval.approve_payment_and_execute(
            order_id, approved_by="gourab", approval_factor="passphrase+live_window",
            rail=rail, adapter=adapter,
        )
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/order/{order_id}/abort")
def console_abort(request: Request, order_id: str, reason: str = Form("")):
    _require_session(request)
    try:
        order = approval.abort(order_id, aborted_by="gourab", reason=reason)
    except approval.IllegalTransition as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/killswitch/pause")
def console_pause(request: Request, reason: str = ""):
    _require_session(request)
    killswitch.trip_pause(reason=reason or "paused from console", set_by="gourab")
    return {"ok": True}


@app.post("/console/killswitch/panic")
def console_panic(request: Request, reason: str = ""):
    _require_session(request)
    killswitch.trip_panic(reason=reason or "panic from console", set_by="gourab")
    return {"ok": True}


@app.post("/console/killswitch/clear_panic")
def console_clear_panic(request: Request, reason: str, passphrase_value: str):
    _require_session(request)
    if not passphrase.verify(passphrase_value):
        raise HTTPException(401, "incorrect passphrase")
    if not reason.strip():
        raise HTTPException(422, "a typed reason is required to clear PANIC")
    killswitch.clear_panic(cleared_by="gourab", typed_reason=reason)
    return {"ok": True}


@app.post("/console/killswitch/clear_pause")
def console_clear_pause(request: Request):
    _require_session(request)
    killswitch.clear_pause(cleared_by="gourab")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    from urllib.parse import urlparse
    from grocery import config
    port = urlparse(config.CONSOLE_URL).port or 8800
    uvicorn.run("console.app:app", host="127.0.0.1", port=port, reload=False)
