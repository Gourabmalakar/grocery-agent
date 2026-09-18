"""
Scenario 1 from the plan's test table: simple order, no revision, approve,
pay — through the REAL HTTP boundary (two real processes), proving the
worker/console separation actually holds end to end, not just in Python
object composition.
"""
import time

import httpx


def _login(base_console: str) -> httpx.Client:
    client = httpx.Client(base_url=base_console, timeout=10)
    r = client.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})
    assert r.status_code == 200, r.text
    return client


def test_full_order_happy_path(live):
    client = _login(live["console"])

    r = client.post("/api/intent", json={"items": ["milk"], "max_total_inr": 800})
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]

    # Run the worker's planning function directly in-process (it only talks
    # HTTP to the console, exactly as the real `python -m worker.run` would).
    # The `live` fixture purged worker/grocery/console from sys.modules, so
    # this import binds fresh against the URLs it just set.
    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["milk"], 800)

    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed"
    assert order["cart"] is not None
    assert order["comparison"] is not None
    # A single-item "milk" order legitimately clears only one merchant's
    # minimum order (FreshMart ₹199, QuickBasket ₹99) — dailygrocer (min ₹0)
    # is the only one that can actually fulfil it, and it should win.
    assert len(order["comparison"]) >= 1
    assert order["cart"]["merchant"] == "dailygrocer"

    r = client.post(f"/console/order/{order_id}/approve_cart")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved"

    r = client.post(f"/console/order/{order_id}/approve_payment")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["state"] == "done"
    assert done["merchant_order_id"]
    assert done["payment"]["amount_inr"] == done["cart"]["landed_total_inr"]


