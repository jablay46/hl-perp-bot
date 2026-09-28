"""Backtester tests: fully offline, driven by synthetic candles and funding."""

from hlperp.backtest import Backtester, _FundingPoint
from hlperp.config import load_config


def _cfg(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return load_config()


def _candles(closes):
    rows = []
    for i, c in enumerate(closes):
        rows.append({"t": 1_700_000_000_000 + i * 60_000, "o": c, "h": c * 1.001,
                     "l": c * 0.999, "c": c, "v": 10.0})
    return rows


def test_backtest_runs_offline_and_reports_metrics(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="IOC",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10)
    closes = [100 + i * 0.5 for i in range(20)] + [110 - i * 0.5 for i in range(20)]
    funding = [_FundingPoint(r["t"], 0.0001) for r in _candles(closes)]
    bt = Backtester(cfg)
    res = bt.run(candles=_candles(closes), funding=funding)
    assert res.candles == len(closes)
    assert res.decisions > 0
    assert res.orders > 0
    assert res.fills > 0
    assert res.fees > 0
    # A long held through positive funding bleeds.
    assert res.funding > 0
    d = res.as_dict()
    assert set(d) >= {"return_pct", "max_drawdown_pct", "win_rate"}


def test_backtest_requires_enough_candles(monkeypatch):
    cfg = _cfg(monkeypatch, HL_MIN_LIQ_DISTANCE_PCT=0)
    bt = Backtester(cfg)
    try:
        bt.run(candles=_candles([100.0, 101.0]), funding=[])
    except ValueError as exc:
        assert "candles" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_backtest_respects_lot_size_and_venue_minimum(monkeypatch):
    """A $200 account at BTC's 5-decimal lot sizes to $9.96 and must not order.

    Regression: the backtest used to hardcode 2 decimals, so sizes were silently
    rounded to a different lot than the venue enforces.
    """
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="IOC",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10)
    closes = [83000 + i * 5 for i in range(30)]
    small = Backtester(cfg, base_equity=200.0, sz_decimals=5)
    res_small = small.run(candles=_candles(closes), funding=[])
    assert res_small.orders == 0  # below the $10 minimum at this lot size
    assert res_small.fills == 0

    # The same replay with enough equity does trade.
    big = Backtester(cfg, base_equity=100_000.0, sz_decimals=5)
    res_big = big.run(candles=_candles(closes), funding=[])
    assert res_big.orders > 0
