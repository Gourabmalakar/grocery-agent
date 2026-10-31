"""Resolve payments whose outcome was never established.

WHEN AN INTENT IS LEFT UNSETTLED, the system is saying: money may or may not
have moved, and I will not guess. That is the correct answer at the moment it
happens and a useless one to live with, so this closes the loop the only way it
can be closed honestly — by asking the merchant what orders exist.

    python -m tools.reconcile            report only, changes nothing
    python -m tools.reconcile --apply    settle or clear each intent

WHAT IT WILL NOT DO. It never places, cancels or retries anything. It reads
Swiggy's order history and compares. If an order matching the intent exists,
the intent is settled and the spend recorded — the money was owed all along and
the caps must know. If no order exists, the intent is cleared and the order
refused, because a basket that was never placed must not block a fresh attempt
at the same items.

AMBIGUITY STAYS AMBIGUOUS. If the history cannot be read, nothing is changed
and it says so. A reconciler that resolves doubt by assuming is worse than one
that leaves the doubt visible.
"""
from __future__ import annotations

import sys


def _swiggy_orders() -> list[dict]:
    from grocery.adapters.swiggy_mcp import _access_token
    from grocery.mcp.client import McpClient, payload_of

    client = McpClient("https://mcp.swiggy.com/im", client_name="grocery-agent",
                       token_provider=_access_token)
    client.connect()
    payload = payload_of(client.call_tool("get_orders", {})) or {}
    return list(payload.get("orders") or [])


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    from console import approval
    from grocery.ledger import store

    pending = store.unsettled()
    if not pending:
        print("nothing to reconcile — no unsettled payment intents")
        return 0

    try:
        orders = _swiggy_orders()
    except Exception as exc:  # noqa: BLE001
        print(f"could not read Swiggy's order history: {exc}")
        print("NOTHING CHANGED. Resolve this by hand rather than guessing.")
        return 1

    print(f"{len(pending)} unsettled intent(s); Swiggy reports {len(orders)} order(s)\n")
    for intent in pending:
        order_id = intent["order_id"]
        amount = float(intent["amount_inr"])
        # Match on amount: these intents are minutes old and the totals are
        # exact to the paisa, so a same-amount order is the one.
        match = next((o for o in orders
                      if abs(float(o.get("totalAmount") or o.get("amount") or 0) - amount) < 0.01),
                     None)

        if match:
            merchant_order_id = str(match.get("orderId") or match.get("id") or "")
            print(f"  {order_id}  ₹{amount:.2f}  FOUND at Swiggy ({merchant_order_id})")
            if apply:
                store.settle_intent(intent["key"], merchant_order_id=merchant_order_id,
                                    detail="reconciled against order history")
                store.record_spend(payment_id=intent["payment_id"],
                                   cart_id=intent["cart_hash"], amount_inr=amount,
                                   rail=intent["rail"])
                order = approval.load(order_id)
                order.merchant_order_id = merchant_order_id
                approval._transition(order, approval.State.DONE)
                approval._save(order)
                print("     -> settled, spend recorded, order marked done")
        else:
            print(f"  {order_id}  ₹{amount:.2f}  NOT at Swiggy — nothing was placed")
            if apply:
                store.fail_intent(intent["key"], detail="no matching order at the merchant")
                order = approval.load(order_id)
                if order.state is approval.State.NEEDS_RECONCILIATION:
                    approval._transition(order, approval.State.REFUSED)
                    approval._save(order)
                print("     -> intent cleared, order refused, nothing charged")

    if not apply:
        print("\nreport only. Re-run with --apply to act on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