def test_revision_then_approve(live):
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["biscuits"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["biscuits"], 800)

    order = client.get(f"/api/order/{order_id}").json()
    first_cart_id = order["cart"]["cart_id"]

    r = client.post(f"/console/order/{order_id}/revise", json={"instruction_text": "Amul not Britannia"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "revising"

    order = client.get(f"/api/order/{order_id}").json()
    hints = worker_run._interpret_revision("Amul not Britannia")
    worker_run._plan_and_propose(
        order_id, ["biscuits"], 800,
        brand_swap=hints["brand_swap"],
        cart_version=order["cart_version"] + 1,
        amendment_ids=[a["amendment_id"] for a in order["amendments"]],
    )

    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed"
    assert order["cart"]["cart_id"] != first_cart_id
    assert order["cart_version"] == 2
    assert all(li["brand"].lower() == "amul" for li in order["cart"]["line_items"])

    r = client.post(f"/console/order/{order_id}/approve_cart")
    assert r.status_code == 200, r.text


def test_brand_swap_scoped_to_affected_item_only(live):
    """Regression test for a real bug caught by hand in the browser: a brand
    swap meant for one item category ("Amul not Britannia", about biscuits)
    must not silently drop every other item whose brand also isn't Amul
    (Mother Dairy milk, generic eggs, Aashirvaad/Fortune atta). Before the
    fix, _plan_and_propose applied forced_brand as a blanket filter and the
    revised cart lost three of four items without any error — a textbook
    case of the over-interpretation cart-reviser's skill file warns against."""
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["milk", "eggs", "atta 5kg", "biscuits"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["milk", "eggs", "atta 5kg", "biscuits"], 800)
    order = client.get(f"/api/order/{order_id}").json()
    assert len(order["cart"]["line_items"]) == 4  # all four items present before any revision

    client.post(f"/console/order/{order_id}/revise", json={"instruction_text": "Amul not Britannia"})
    order = client.get(f"/api/order/{order_id}").json()
    hints = worker_run._interpret_revision("Amul not Britannia")
    worker_run._plan_and_propose(
        order_id, ["milk", "eggs", "atta 5kg", "biscuits"], 800,
        brand_swap=hints["brand_swap"],
        cart_version=order["cart_version"] + 1,
        amendment_ids=[a["amendment_id"] for a in order["amendments"]],
    )

    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed"
    skus = {li["sku"] for li in order["cart"]["line_items"]}
    # All four items must still be present — the brand swap should have
    # touched only the biscuit line, not caused milk/eggs/atta to vanish.
    assert len(order["cart"]["line_items"]) == 4, (
        f"brand swap dropped items outside its scope — only {len(order['cart']['line_items'])} "
        f"of 4 items remain: {skus}"
    )
    biscuit_line = next(li for li in order["cart"]["line_items"] if "cookie" in li["name"].lower())
    assert biscuit_line["brand"].lower() == "amul"


def test_requested_quantity_is_actually_bought(live):
    """Regression test for a real bug found by hand: 'milk, eggs, atta 5kg'
    ignored the leading numbers entirely and always bought exactly 1 pack —
    'atta 5kg' and plain 'atta' produced identical carts. Now the requested
    amount decides how many packs of the chosen listing to buy."""
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["30 eggs"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["30 eggs"], 800)

    order = client.get(f"/api/order/{order_id}").json()
    line = order["cart"]["line_items"][0]
    # Whatever pack size won, qty * pack_qty must cover at least 30 eggs.
    assert line["qty"] * line["pack_qty"] >= 30, (
        f"asked for 30 eggs, got qty={line['qty']} x {line['pack_qty']}-pack "
        f"= {line['qty'] * line['pack_qty']}"
    )


def test_ambiguous_feedback_does_not_silently_misfire_as_brand_swap(live):
    """Regression test for a real bug found by hand: 'I asked for 30 eggs
    not 6' was matched by the brand-swap regex as swapping FROM brand '6'
    TO brand 'eggs' — a false positive that silently did nothing (no real
    brand is '6') while leaving the actual complaint unaddressed and
    unexplained. The interpreter must not treat a bare digit as a brand,
    and must say plainly when it found nothing actionable."""
    from worker.run import _interpret_revision
    hints = _interpret_revision("also i asked for 30 eggs not 6")
    assert hints["brand_swap"] is None, f"false positive: {hints['brand_swap']}"


def test_unparseable_revision_is_flagged_not_silent(live):
    """The other half of the same bug: when nothing actionable can be
    extracted, the worker must record a decision saying so — not just
    re-propose an identical cart with no explanation anywhere."""
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["eggs"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["eggs"], 800)
    order = client.get(f"/api/order/{order_id}").json()

    instruction = "i need pilsbury multigrain, also 30 eggs not 6"
    client.post(f"/console/order/{order_id}/revise", json={"instruction_text": instruction})
    order = client.get(f"/api/order/{order_id}").json()
    hints = worker_run._interpret_revision(instruction)
    worker_run.note_if_revision_not_understood(order_id, instruction, hints)  # what main()'s loop does
    worker_run._plan_and_propose(
        order_id, ["eggs"], 800,
        brand_swap=hints["brand_swap"], drop_terms=hints["drop"],
        cart_version=order["cart_version"] + 1,
        amendment_ids=[a["amendment_id"] for a in order["amendments"]],
    )

    from grocery.audit import decisions as decision_log
    trace = decision_log.trace(order_id)
    assert any(s["action"] == "revision_not_understood" for s in trace), (
        "an unparseable revision must be flagged in the trace, not silently ignored"
    )


def test_worker_main_resumes_without_crashing_on_a_non_draft_order(live):
    """Regression test for a real crash found by hand: re-running
    `python -m worker.run <order_id>` against an order that had already been
    proposed (e.g. the worker died and was restarted, or was run twice)
    raised an unhandled 409 from propose_cart, because main() always called
    _plan_and_propose() unconditionally as if the order were fresh. main()
    must detect the order's actual state and resume appropriately instead."""
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["milk"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["milk"], 800)
    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed"

    # Simulate re-running the worker against this already-proposed order —
    # main()'s initial call must not attempt to propose again.
    import threading
    result = {}
    def run():
        try:
            worker_run.main(order_id)
            result["ok"] = True
        except Exception as e:  # noqa: BLE001
            result["ok"] = False
            result["error"] = str(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1)  # long enough to see a startup crash if there is one
    client.post(f"/console/killswitch/panic", data={"reason": "test cleanup"})  # let the poll loop exit on its own
    t.join(timeout=5)
    assert result.get("ok", True) is not False, f"main() crashed on a non-draft order: {result.get('error')}"


def test_worker_main_picks_up_a_pending_revision_on_start(live):
    """The other half: if a revision was submitted while no worker was
    running, starting the worker fresh must still pick it up — not treat
    the pre-existing amendment as already-seen and sit idle forever."""
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["biscuits"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["biscuits"], 800)
    client.post(f"/console/order/{order_id}/revise", json={"instruction_text": "Amul not Britannia"})
    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "revising"
    first_cart_version = order["cart_version"]

    import threading
    t = threading.Thread(target=worker_run.main, args=(order_id,), daemon=True)
    t.start()
    t.join(timeout=3)

    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed", "a fresh worker start must still act on a pending revision"
    assert order["cart_version"] == first_cart_version + 1
    assert all(li["brand"].lower() == "amul" for li in order["cart"]["line_items"])

    client.post(f"/console/killswitch/panic", data={"reason": "test cleanup"})  # let the still-polling thread exit
    t.join(timeout=5)
