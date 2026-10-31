"""The bot's view of the Console. Deliberately small.

Mirrors `worker/tools.py`'s discipline: the whole interface the bot is allowed
to use lives here, so what the bot can do is a thing you can read in one screen
rather than infer from call sites.

It can create an order, read one, revise it, approve a CART, and switch baskets.
It cannot approve a payment — `/console/order/{id}/approve_payment` requires a
human session inside a live window and refuses the bot token by construction
(`console/app.py::_require_session_or_bot`). That refusal is tested, not assumed.
"""
from __future__ import annotations

from typing import Any

import requests

from grocery import config

_TIMEOUT = 20


def _headers() -> dict[str, str]:
    return {"X-Bot-Token": config.BOT_API_TOKEN} if config.BOT_API_TOKEN else {}


def create_order(items: list[str], max_total_inr: int) -> str:
    r = requests.post(f"{config.CONSOLE_URL}/api/intent",
                      json={"items": items, "max_total_inr": max_total_inr},
                      timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()["order_id"]


def get_order(order_id: str) -> dict[str, Any]:
    r = requests.get(f"{config.CONSOLE_URL}/api/order/{order_id}", timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def revise(order_id: str, instruction: str) -> tuple[bool, str]:
    r = requests.post(f"{config.CONSOLE_URL}/console/order/{order_id}/revise",
                      json={"instruction_text": instruction},
                      headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 200:
        return True, ""
    return False, _detail(r)


def approve_cart(order_id: str) -> tuple[bool, str]:
    r = requests.post(f"{config.CONSOLE_URL}/console/order/{order_id}/approve_cart",
                      headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 200:
        return True, ""
    return False, _detail(r)


def choose_basket(order_id: str, merchant: str) -> tuple[bool, str]:
    r = requests.post(f"{config.CONSOLE_URL}/console/order/{order_id}/choose_basket",
                      data={"merchant": merchant}, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 200:
        return True, ""
    return False, _detail(r)


def build_cart(order_id: str) -> tuple[bool, dict | str]:
    """Put the approved basket into the merchant's real account. Spends nothing
    — the cart surface has no checkout tool."""
    r = requests.post(f"{config.CONSOLE_URL}/console/order/{order_id}/build_cart",
                      headers=_headers(), timeout=120)
    if r.status_code == 200:
        return True, r.json()
    return False, _detail(r)


def abort(order_id: str, reason: str = "cancelled from chat") -> tuple[bool, str]:
    r = requests.post(f"{config.CONSOLE_URL}/console/order/{order_id}/abort",
                      data={"reason": reason}, headers=_headers(), timeout=_TIMEOUT)
    return (True, "") if r.status_code == 200 else (False, _detail(r))


def _detail(response: requests.Response) -> str:
    try:
        return str(response.json().get("detail") or response.text)[:300]
    except ValueError:
        return response.text[:300]


def payment_methods() -> list[str]:
    """What the Console will actually accept. Asked rather than assumed, so the
    chat cannot offer a button the Console refuses."""
    # NARROW, AND THE REASON IS A BUG THIS EXACT FUNCTION SHIPPED WITH. It was
    # written against a module global `_BASE` that does not exist here, so every
    # call raised NameError — and a blanket `except Exception: return []` turned
    # that into "this Console offers no payment methods". The payment buttons
    # simply never appeared, with no error anywhere, while the route itself
    # answered 200.
    #
    # A network failure is a legitimate empty answer. A programming error is
    # not, and must be loud.
    try:
        response = requests.get(f"{config.CONSOLE_URL}/console/payment_methods",
                                headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        print(f"[bot] could not reach the Console for payment methods: {exc}")
        return []
    if response.status_code >= 400:
        print(f"[bot] payment methods refused: {response.status_code} "
              f"{response.text[:120]}")
        return []
    return list(response.json().get("available") or [])


def pay(order_id: str, *, method: str, passphrase: str,
        cart_hash: str) -> tuple[bool, str | dict]:
    """Relay a payment request. THE PASSPHRASE PASSES THROUGH AND IS NOT KEPT.

    It is a parameter, sent once over loopback, and never assigned to anything
    that outlives this call — no module global, no log line, no retry buffer.
    The Console verifies it; this process only carries it.
    """
    try:
        response = requests.post(
            f"{config.CONSOLE_URL}/console/order/{order_id}/pay",
            data={"method": method, "passphrase": passphrase, "cart_hash": cart_hash},
            headers=_headers(), timeout=120,
        )
    except Exception as exc:                         # noqa: BLE001
        # A timeout here means the payment outcome is UNKNOWN, never "failed".
        return False, (f"the Console did not answer in time, so whether the "
                       f"payment went through is unknown: {str(exc)[:160]}")
    if response.status_code == 403:
        return False, "passphrase"
    if response.status_code >= 400:
        return False, _detail(response)
    return True, response.json()
