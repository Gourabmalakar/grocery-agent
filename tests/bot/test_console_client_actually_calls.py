"""Every console_client function must actually run.

THE BUG THIS EXISTS FOR. `payment_methods()` and `pay()` were written against a
module global `_BASE` that does not exist in this module — it uses
`config.CONSOLE_URL`. Every call raised NameError, and `payment_methods()`
caught it with a blanket `except Exception: return []`, which the conversation
read as "this Console offers no payment methods". So the payment buttons never
appeared, no error reached any log, and the route itself answered 200 to curl.

The existing bot tests all monkeypatch these functions, so none of them execute
a single line of their bodies. That is why 413 green tests said nothing. This
file calls them for real against a stubbed transport — the HTTP is fake, the
function bodies are not.

It is the same failure as `test_entrypoints_actually_run.py`: a name that is
only resolved at call time, in a path no test calls.
"""
from __future__ import annotations

import pytest


class _Response:
    status_code = 200
    text = "{}"

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _stub_requests(monkeypatch, payload=None):
    """Replace the transport, not the function under test."""
    import requests
    from bot import console_client

    calls = []

    def _get(url, **kwargs):
        calls.append(("GET", url))
        return _Response(payload)

    def _post(url, **kwargs):
        calls.append(("POST", url))
        return _Response(payload)

    monkeypatch.setattr(console_client.requests, "get", _get)
    monkeypatch.setattr(console_client.requests, "post", _post)
    return calls


def test_payment_methods_runs_and_returns_what_the_console_said(isolated_data_dir, monkeypatch):
    from bot import console_client

    calls = _stub_requests(monkeypatch, {"available": ["app", "wallet"],
                                         "backend": "wallet"})
    assert console_client.payment_methods() == ["app", "wallet"]
    assert calls and calls[0][1].endswith("/console/payment_methods")


def test_pay_runs_and_posts_to_the_pay_route(isolated_data_dir, monkeypatch):
    from bot import console_client

    calls = _stub_requests(monkeypatch, {"state": "done", "amount_inr": 64.0})
    ok, result = console_client.pay("order_x", method="wallet",
                                    passphrase="secret", cart_hash="HASH1")
    assert ok is True
    assert result["state"] == "done"
    assert calls[0][1].endswith("/console/order/order_x/pay")


def test_a_programming_error_is_not_swallowed_into_an_empty_answer(isolated_data_dir, monkeypatch):
    """The specific regression. A NameError inside payment_methods() must not
    come back as "no payment methods available" — it must raise, loudly."""
    from bot import console_client

    def _boom(*a, **k):
        raise NameError("name '_BASE' is not defined")

    monkeypatch.setattr(console_client.requests, "get", _boom)
    with pytest.raises(NameError):
        console_client.payment_methods()


def test_a_genuine_network_failure_is_still_an_empty_answer(isolated_data_dir, monkeypatch):
    """The Console being down is a legitimate reason to offer no buttons."""
    import requests
    from bot import console_client

    def _down(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(console_client.requests, "get", _down)
    assert console_client.payment_methods() == []
