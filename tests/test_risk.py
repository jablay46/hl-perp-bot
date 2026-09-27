from hlperp.config import load_config
from hlperp.risk import RiskEngine, liquidation_price
from hlperp.types import Position


def _cfg(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return load_config()


def test_liquidation_price_formula_long():
    # long, 10x maintenance -> l = 0.1, denom = 1 - 0.1 = 0.9
    liq = liquidation_price(entry_px=100.0, size=1.0, side="long",
                            margin_available=50.0, maintenance_leverage=10)
    assert abs(liq - (100.0 - 50.0 / 0.9)) < 1e-9
    assert liq < 100.0


def test_liquidation_price_formula_short():
    liq = liquidation_price(entry_px=100.0, size=1.0, side="short",
                            margin_available=50.0, maintenance_leverage=10)
    assert liq > 100.0


def test_kill_switch_trips_on_drawdown(monkeypatch):
    monkeypatch.delenv("HL_ALLOW_LIVE", raising=False)
    cfg = _cfg(monkeypatch, HL_MAX_DRAWDOWN_PCT=10)
    r = RiskEngine(cfg)
    r.observe_equity(1000)
    assert r.check_kill_switch(1000) is False
    assert r.check_kill_switch(880) is True
    assert "drawdown" in r.halt_reason
    r.resume()
    assert r.halted is False


def test_sizing_respects_position_cap(monkeypatch):
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    d = r.size("buy", equity=10_000, px=100.0, exchange_max_leverage=10)
    # risk 1% of equity * 5x leverage = 500 notional, well under the 50% cap.
    assert d.allowed and abs(d.notional - 500) < 1e-6
    assert d.side == "buy"
    assert abs(d.size - 5.0) < 1e-9


def test_sizing_is_delta_against_existing_position(monkeypatch):
    """A position already at target must not be stacked by a repeat tick."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    pos = Position(coin="BTC", side="long", size=5.0, entry_px=100.0, position_value=500.0,
                   unrealized_pnl=0.0, margin_used=100.0, liquidation_px=80.0,
                   leverage=5.0, leverage_type="cross")
    d = r.size("buy", equity=10_000, px=100.0, exchange_max_leverage=10, current_position=pos)
    assert d.allowed is False
    assert "target" in d.reason


def test_sizing_reverses_through_existing_position(monkeypatch):
    """Flipping short from a long must size the full distance, not just the target."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    pos = Position(coin="BTC", side="long", size=5.0, entry_px=100.0, position_value=500.0,
                   unrealized_pnl=0.0, margin_used=100.0, liquidation_px=80.0,
                   leverage=5.0, leverage_type="cross")
    d = r.size("sell", equity=10_000, px=100.0, exchange_max_leverage=10, current_position=pos)
    assert d.allowed and d.side == "sell"
    # target short notional 500 plus closing the 500 long = 1000 -> 10 units.
    assert abs(d.size - 10.0) < 1e-9


def test_sizing_refuses_when_liquidation_too_close(monkeypatch):
    cfg = _cfg(monkeypatch, HL_LEVERAGE=10, HL_MAX_LEVERAGE=10,
               HL_MAINTENANCE_LEVERAGE=10, HL_MIN_LIQ_DISTANCE_PCT=15,
               HL_MAX_DRAWDOWN_PCT=99, HL_RISK_PCT=1.0, HL_MAX_NOTIONAL_PCT=100)
    r = RiskEngine(cfg)
    # At 10x with 10x maintenance, liquidation sits ~5.6% away -> refused by 15%.
    d = r.size("buy", equity=10_000, px=100.0, exchange_max_leverage=10)
    assert d.allowed is False
    assert "liquidation" in d.reason


def test_required_distance_matches_formula(monkeypatch):
    cfg = _cfg(monkeypatch, HL_MAINTENANCE_LEVERAGE=10, HL_MAX_LEVERAGE=40)
    r = RiskEngine(cfg)
    # (1/10) / (1 - 1/10) * 100 = 11.11%
    assert abs(r.required_distance_pct(10) - (0.1 / 0.9 * 100)) < 1e-9
    # 1x has no meaningful liquidation distance.
    assert r.required_distance_pct(1) > 100


def test_sizing_blocked_when_halted(monkeypatch):
    cfg = _cfg(monkeypatch, HL_MAX_DRAWDOWN_PCT=1, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    r.observe_equity(1000)
    r.check_kill_switch(900)
    d = r.size("buy", equity=900, px=100.0, exchange_max_leverage=10)
    assert d.allowed is False and "halted" in d.reason
