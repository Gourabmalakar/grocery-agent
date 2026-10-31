"""
Returns the right set of adapters based on MERCHANT_BACKEND config.

mock  — the three mock merchants (freshmart, quickbasket, dailygrocer) via
        mockstore HTTP. Default, and what every test uses.
real  — whichever real merchants are currently reachable (JioMart, Blinkit,
        Zepto, Swiggy Instamart). Each adapter that fails to initialize is
        logged and skipped, never fatal.
"""
from __future__ import annotations

from grocery import config
from grocery.adapters.base import StorefrontAdapter


class _Skip(Exception):
    """A merchant turned off on purpose. Caught by the same handler that
    catches an unreachable one, so a disabled merchant takes exactly the
    path a broken one does and cannot accidentally stay in the list."""


def adapters_for_backend(backend: str | None = None) -> list[StorefrontAdapter]:
    backend = backend or config.MERCHANT_BACKEND
    if backend == "mock":
        from grocery.adapters.mock import all_mock_adapters
        return all_mock_adapters()
    if backend == "real":
        return _real_adapters()
    raise ValueError(f"unknown MERCHANT_BACKEND: {backend!r}")


def fee_metadata(backend: str | None = None) -> dict:
    """Fee schedules for merchants that publish them. Mock merchants have
    known fees (from /merchants); real merchants do not — their fees
    require building a cart, which is a write we never do for price
    comparison. Returns an empty dict for backends with no fee data."""
    backend = backend or config.MERCHANT_BACKEND
    if backend == "mock":
        from grocery.adapters.mock import fetch_merchant_meta
        return fetch_merchant_meta()
    return {}


def _real_adapters() -> list[StorefrontAdapter]:
    adapters: list[StorefrontAdapter] = []

    def disabled(merchant: str) -> bool:
        if merchant in config.MERCHANTS_DISABLED:
            print(f"[backends] {merchant} disabled by MERCHANTS_DISABLED — not searched")
            return True
        return False

    try:
        if disabled("jiomart"):
            raise _Skip()
        from grocery.adapters.jiomart_api import JioMartApiAdapter
        adapters.append(JioMartApiAdapter())
    except _Skip:
        pass
    except Exception as e:
        print(f"[backends] jiomart unavailable: {e}")

    try:
        if disabled("blinkit"):
            raise _Skip()
        from grocery.adapters.blinkit_browser import BlinkitBrowserAdapter
        from grocery import location
        # shopping_locus(), not current(): the worker is the process CLAUDE.md
        # rule 1 says holds no delivery address, and handing it a type with no
        # label field is how that stays true instead of being remembered.
        loc = location.shopping_locus()
        if not loc.has_coordinates:
            print("[backends] blinkit skipped: no lat/lon set for the delivery area")
        else:
            adapters.append(BlinkitBrowserAdapter())
    except _Skip:
        pass
    except Exception as e:
        print(f"[backends] blinkit unavailable: {e}")

    try:
        if disabled("zepto"):
            raise _Skip()
        from grocery.adapters.zepto_mcp import ZeptoMCPAdapter
        adapters.append(ZeptoMCPAdapter())
    except _Skip:
        pass
    except Exception as e:
        print(f"[backends] zepto unavailable: {e}")

    try:
        if disabled("swiggy"):
            raise _Skip()
        from grocery.adapters.swiggy_mcp import SwiggyMCPAdapter
        adapters.append(SwiggyMCPAdapter())
    except _Skip:
        pass
    except Exception as e:
        print(f"[backends] swiggy unavailable: {e}")

    return adapters
