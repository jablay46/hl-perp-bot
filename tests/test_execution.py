from hlperp.execution import PaperBroker
from hlperp.types import Signal


def _sig(side, tif, px):
    return Signal(coin="BTC", side=side, tif=tif, limit_px=px)


def test_paper_alo_rests_and_fills_on_crossing_print():
    b = PaperBroker("BTC", participation=1.0)
    r = b.place(_sig("buy", "ALO", 99.0), sz=1.0, mark_px=100.0, sz_decimals=5)
    assert r.status == "resting" and r.oid is not None
    assert len(b.open_orders("BTC")) == 1
    # A print above our resting bid never fills it.
    assert b.on_print("BTC", 100.5, 1, is_buy=True) == []
    # A print at or below our bid does.
    fills = b.on_print("BTC", 98.9, 2, is_buy=False)
    assert len(fills) == 1
    assert fills[0].px == 99.0 and fills[0].crossed is False
    assert len(b.open_orders("BTC")) == 0


def test_paper_print_only_partially_fills_a_large_order():
    """One print cannot clear a large resting order; only a slice fills."""
    b = PaperBroker("BTC", participation=0.25)
    b.place(_sig("buy", "ALO", 99.0), sz=4.0, mark_px=100.0, sz_decimals=5)
    fills = b.on_print("BTC", 98.9, 1, is_buy=False)
    assert len(fills) == 1 and fills[0].sz == 1.0
    remaining = b.open_orders("BTC")
    assert len(remaining) == 1 and remaining[0].sz == 3.0


def test_paper_alo_refuses_to_cross():
    b = PaperBroker("BTC")
    # A buy at or above the mark would cross: post-only is rejected.
    r = b.place(_sig("buy", "ALO", 101.0), sz=1.0, mark_px=100.0, sz_decimals=5)
    assert r.status == "rejected"
    assert "cross" in (r.error or "")


def test_paper_ioc_fills_immediately_with_slippage():
    b = PaperBroker("BTC", slippage_bps=10.0)
    r = b.place(_sig("buy", "IOC", 100.0), sz=1.0, mark_px=100.0, sz_decimals=5)
    assert r.status == "filled"
    assert r.px > 100.0  # paid up
    assert b.position("BTC").size == 1.0


def test_paper_position_accounting_and_pnl():
    b = PaperBroker("BTC")
    b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)
    # Sell at a higher price closes the long for a profit.
    b.place(_sig("sell", "IOC", 110.0), 1.0, 110.0, 5)
    pos = b.position("BTC")
    assert pos.size == 0.0
    assert pos.realized > 0


def test_paper_bracket_fires_on_take_profit():
    b = PaperBroker("BTC")
    b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)  # taker long at 100
    long = b.position("BTC").size > 0
    assert long
    assert b.place_bracket("BTC", True, b.position("BTC").size, 110.0, 90.0) is True
    # A print above the take-profit closes the long.
    b.on_print("BTC", 111.0, 1, is_buy=True)
    assert b.position("BTC").size == 0
    assert b.position("BTC").realized > 0


def test_paper_bracket_fires_on_stop_loss():
    b = PaperBroker("BTC")
    b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)
    b.place_bracket("BTC", True, b.position("BTC").size, 110.0, 90.0)
    b.on_print("BTC", 89.0, 1, is_buy=False)
    assert b.position("BTC").size == 0
    assert b.position("BTC").realized < 0
    # The bracket is one-shot: a later print does nothing.
    b.on_print("BTC", 200.0, 2, is_buy=True)
    assert b.position("BTC").size == 0


def test_paper_bracket_can_be_cancelled():
    b = PaperBroker("BTC")
    b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)
    b.place_bracket("BTC", True, 1.0, 110.0, 90.0)
    b.cancel_brackets("BTC")
    b.on_print("BTC", 111.0, 1, is_buy=True)
    assert b.position("BTC").size == 1.0  # still open


def test_paper_fees_are_charged():
    b = PaperBroker("BTC", maker_bps=1.5, taker_bps=4.5)
    taker = b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)
    assert taker.raw["fill"]["fee"] > 0

    b2 = PaperBroker("BTC", maker_bps=1.5, participation=1.0)
    b2.place(_sig("buy", "ALO", 99.0), 1.0, 100.0, 5)
    fill = b2.on_print("BTC", 98.5, 1, is_buy=False)[0]
    expected_maker = 1.0 * 99.0 * 1.5 / 10_000
    assert abs(fill.fee - expected_maker) < 1e-9
