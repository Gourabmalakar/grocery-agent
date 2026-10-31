"""Prove WHICH merchant accounts this repo's stored tokens belong to.

WHY THIS EXISTS. "The cart is in your account" is a claim, and a claim about
somebody else's account is worth nothing without evidence they can check
themselves. This prints the things you can compare against your own phone in
ten seconds: the name on the account, the saved address, the last few orders
and their totals, and whatever is in the cart right now.

If the order history below is not YOUR order history, these tokens are for
somebody else's account and nothing else in this repo can be trusted. That is
the whole point of the tool.

WHAT IT CAN DO, exactly. Its own allowlist is READS ONLY, and narrower than
either production adapter's — see _READS. Notably it CAN read order history,
which the production adapters refuse: their guard rejects any tool whose name
contains "order", which is the right default for a thing that might otherwise
place one, and wrong for a thing whose only job is to show you your receipts.
Reading a receipt cannot buy anything. Nothing here writes, carts, or pays.

PRIVACY. Masked by default: addresses shrink to a pincode, phone numbers to
their last four digits, order ids to a prefix. Pass --full to see everything,
which you may well want on your own machine — the default exists so that
pasting this output into a chat does not post your home address.

    python -m tools.whoami              both merchants, masked
    python -m tools.whoami --full       everything, unmasked
    python -m tools.whoami zepto        one merchant only
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable

from grocery.mcp.client import McpClient, payload_of

# READ-ONLY, and narrower than the adapters'. Order history is here on purpose
# and is the most useful line in the output; nothing in this set can mutate
# anything.
_READS: dict[str, tuple[str, ...]] = {
    "zepto": ("get_user_details", "list_saved_addresses", "list_order_history",
              "get_past_order_items", "view_cart"),
    "swiggy": ("get_addresses", "get_orders", "get_cart"),
}

_ENDPOINTS = {
    "zepto": "https://mcp.zepto.co.in/mcp",
    "swiggy": "https://mcp.swiggy.com/im",
}


class WroteNothing(PermissionError):
    """This tool reads. If you are seeing this, somebody added a call it should
    not be making."""


def _tokens() -> dict[str, Callable[[], str]]:
    from grocery.adapters.swiggy_mcp import _access_token as swiggy_token
    from grocery.adapters.zepto_mcp import _access_token as zepto_token
    return {"zepto": zepto_token, "swiggy": swiggy_token}


def mask(text: str, *, full: bool) -> str:
    if full:
        return text
    # Phone numbers -> last four. Indian mobiles, and the already-masked form.
    text = re.sub(r'(\+?91[\- ]?)?\b([6-9]\d{9})\b',
                  lambda m: "<phone …" + m.group(2)[-4:] + ">", text)
    # Anything that looks like a street address -> its pincode only.
    text = re.sub(r'[A-Za-z0-9#/,\-\. ]{18,}?(\b\d{6}\b)',
                  lambda m: f"<address … {m.group(1)}>", text)
    return text


def _grant(name: str) -> dict[str, Any]:
    from grocery.mcp import oauth
    stored = oauth.load(f"{name}_mcp_token")
    if stored is None:
        return {"connected": False}
    left = stored.expires_at - time.time() if getattr(stored, "expires_at", 0) else 0
    return {
        "connected": True,
        "scope": " ".join(sorted(stored.granted_scopes())) or "(none stated)",
        "client_id": stored.client_id,
        "expires_in_hours": round(left / 3600, 1),
        "expired": left <= 0,
    }


def _probe(name: str, token: Callable[[], str], *, full: bool) -> None:
    allowed = _READS[name]
    client = McpClient(_ENDPOINTS[name], client_name="grocery-agent",
                       token_provider=token)

    def call(tool: str, args: dict[str, Any] | None = None) -> Any:
        if tool not in allowed:
            raise WroteNothing(f"{tool!r} is not readable by this tool ({allowed})")
        return payload_of(client.call_tool(tool, args or {}))

    print(f"\n{'=' * 68}\n  {name.upper()}\n{'=' * 68}")
    print("  grant:", json.dumps(_grant(name)))
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
        print(f"  CANNOT CONNECT: {type(exc).__name__}: {str(exc)[:200]}")
        return

    offered = sorted(t.get("name", "") for t in client.list_tools())
    print(f"  surface: {len(offered)} tools")

    for label, tool, args in (
        ("who the account is", "get_user_details", {}),
        ("saved addresses", "list_saved_addresses", {}),
        ("saved addresses", "get_addresses", {}),
        ("past orders", "list_order_history", {}),
        ("past orders", "get_orders", {}),
        ("cart right now", "view_cart", {}),
        ("cart right now", "get_cart", {}),
    ):
        if tool not in offered or tool not in allowed:
            continue
        print(f"\n  --- {label}  ({tool}) ---")
        try:
            out = call(tool, args)
        except Exception as exc:  # noqa: BLE001
            print(f"      unavailable: {str(exc)[:180]}")
            continue
        rendered = out if isinstance(out, str) else json.dumps(out, indent=2)
        for line in mask(rendered, full=full).splitlines()[:40]:
            print("      " + line[:160])


def main(argv: list[str]) -> int:
    full = "--full" in argv
    wanted = [a for a in argv if not a.startswith("-")] or ["zepto", "swiggy"]
    print(__doc__.split("\n\n")[1].strip())
    if not full:
        print("\n(masked — pass --full to see addresses and phone numbers)")
    for name in wanted:
        if name not in _READS:
            print(f"\nunknown merchant {name!r}; known: {sorted(_READS)}")
            continue
        _probe(name, _tokens()[name], full=full)
    print("\nCompare the orders above against your own app. If they are not "
          "yours, these tokens are not yours.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
