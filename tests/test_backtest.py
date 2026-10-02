"""Backtester tests: fully offline, driven by synthetic candles and funding."""

from hlperp.backtest import Backtester, _FundingPoint
from hlperp.config import load_config
from hlperp.rounding import MAX_DECIMALS_PERP


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
    # Positive funding is paid by longs and received by shorts, so the sign depends
    # on which side the replay happened to be holding. What must hold is that funding
    # was charged at all, i.e. the term is non-zero and consistent with the exposure.
    assert res.funding != 0.0
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


class _FakeInfo:
    def __init__(self, meta=None, err=None):
        self._meta = meta
        self._err = err

    def meta(self):
        if self._err is not None:
            raise self._err
        return self._meta


def test_resolve_sz_decimals_from_metadata(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC")
    bt = Backtester(cfg)
    monkeypatch.setattr(bt, "_info", lambda: _FakeInfo(
        meta={"universe": [{"name": "ETH", "szDecimals": 4}, {"name": "BTC", "szDecimals": 5}]}
    ))
    assert bt.resolve_sz_decimals() == 5
    assert bt.sz_decimals == 5  # cached only when resolved from metadata


def test_resolve_sz_decimals_unknown_coin_raises(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="NOPE")
    bt = Backtester(cfg)
    monkeypatch.setattr(bt, "_info", lambda: _FakeInfo(
        meta={"universe": [{"name": "BTC", "szDecimals": 5}]}
    ))
    try:
        bt.resolve_sz_decimals()
    except ValueError as exc:
        assert "NOPE" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown coin")


def test_resolve_sz_decimals_network_failure_falls_back_without_caching(monkeypatch, caplog):
    cfg = _cfg(monkeypatch, HL_COIN="BTC")
    bt = Backtester(cfg)
    monkeypatch.setattr(bt, "_info", lambda: _FakeInfo(err=OSError("offline")))
    with caplog.at_level("WARNING"):
        assert bt.resolve_sz_decimals() == MAX_DECIMALS_PERP
    assert "falling back" in caplog.text
    # Not cached, so a later success can still resolve the real lot size.
    assert bt.sz_decimals is None
    monkeypatch.setattr(bt, "_info", lambda: _FakeInfo(
        meta={"universe": [{"name": "BTC", "szDecimals": 5}]}
    ))
    assert bt.resolve_sz_decimals() == 5


def test_explicit_sz_decimals_skips_metadata(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC")
    bt = Backtester(cfg, sz_decimals=4)
    monkeypatch.setattr(bt, "_info", lambda: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert bt.resolve_sz_decimals() == 4


def test_backtest_refuses_a_model_that_can_recall_the_period(monkeypatch):
    """An LLM that has seen the past cannot honestly "predict" it.

    Replaying history through a model trained on that history measures recall,
    not edge, and the equity curve that comes out is fiction. The guard must
    fire before any data is fetched.
    """
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MODEL="openai",
               OPENAI_API_KEY="sk-test")
    try:
        Backtester(cfg)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "recall" in str(exc) or "honest" in str(exc)


def test_backtest_guard_is_overridable_only_deliberately(monkeypatch):
    """The escape hatch exists, but it is explicit and it is the caller's choice."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MODEL="openai",
               OPENAI_API_KEY="sk-test")
    bt = Backtester(cfg, allow_live_model=True)
    assert bt.model.name.startswith("openai:")


def test_backtest_momentum_model_needs_no_override(monkeypatch):
    """The default path is unaffected: the guard is specific to a recalling model."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MODEL="momentum")
    bt = Backtester(cfg)
    assert bt.model.name == "momentum"


def test_backtest_fee_rates_come_from_config(monkeypatch):
    """Costs are modelled, so a backtest and the paper loop must agree on them."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper",
               HL_MAKER_BPS=0.0, HL_TAKER_BPS=9.0, HL_SLIPPAGE_BPS=5.0)
    bt = Backtester(cfg)
    assert bt.broker.maker_rate == 0.0
    assert abs(bt.broker.taker_rate - 9.0 / 10_000) < 1e-15
    assert abs(bt.broker.slippage - 5.0 / 10_000) < 1e-15

