from hlperp.execution import PaperBroker
from hlperp.types import Signal


def _sig(side, tif, px):
    return Signal(coin="BTC", side=side, tif=tif, limit_px=px)


def test_paper_alo_rests_and_fills_on_crossing_print():
    b = PaperBroker("BTC")
    r = b.place(_sig("buy", "ALO", 99.0), sz=1.0, mark_px=100.0, sz_decimals=5)
    assert r.status == "resting" and r.oid is not None
    assert len(b.open_orders("BTC")) == 1
    # A print above our bid does not fill a bid resting at 99.
    assert b.on_print("BTC", 100.5, 1, is_buy=True) == []
    # A print at or below our bid does.
    fills = b.on_print("BTC", 98.9, 2, is_buy=False)
    assert len(fills) == 1
    assert fills[0].px == 99.0 and fills[0].crossed is False
    assert len(b.open_orders("BTC")) == 0


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


def test_paper_fees_are_charged():
    b = PaperBroker("BTC", maker_bps=1.5, taker_bps=4.5)
    taker = b.place(_sig("buy", "IOC", 100.0), 1.0, 100.0, 5)
    assert taker.raw["fill"]["fee"] > 0

    b2 = PaperBroker("BTC", maker_bps=1.5)
    b2.place(_sig("buy", "ALO", 99.0), 1.0, 100.0, 5)
    fill = b2.on_print("BTC", 98.5, 1, is_buy=False)[0]
    expected_maker = 1.0 * 99.0 * 1.5 / 10_000
    assert abs(fill.fee - expected_maker) < 1e-9
