"""Throwaway side-by-side: same query, both real merchants, one table.

NOT part of the product and NOT the order flow — the worker still runs on
mockstore. This exists only so the real adapters can be eyeballed together
until the worker refactor lands.

    ./.venv/bin/python compare_demo.py "amul gold milk 1l" "tata salt 1kg"
"""
import sys
from grocery import location
from grocery.adapters.jiomart_api import JioMartApiAdapter
from grocery.adapters.blinkit_browser import BlinkitBrowserAdapter

loc = location.current()
print(f"delivering to: {loc.label}  ({loc.pincode}, {loc.lat},{loc.lon})\n")

for query in sys.argv[1:]:
    print("=" * 78)
    print(f"  {query}")
    print("=" * 78)
    for name, make in (("jiomart", JioMartApiAdapter), ("blinkit", BlinkitBrowserAdapter)):
        try:
            rows = make().search(query)
        except Exception as exc:
            print(f"\n  {name}: unavailable — {type(exc).__name__}: {exc}")
            continue
        print(f"\n  {name} — {len(rows)} listings")
        for r in rows[:10]:
            pack = f"{r.pack_qty:g}{r.pack_unit}" if r.pack_qty else "?"
            print(f"    {r.name[:44]:<44} {pack:>8}  Rs {r.price_inr:>7.2f}  [{r.price_source}]")
    print()
