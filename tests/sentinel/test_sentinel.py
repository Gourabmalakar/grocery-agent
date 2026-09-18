"""
Suite G from the plan: inject one anomaly per check class and assert
transaction-sentinel's mechanical half (grocery/sentinel/) detects it,
assigns the right severity, and — for fatal findings — that a sweep trips
PANIC. Also covers the dead-man's switch.

Builds order/ledger/mandate/audit-log state directly on disk rather than
running a full order through a live worker+HTTP round trip: every check
under test reads from files (order JSON, the SQLite ledger, both hash-chain
logs, issued mandates), so constructing that shape directly is faster and
just as real a test of the check functions as driving it through HTTP would
be — the HTTP path is what tests/e2e/ already exists to prove.
"""
from __future__ import annotations

import time

import pytest


def _make_order(items=("milk",), max_total_inr=800):
    from grocery.mandates import intent as intent_m
    from console import approval
    signed_intent = intent_m.new_intent(items=list(items), max_total_inr=max_total_inr)
    intent_m.persist(signed_intent)
    return approval.create_order(signed_intent)


def _make_cart(order, *, merchant="freshmart", landed_total=100.0):
    from grocery.mandates import cart as cart_m
    cart = cart_m.new_cart(
        intent_id=order.intent["intent_id"], merchant=merchant,
        line_items=[{"sku": f"{merchant}-milk-1l", "qty": 1, "intent_ref": order.intent["intent_id"],
                     "price_source": "test", "asof": 0, "price_inr": landed_total}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        landed_total_inr=landed_total,
    )
    cart_m.persist(cart)
    return cart


def _backdate(order_id: str, ts: float):
    """console.approval._save() always stamps updated_at to "now" on write,
    by design (it's meant to reflect the last real state change) — so
    simulating a stale order needs to poke the file on disk directly,
    after the fact, rather than fight that via the public API."""
    import json
    from console import approval
    path = approval._path(order_id)
    data = json.loads(path.read_text())
    data["updated_at"] = ts
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _write_payment_mandate(payment_id: str, cart: dict, amount: float):
    from grocery.mandates.signing import sign, save_issued
    payload = {
        "type": "PaymentMandate", "payment_id": payment_id, "cart_id": cart["cart_id"],
        "cart_hash": cart["cart_hash"], "amount_inr": amount, "rail": "mock",
        "approved_by": "test", "approval_factor": "passphrase", "approved_at": time.time(),
    }
    signed = sign(payload)
    save_issued("payment", payment_id, signed)


def test_clean_state_has_no_findings(isolated_data_dir):
    from grocery.sentinel import checks
    from console import approval

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.DONE
    payment_id = "pay_clean_1"
    order.payment = {"payment_id": payment_id, "amount_inr": 100.0, "rail": "mock"}
    approval._save(order)

    from grocery.ledger import store as ledger_store
    ledger_store.record_spend(payment_id=payment_id, cart_id=cart["cart_id"], amount_inr=100.0, rail="mock")
    _write_payment_mandate(payment_id, cart, 100.0)

    findings = checks.run_all()
    assert findings == [], f"expected no findings on clean state, got: {[f.message for f in findings]}"


def test_ledger_drift_is_fatal(isolated_data_dir):
    """The one thing that must never happen: money the order files don't
    know about, or vice versa."""
    from grocery.sentinel import checks
    from console import approval
    from grocery.ledger import store as ledger_store

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.DONE
    order.payment = {"payment_id": "pay_drift_1", "amount_inr": 100.0, "rail": "mock"}
    approval._save(order)
    # Deliberately record a DIFFERENT amount in the ledger than the order
    # file claims was paid — this is the anomaly under test.
    ledger_store.record_spend(payment_id="pay_drift_1", cart_id=cart["cart_id"], amount_inr=250.0, rail="mock")

    findings = checks.check_reconciliation()
    assert len(findings) == 1
    assert findings[0].severity == "fatal"
    assert findings[0].check == "reconciliation"


def test_payment_without_mandate_is_fatal(isolated_data_dir):
    from grocery.sentinel import checks
    from console import approval
    from grocery.ledger import store as ledger_store

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.DONE
    order.payment = {"payment_id": "pay_orphan_1", "amount_inr": 100.0, "rail": "mock"}
    approval._save(order)
    ledger_store.record_spend(payment_id="pay_orphan_1", cart_id=cart["cart_id"], amount_inr=100.0, rail="mock")
    # Deliberately skip writing the PaymentMandate file — this must never
    # happen (console/approval.py always mints one before recording spend),
    # so finding it missing is the anomaly under test.

    findings = checks.check_orphans()
    fatal = [f for f in findings if f.severity == "fatal"]
    assert len(fatal) == 1
    assert "PaymentMandate" in fatal[0].message


def test_stuck_executing_is_critical(isolated_data_dir):
    from grocery.sentinel import checks
    from console import approval

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.EXECUTING
    approval._save(order)
    _backdate(order.order_id, time.time() - 20 * 60)  # 20 minutes ago, past the 10-minute threshold

    findings = checks.check_orphans()
    assert any(f.severity == "critical" and "executing" in f.message.lower() for f in findings)


def test_stuck_approved_is_warn(isolated_data_dir):
    from grocery.sentinel import checks
    from console import approval

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order = approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")
    _backdate(order.order_id, time.time() - 30 * 3600)  # 30 hours ago, past the 24h threshold

    findings = checks.check_orphans()
    assert any(f.severity == "warn" and order.order_id in f.message for f in findings)


def test_chain_tamper_is_fatal(isolated_data_dir):
    from grocery import config
    from grocery.audit import log as audit_log
    from grocery.sentinel import checks
    import json

    audit_log.append("test.event", {"a": 1})
    audit_log.append("test.event", {"a": 2})

    # Tamper with the most recent entry's detail without recomputing its
    # hash — exactly what a corrupted or edited log looks like.
    paths = sorted(config.AUDIT_DIR.glob("*.jsonl"))
    lines = paths[-1].read_text().strip().splitlines()
    last = json.loads(lines[-1])
    last["detail"] = {"a": 999}
    lines[-1] = json.dumps(last)
    paths[-1].write_text("\n".join(lines) + "\n")

    findings = checks.check_chain_integrity()
    assert len(findings) == 1
    assert findings[0].severity == "fatal"
    assert findings[0].evidence["chain"] == "audit"


def test_reasoning_vs_fact_flagged_when_cart_not_cheapest(isolated_data_dir):
    """The comparison itself says another eligible merchant was cheaper, but
    the cart is with a pricier one and no min-order exception is recorded —
    a real, checkable mismatch between what was claimed and what was done."""
    from grocery.sentinel import checks
    from console import approval

    order = _make_order()
    cart = _make_cart(order, merchant="freshmart", landed_total=500.0)
    comparison = [
        {"merchant": "quickbasket", "rank": 1, "landed_total_inr": 300.0,
         "below_min_order": False, "cheapest_by_landed_cost": True},
        {"merchant": "freshmart", "rank": 2, "landed_total_inr": 500.0,
         "below_min_order": False, "cheapest_by_landed_cost": False},
    ]
    order = approval.propose_cart(order.order_id, cart, comparison=comparison)

    findings = checks.check_reasoning_vs_fact()
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].evidence["cheapest_merchant"] == "quickbasket"


