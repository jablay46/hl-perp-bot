from hlperp.rounding import round_px, round_sz, wire


def test_size_rounds_to_lot():
    assert round_sz(0.00014, 5) == 0.00014
    assert round_sz(1.0001, 3) == 1.0
    assert round_sz(0.12349, 2) == 0.12


def test_price_five_significant_figures():
    # 1234.56 has 6 sig figs -> invalid; must collapse to 5.
    assert round_px(1234.56, 1) == 1234.6
    assert round_px(0.001234, 0) == 0.001234


def test_price_respects_perp_decimal_ceiling():
    # szDecimals=1 -> at most 6-1 = 5 decimal places.
    assert round_px(0.012345, 1) == 0.01235
    # integer prices stay integer
    assert round_px(123456, 5) == 123456


def test_wire_has_no_trailing_zeros_or_exponent():
    assert wire(0.001234) == "0.001234"
    assert wire(100.0) == "100"
    assert wire(0.10) == "0.1"
