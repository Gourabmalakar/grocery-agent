"""
Regression coverage for a real bug found by hand in the browser: the HTML
order page (/orders/{id}) 500'd on a fresh draft order because the template
wrote `o.intent.items`, which Jinja2 resolves to dict.items (the bound
method) before it ever tries the "items" key — a classic Jinja/dict
collision that /api/order/{id} (pure JSON, no template) never exercises.

The unit and adversarial suites all talk to the JSON API; nothing before
this test actually rendered a template. That gap is exactly why a passing
test suite still shipped a 500 on first real click-through — this file
closes it by hitting every human-facing page at least once, in every state
the state machine allows.
"""
import httpx


def _login(base_console: str) -> httpx.Client:
    client = httpx.Client(base_url=base_console, timeout=10)
    r = client.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})
    assert r.status_code == 200, r.text
    return client


def test_all_pages_render_across_order_states(live):
    client = _login(live["console"])

    # Static pages
    for path in ["/", "/ask", "/orders", "/control"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"

    r = client.post("/api/intent", json={"items": ["milk"], "max_total_inr": 800})
    order_id = r.json()["order_id"]

    # draft — no cart yet (this exact state is what 500'd before the fix)
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "draft" in r.text

    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["milk"], 800)

    # proposed — cart present, revise/approve/abort controls rendered
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "proposed" in r.text
    assert "Approve cart" in r.text

    client.post(f"/console/order/{order_id}/approve_cart")
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "approved" in r.text
    assert "Approve payment" in r.text

    client.post(f"/console/order/{order_id}/approve_payment")
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "done" in r.text
    assert "Ordered" in r.text

    # orders list must render with at least one row now
    r = client.get("/orders")
    assert r.status_code == 200, r.text
    assert order_id in r.text


def test_aborted_and_revising_pages_render(live):
    client = _login(live["console"])
    r = client.post("/api/intent", json={"items": ["milk"], "max_total_inr": 800})
    order_id = r.json()["order_id"]
    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, ["milk"], 800)

    client.post(f"/console/order/{order_id}/revise", json={"instruction_text": "make it 2"})
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "revising" in r.text

    worker_run._plan_and_propose(order_id, ["milk"], 800, cart_version=2, amendment_ids=[])
    r2 = client.get(f"/orders/{order_id}")
    assert r2.status_code == 200

    client.post(f"/console/order/{order_id}/abort", data={"reason": "changed my mind"})
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200, r.text
    assert "aborted" in r.text
