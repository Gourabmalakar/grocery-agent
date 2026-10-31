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

import secrets

import time
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from grocery import config, killswitch, location, narrator
from grocery.audit import log as audit_log
from grocery.audit import decisions as decision_log
from grocery.audit import verify as audit_verify
from grocery.ledger import budget, store as ledger_store
from grocery.sentinel import report as sentinel_report
from grocery.mandates import intent as intent_mandate
from grocery.mandates import cart as cart_mandate
from console import approval, revision
from console.auth import passphrase, session
from console.rails.mock_rail import MockRail
from grocery.adapters.mock import MockAdapter
from console import adapters as console_adapters
from console import choose_basket
from console import ordering
from console.rails import select as rail_select

app = FastAPI(title="grocery-console", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def _payment_preflight() -> None:
    """Refuse to serve with payments on and an unsafe configuration.

    Checked at STARTUP rather than at the first payment, because a
    misconfiguration discovered when somebody is trying to pay is discovered in
    the worst possible place. assert_safe_to_take_payments() is a no-op while
    PAYMENT_BACKEND=off, so this costs nothing until it matters.
    """
    config.assert_safe_to_take_payments()
    if config.payments_enabled():
        from console.auth import bot_passphrase
        bot_passphrase.provision_from_config()
        print(f"[console] payments={config.PAYMENT_BACKEND} "
              f"· methods={rail_select.available()}")
templates = Jinja2Templates(directory=str(config.ROOT / "console" / "templates"))

# NB: this mount was missing entirely through Phase 0 — every page linked
# `/static/style.css` but nothing ever served it, so the console rendered
# completely unstyled (a 404, silently swallowed by the <link> tag) from
# the very first commit. Found while visually verifying the redesigned UI;
# see tests/e2e/test_pages_render.py's new assertion that this now 200s.
app.mount("/static", StaticFiles(directory=str(config.ROOT / "console" / "static")), name="static")

SESSION_COOKIE = "grocery_console_session"


def _require_session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not authenticated — visit / and enter the passphrase")
    data = session.read_session_token(token)
    if not data:
        raise HTTPException(401, "session expired — re-enter the passphrase")
    return data


def _require_session_or_bot(request: Request) -> None:
    """A human session OR the bot's own token — for the routes that change what
    is being bought but cannot spend anything.

    Deliberately NOT usable for payment. `console_approve_payment` keeps
    `_require_session` + `_require_live_window`, so the bot's standing
    credential can revise a basket and freeze a cart, and can never buy it.
    That split is the whole reason the bot has a token of its own rather than
    Gourab's passphrase — see grocery/config.py::BOT_API_TOKEN.
    """
    offered = request.headers.get("X-Bot-Token", "")
    if offered and config.BOT_API_TOKEN and secrets.compare_digest(offered, config.BOT_API_TOKEN):
        return
    _require_session(request)


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
        "active_nav": "ask",
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
    # Whether the "Approve payment" button should exist at all. A button that
    # 409s when pressed teaches a human that the system is flaky; a card that
    # explains the boundary teaches them where it is.
    cart = order_json.get("cart") or {}
    execution_available = bool(cart) and console_adapters.is_executable(cart.get("merchant", ""))
    return templates.TemplateResponse(request, "order.html", {
        "active_nav": "orders",
        "order": order_json, "trace": trace, "assistant_message": assistant_message,
        "execution_available": execution_available,
    })


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    _require_session(request)
    orders = [o.to_json() for o in approval.list_orders()]
    return templates.TemplateResponse(request, "orders.html", {"active_nav": "orders", "orders": orders})


@app.get("/control", response_class=HTMLResponse)
def control_page(request: Request):
    _require_session(request)
    ks = killswitch.state()
    a_ok, a_msg = audit_verify.verify_audit_chain()
    d_ok, d_msg = audit_verify.verify_decision_chain()
    last_sweep = sentinel_report.last_sweep()
    return templates.TemplateResponse(request, "control.html", {
        "active_nav": "control",
        "mode": config.MODE,
        "kill_tier": ks.tier.value, "kill_reason": ks.reason,
        "audit_ok": a_ok, "audit_msg": a_msg, "decision_ok": d_ok, "decision_msg": d_msg,
        "last_sweep": last_sweep,
        "dead_mans_switch_tripped": sentinel_report.dead_mans_switch_tripped(),
        "spend_today": ledger_store.spend_today(), "cap_today": config.CAP_PER_DAY_INR,
        "spend_month": ledger_store.spend_this_month(), "cap_month": config.CAP_PER_MONTH_INR,
        "location": location.current(),
    })


@app.post("/console/sentinel/sweep")
def console_sentinel_sweep(request: Request):
    """Manual trigger for a transaction-sentinel sweep — until Phase 1's
    scheduler runs this on a cadence (see the plan's heartbeat section), a
    human can ask for one from the Control page. Read-only from the
    console's point of view: it can only ever report findings or, on a
    fatal one, trip PANIC — the same authority the sentinel always has."""
    _require_session(request)
    return sentinel_report.sweep()


@app.post("/console/location")
def console_set_location(
    request: Request,
    pincode: str = Form(...),
    lat: str = Form(""),
    lon: str = Form(""),
    label: str = Form(""),
):
    """Set the delivery address. Session-gated: where the groceries go is a
    decision for the person who pays, not for the agent — worker/tools.py has
    no route to this, by omission and on purpose.

    Audit-logged because it materially changes what can be bought and at what
    price: measured on JioMart, 560001 returns twelve Amul products and 110001
    returns none. An address change that left no trace would make a later
    price look inexplicable.
    """
    _require_session(request)
    try:
        here = location.set_location(
            pincode=pincode,
            lat=float(lat) if lat.strip() else None,
            lon=float(lon) if lon.strip() else None,
            label=label,
        )
    except location.InvalidLocation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="latitude and longitude must be numbers") from exc
    audit_log.append("location.changed", here.to_json())
    return here.to_json()


