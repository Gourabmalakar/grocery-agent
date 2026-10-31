"""What the bot ADVERTISES as cart-capable and what can actually be built must
be the same set.

THIS IS THE BUG THAT WASTED A DAY. `config.CART_CAPABLE_MERCHANTS` defaulted to
"zepto,swiggy", so bot/format.py marked a Swiggy basket buyable and offered a
"Build this cart" button for it. `console/ordering.py` had only Zepto in
`_UCP_ENDPOINTS`, so the moment the button was pressed it answered:

    there is no cart API for 'swiggy' — this system can read its prices and
    cannot put anything in a basket there

Both statements were true of their own module and the pair was a lie. The
failure arrived AFTER approval, phrased as though Swiggy had no cart API at all
— when in fact Swiggy has sixteen tools including `update_cart` and `get_cart`,
and it was this repo that had not wired them.

Two registers of the same fact will drift. This test is the join.
"""
from __future__ import annotations

from grocery import config
from console import ordering


def test_everything_advertised_as_buyable_can_actually_be_built():
    unbuildable = sorted(m for m in config.CART_CAPABLE_MERCHANTS
                         if not ordering.can_build(m))
    assert not unbuildable, (
        f"these merchants are offered to the user as cart-capable but "
        f"console/ordering.py cannot build them: {unbuildable}. Either wire a "
        f"cart builder or take them out of CART_CAPABLE_MERCHANTS — do not let "
        f"the user find out after he has approved a basket."
    )


def test_nothing_buildable_is_hidden_from_the_user():
    """The converse. A merchant we CAN cart at but never offer is dead code
    with a credential attached."""
    known = ("zepto", "swiggy", "jiomart", "blinkit")
    hidden = sorted(m for m in known
                    if ordering.can_build(m) and m not in config.CART_CAPABLE_MERCHANTS)
    assert not hidden, f"buildable but never offered: {hidden}"


def test_read_only_merchants_stay_read_only():
    """JioMart and Blinkit have no sanctioned write surface and never will —
    carting them needs a stored merchant password or a stealable cookie."""
    for merchant in ("jiomart", "blinkit"):
        assert not ordering.can_build(merchant)
        assert merchant not in config.CART_CAPABLE_MERCHANTS
