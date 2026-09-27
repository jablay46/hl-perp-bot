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
