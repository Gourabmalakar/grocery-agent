from grocery.compare.normalise import normalise


def test_kg_vs_g_comparable():
    a = normalise(5, "kg", 285)      # ₹57/kg
    b = normalise(1, "kg", 62)       # ₹62/kg
    assert a.price_per_100_base < b.price_per_100_base


def test_pack_of_n_with_size_prefix():
    # QuickBasket's "500ml" pack_unit with pack_qty=2 means 2x500ml = 1000ml
    n = normalise(2, "500ml", 68)
    assert n.base_kind == "volume_ml"
    assert n.base_quantity == 1000
    assert abs(n.price_per_base_unit - 0.068) < 1e-9


def test_count_units():
    a = normalise(12, "count", 84)   # ₹7/egg
    b = normalise(6, "count", 45)    # ₹7.5/egg
    assert a.price_per_base_unit < b.price_per_base_unit


def test_unknown_unit_does_not_crash():
    n = normalise(1, "bottle", 100)
    assert n.base_kind == "unknown"
    assert n.price_per_base_unit == 100