@app.get("/console/location")
def console_get_location(request: Request):
    _require_session(request)
    here = location.current()
    return {
        **here.to_json(),
        "merchants": {m: {"serviceable": here.supports(m), "why_not": here.missing_for(m)}
                      for m in ("blinkit", "jiomart", "zepto")},
    }


# --------------------------------------------------------------- health ----

@app.get("/health")
def health():
    """Following the family office's rule that a degraded backend must be
    visible rather than silently tolerated: mode, kill-switch state, both
    chains, budget, pending orders, and the sentinel's own freshness — a
    dead-man's-switch trip here means the sentinel itself has gone quiet,
    which per the plan is the failure this system is least allowed to have."""
    ks = killswitch.state()
    a_ok, _ = audit_verify.verify_audit_chain()
    d_ok, _ = audit_verify.verify_decision_chain()
    last_sweep = sentinel_report.last_sweep()
    dead_mans_switch = sentinel_report.dead_mans_switch_tripped()
    ok = a_ok and d_ok and ks.tier.value != "panic" and not dead_mans_switch
    return {
        "ok": ok, "mode": config.MODE, "kill_switch": ks.tier.value,
        "audit_chain_ok": a_ok, "decision_chain_ok": d_ok,
        "spend_today_inr": ledger_store.spend_today(), "spend_month_inr": ledger_store.spend_this_month(),
        "pending_orders": [o.order_id for o in approval.list_orders() if o.state.value in ("draft", "proposed", "revising", "approved", "executing")],
        "sentinel": {
            "last_swept_at": last_sweep["swept_at"] if last_sweep else None,
            "last_sweep_findings": last_sweep["by_severity"] if last_sweep else None,
            "dead_mans_switch_tripped": dead_mans_switch,
        },
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
    _require_session_or_bot(request)
    try:
        order = revision.submit_feedback(order_id, body.instruction_text)
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/order/{order_id}/approve_cart")
def console_approve_cart(request: Request, order_id: str):
    _require_session_or_bot(request)
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
    try:
        adapter = console_adapters.adapter_for(order.cart["merchant"], for_execution=True)
    except console_adapters.ExecutionUnavailable as e:
        # 409 and the order STAYS `approved` — deliberately not transitioned to
        # refused. Burning a cart the human just approved is their call, not a
        # side effect of hitting a boundary they could not have known about.
        audit_log.append("order.execution_unavailable", {
            "order_id": order_id, "merchant": order.cart["merchant"], "reason": str(e)})
        raise HTTPException(409, str(e))
    try:
        order = approval.approve_payment_and_execute(
            order_id, approved_by="gourab", approval_factor="passphrase+live_window",
            rail=rail, adapter=adapter,
        )
    except (approval.IllegalTransition, approval.ApprovalRefused) as e:
        raise HTTPException(409, str(e))
    return order.to_json()


@app.post("/console/order/{order_id}/choose_basket")
def console_choose_basket(request: Request, order_id: str, merchant: str = Form(...)):
    """Take a different store's basket than the one that was recommended.

    A human choice between options the worker already produced — see
    console/choose_basket.py for why that adds no authority and why it does not
    go through the worker."""
    _require_session_or_bot(request)
    order = approval.load(order_id)
    if order.state != approval.State.PROPOSED:
        raise HTTPException(409, f"a basket can only be chosen while the order is proposed, "
                                 f"not {order.state.value}")
    if order.cart and order.cart.get("merchant") == merchant:
        raise HTTPException(409, f"{merchant} is already the cart for this order")
    try:
        cart = choose_basket.build_cart(order, merchant,
                                        cart_version=(order.cart_version or 1) + 1)
    except choose_basket.BasketUnavailable as e:
        raise HTTPException(409, str(e))

    cart_mandate.persist(cart)
    audit_log.append("order.basket_chosen_by_human", {
        "order_id": order_id, "merchant": merchant,
        "previous_merchant": (order.cart or {}).get("merchant"),
        "landed_total_inr": cart["landed_total_inr"],
    })
    decision_log.record(
        trace_id=order_id, actor="console", action="basket_chosen_by_human",
        reasoning=f"Gourab chose {merchant}'s basket over the recommended "
                  f"{(order.cart or {}).get('merchant')}. No price was re-read and no item "
                  f"changed: this is the basket the worker already assembled for {merchant}, "
                  f"re-minted as cart v{cart['cart_version']}.",
        confidence="high",
    )
    # Back through the same door the worker uses, so the lineage and state checks
    # in propose_cart apply identically to a human-chosen cart.
    order = approval.request_revision(
        order_id, instruction_text=f"use {merchant}'s basket instead",
        requested_by="gourab")
    order = approval.propose_cart(
        order_id, cart, comparison=order.comparison,
        narrative=narrator.narrate_basket_chosen(cart=cart, comparison=order.comparison or []),
    )
    return order.to_json()


def _execution_adapter_for(order):
    """What execute() re-quotes against. Real merchants get a shim that reads
    their already-built basket; everything else keeps the mock path."""
    merchant = (order.cart or {}).get("merchant", "")
    if merchant.lower() == "swiggy":
        built = order.merchant_cart or {}
        if not built.get("cart_ref"):
            raise HTTPException(409, "there is no built Swiggy basket to pay for")
        from console.swiggy_execution import SwiggyExecutionAdapter
        from grocery.adapters.swiggy_mcp import _access_token
        return SwiggyExecutionAdapter(token_provider=_access_token,
                                      cart_ref=built["cart_ref"])
    return console_adapters.adapter_for(merchant, for_execution=True)


@app.post("/console/order/{order_id}/pay")
def console_pay(request: Request, order_id: str,
                method: str = Form(...), passphrase: str = Form(""),
                cart_hash: str = Form("")):
    """Pay for an approved basket, by one of exactly two methods.

    THE BOT'S TOKEN IS NOT ENOUGH HERE, and that is the point. Every other
    bot-reachable route changes what is being bought and can spend nothing;
    this one can move money, so it demands the bot token AND a passphrase that
    only Gourab knows, verified here rather than in the bot process. The bot
    relays; the Console decides. Compromising the bot yields the ability to
    ask.

    `cart_hash` is required and must match the basket on file: the approval was
    for a specific basket, and a passphrase typed about a ₹451 cart must not
    authorise a ₹4,510 one that replaced it in between.
    """
    _require_session_or_bot(request)
    config.assert_safe_to_take_payments()

    order = approval.load(order_id)
    if not order.cart:
        raise HTTPException(409, "no cart to pay for")
    if cart_hash and cart_hash != cart_mandate.hash_of(order.cart):
        raise HTTPException(409, "the basket changed since you were asked to "
                                 "approve it — look at it again")

    # A human session that is inside its live window has already proved who it
    # is; a bot relay has to carry the passphrase.
    from console.auth import bot_passphrase
    via_bot = bool(request.headers.get("X-Bot-Token"))
    if via_bot and not bot_passphrase.verify(passphrase):
        audit_log.append("order.payment_passphrase_refused", {"order_id": order_id})
        raise HTTPException(403, "passphrase did not verify")

    try:
        rail = rail_select.rail_for(method)
    except rail_select.RailUnavailable as e:
        raise HTTPException(409, str(e))

    try:
        order = approval.approve_payment_and_execute(
            order_id, approved_by="gourab",
            approval_factor="bot_passphrase" if via_bot else "console_session",
            rail=rail, adapter=_execution_adapter_for(order),
        )
    except approval.ApprovalRefused as e:
        raise HTTPException(409, str(e))

    return {"state": order.state.value, "rail": rail.rail_id,
            "merchant_order_id": order.merchant_order_id,
            "amount_inr": (order.payment or {}).get("amount_inr")}


@app.get("/console/payment_methods")
def console_payment_methods(request: Request):
    """What the chat is allowed to offer. Driven by config, so the two surfaces
    cannot disagree about whether autonomous payment is on."""
    _require_session_or_bot(request)
    return {"available": rail_select.available(),
            "backend": config.PAYMENT_BACKEND}


@app.post("/console/order/{order_id}/build_cart")
def console_build_cart(request: Request, order_id: str):
    """Put the approved basket into the merchant's own cart, for real.

    This is a WRITE to Gourab's merchant account, and it is as far as this
    system goes: the cart surface it uses has no checkout and no order tool, so
    the worst outcome is a basket he did not want, which `cancel` takes back.
    Paying still happens in the merchant's app, by him.

    Allowed on the bot's token because it spends nothing. The cart must already
    be APPROVED — the point of building it is to act on a decision he made, not
    to make one.
    """
    _require_session_or_bot(request)
    order = approval.load(order_id)
    if order.state != approval.State.APPROVED:
        raise HTTPException(409, f"the cart must be approved before it is built "
                                 f"at the merchant (it is {order.state.value})")
    if not order.cart:
        raise HTTPException(409, "no cart to build")
    if order.merchant_cart:
        return order.merchant_cart          # idempotent: one basket, not three

    try:
        built = ordering.build(order.cart)
    except ordering.CannotBuild as e:
        audit_log.append("order.merchant_cart_refused", {
            "order_id": order_id, "merchant": order.cart.get("merchant"), "reason": str(e)})
        raise HTTPException(409, str(e))

    order.merchant_cart = built.to_json()
    approval._save(order)
    audit_log.append("order.merchant_cart_built", {
        "order_id": order_id, "merchant": built.merchant, "cart_ref": built.cart_ref,
        "total_inr": built.totals.total_inr})
    decision_log.record(
        trace_id=order_id, actor="console", action="merchant_cart_built",
        reasoning=f"built the approved basket in the real {built.merchant} account as "
                  f"{built.cart_ref}; merchant totals ₹{built.totals.total_inr:.2f}. "
                  f"Nothing has been paid — the cart surface used has no checkout tool.",
        evidence=[built.to_json()], confidence="high")
    return order.merchant_cart


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
