---
name: adapter-writer
description: How to build a new StorefrontAdapter — mock, browser, or an API like Swiggy's Instamart MCP — so it's interchangeable with every other adapter. Use when adding a new merchant or repairing a broken one.
---

# Adapter Writer

## The contract

Implement `grocery.adapters.base.StorefrontAdapter` exactly:
`search`, `get_item`, `build_cart`, `quote`, `place_order`. Read methods are
always callable. Write methods (`build_cart`, `place_order`) **must** raise
`PermissionError` unless called with `authorized=True` — and nothing in a
worker-facing code path should ever set that flag; only the broker does, at
execute time.

A new adapter is "working" when it passes `tests/adapters/` — the same
conformance suite every adapter, mock included, is held to. That is the
whole bar; do not argue for a site-specific exception to it.

## Method

1. Start from `grocery/adapters/mock.py` as the reference shape.
2. Every `Listing` needs `price_source` and `asof` set from the actual
   moment of reading — never backfilled or defaulted to "now" after the
   fact if the read happened earlier.
3. For a browser adapter, capture a fixture (`tests/fixtures/`) of the page
   structure you're parsing, so a later break is caught as a fixture diff
   rather than a wrong price in production.
4. For an API adapter (e.g. Swiggy MCP), map its native response shape into
   `Listing`/`Quote` faithfully — do not invent fields it doesn't provide;
   an absent field stays absent (and unsourced_price fires) rather than
   being guessed.

## Rules

- Never read a listing's free-text fields for anything decision-relevant —
  see `injection-defense`.
- A repaired adapter must still pass the full conformance suite, not just
  the specific case that was broken.