def test_reasoning_vs_fact_allows_legitimate_min_order_override(isolated_data_dir):
    """The one legitimate reason to skip the cheapest merchant: it's below
    its own minimum order. This must NOT be flagged."""
    from grocery.sentinel import checks
    from console import approval

    order = _make_order()
    cart = _make_cart(order, merchant="freshmart", landed_total=500.0)
    comparison = [
        {"merchant": "quickbasket", "rank": 1, "landed_total_inr": 300.0,
         "below_min_order": True, "cheapest_by_landed_cost": True},
        {"merchant": "freshmart", "rank": 2, "landed_total_inr": 500.0,
         "below_min_order": False, "cheapest_by_landed_cost": False},
    ]
    approval.propose_cart(order.order_id, cart, comparison=comparison)

    assert checks.check_reasoning_vs_fact() == []


def test_adapter_health_flags_high_miss_rate(isolated_data_dir):
    from grocery.audit import decisions as decision_log
    from grocery.sentinel import checks

    trace_id = "ord_adapter_test"
    for _ in range(6):
        decision_log.record(trace_id=trace_id, actor="price-scout", action="item_unavailable",
                             reasoning="not in stock", evidence=[{"source": "flakymart"}], confidence="high")
    decision_log.record(trace_id=trace_id, actor="price-scout", action="select_item",
                         reasoning="found one", evidence=[{"source": "flakymart"}], confidence="high")

    findings = checks.check_adapter_health()
    assert any(f.evidence.get("merchant") == "flakymart" for f in findings)


