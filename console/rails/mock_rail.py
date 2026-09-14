"""
Phase 0-1 rail: pays through mockstore. No real money, no real credential —
this is the rail every adversarial and chaos test runs against, so those
tests are runnable with zero external dependencies.
"""
from __future__ import annotations

from typing import Any

from console.rails.base import PaymentRail, PayResult


class MockRail(PaymentRail):
    rail_id = "mock"

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str, adapter: Any) -> PayResult:
        try:
            order_id = adapter.place_order(cart_ref, payment_mandate["payment_id"], authorized=True)
            return PayResult(ok=True, detail="mock payment settled", merchant_order_id=order_id)
        except Exception as e:  # noqa: BLE001 — a failed mock payment must fail closed, not raise past the caller
            return PayResult(ok=False, detail=f"mock rail failed: {e}")
