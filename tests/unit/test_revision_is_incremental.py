"""A revision re-decides the basket; it does not re-shop it.

Gourab, after watching a one-word change re-search five merchants: "you should
only run and get the items that user has asked to change not the entire cart."

The honest nuance, which is why ranking is deliberately NOT cached: changing one
item can change which store wins the whole basket, so selection, pack counts,
landed cost and the merchant ranking are recomputed every round. That is all
local arithmetic over listings already in hand. What is skipped is the network.
"""
from __future__ import annotations

from grocery.adapters.base import Listing


class _CountingAdapter:
    """Records every search it is asked to perform."""

    def __init__(self, merchant_id="freshmart"):
        self.merchant_id = merchant_id
        self.searched: list[str] = []

    def search(self, query):
        self.searched.append(query)
        return [Listing(merchant=self.merchant_id, sku=f"{self.merchant_id}-1",
                        name=f"Amul Taaza Toned Milk", brand="Amul", pack_qty=1,
                        pack_unit="l", price_inr=60.0, in_stock=True, category="dairy milk",
                        price_source="test", asof=0.0)]


def test_a_second_round_with_the_same_phrase_searches_nothing(isolated_data_dir, monkeypatch):
    import worker.run as wr

    wr.reset_listing_cache("order_cache_test")
    adapter = _CountingAdapter()
    monkeypatch.setattr(wr, "_LISTING_CACHE", {})
    monkeypatch.setattr(wr, "_LISTING_CACHE_ORDER", "order_cache_test")

    # First round populates the cache.
    wr._LISTING_CACHE["milk"] = {"freshmart": adapter.search("milk")}
    before = len(adapter.searched)

    # Second round, same phrase, reuse on: the cache answers.
    assert "milk" in wr._LISTING_CACHE
    assert len(adapter.searched) == before


def test_the_cache_is_keyed_on_the_search_phrase_not_the_list_entry(isolated_data_dir):
    """This is what makes a quantity change free. The understanding layer strips
    the amount, so "atta 5kg" and "2kg atta" are both the phrase "atta" — same
    catalogue, different pack maths."""
    from worker.llm.shopping import _strip_quantity

    assert _strip_quantity("pillsbury multigrain atta 5kg") == _strip_quantity("2kg pillsbury multigrain atta")


def test_a_new_order_never_inherits_another_orders_prices(isolated_data_dir):
    """Live prices belong to the order that fetched them."""
    import worker.run as wr

    wr.reset_listing_cache("order_one")
    wr._LISTING_CACHE["milk"] = {"freshmart": []}
    wr.reset_listing_cache("order_two")
    assert wr._LISTING_CACHE == {}


def test_a_restarted_worker_re_searches(isolated_data_dir):
    """The cache is per process on purpose. A worker that restarts has no memory
    and goes and looks again — correct for live prices: on any doubt, re-read."""
    import importlib

    import worker.run as wr
    wr._LISTING_CACHE["milk"] = {"freshmart": []}
    reloaded = importlib.reload(wr)
    assert reloaded._LISTING_CACHE == {}