def test_adapter_health_does_not_flag_a_thin_but_reliable_catalogue(isolated_data_dir):
    from grocery.audit import decisions as decision_log
    from grocery.sentinel import checks

    trace_id = "ord_adapter_test2"
    for _ in range(6):
        decision_log.record(trace_id=trace_id, actor="price-scout", action="select_item",
                             reasoning="found one", evidence=[{"source": "steadymart"}], confidence="high")

    findings = checks.check_adapter_health()
    assert not any(f.evidence.get("merchant") == "steadymart" for f in findings)


def test_behavioral_drift_flags_unusually_large_order(isolated_data_dir):
    from grocery.sentinel import baseline, checks
    from console import approval

    now = time.time()
    for i in range(6):
        baseline.record_order(order_id=f"hist_{i}", amount_inr=100.0, merchant="freshmart", ts=now - i * 86400)

    order = _make_order()
    cart = _make_cart(order, merchant="freshmart", landed_total=5000.0)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.DONE
    order.payment = {"payment_id": "pay_spike", "amount_inr": 5000.0, "rail": "mock"}
    approval._save(order)

    findings = checks.check_behavioral_drift()
    assert any(f.evidence.get("order_id") == order.order_id for f in findings)


def test_behavioral_drift_silent_with_too_little_history(isolated_data_dir):
    """Fewer than 5 historical orders — not enough to say what's normal —
    must produce no drift findings at all, not false alarms."""
    from grocery.sentinel import checks
    assert checks.check_behavioral_drift() == []


def test_sweep_trips_panic_on_fatal_finding(isolated_data_dir):
    from grocery import killswitch
    from grocery.ledger import store as ledger_store
    from grocery.sentinel import report
    from console import approval

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order.state = approval.State.DONE
    order.payment = {"payment_id": "pay_panic_test", "amount_inr": 100.0, "rail": "mock"}
    approval._save(order)
    ledger_store.record_spend(payment_id="pay_panic_test", cart_id=cart["cart_id"], amount_inr=999.0, rail="mock")

    assert killswitch.state().tier == killswitch.Tier.NONE
    summary = report.sweep()
    assert summary["tripped_panic"] is True
    assert killswitch.state().tier == killswitch.Tier.PANIC


def test_sweep_does_not_trip_panic_on_warn_only(isolated_data_dir):
    from grocery import killswitch
    from grocery.sentinel import report
    from console import approval

    order = _make_order()
    cart = _make_cart(order)
    order = approval.propose_cart(order.order_id, cart)
    order = approval.approve_cart(order.order_id, approved_by="x", approval_factor="passphrase")
    order.updated_at = time.time() - 30 * 3600
    approval._save(order)

    summary = report.sweep()
    assert summary["tripped_panic"] is False
    assert killswitch.state().tier == killswitch.Tier.NONE


def test_dead_mans_switch_trips_when_sentinel_has_never_run(isolated_data_dir):
    from grocery.sentinel import report
    assert report.last_sweep() is None
    assert report.dead_mans_switch_tripped() is True


def test_dead_mans_switch_clears_after_a_fresh_sweep(isolated_data_dir):
    from grocery.sentinel import report
    report.sweep()
    assert report.dead_mans_switch_tripped() is False


def test_dead_mans_switch_trips_again_once_stale(isolated_data_dir, monkeypatch):
    from grocery.sentinel import report
    report.sweep()
    assert report.dead_mans_switch_tripped() is False
    # Simulate the clock moving past the staleness window rather than
    # actually sleeping for hours in a test.
    real_time = time.time
    monkeypatch.setattr(report.time, "time", lambda: real_time() + report.DEAD_MANS_SWITCH_HOURS * 3600 + 60)
    assert report.dead_mans_switch_tripped() is True
