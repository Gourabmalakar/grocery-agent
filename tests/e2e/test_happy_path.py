"""
Scenario 1 from the plan's test table: simple order, no revision, approve,
pay — through the REAL HTTP boundary (two real processes), proving the
worker/console separation actually holds end to end, not just in Python
object composition.
"""
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
        forced_brand=hints["forced_brand"].get("_any_"),
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
