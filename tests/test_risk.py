from hlperp.config import load_config
from hlperp.risk import (
    MIN_ORDER_NOTIONAL,
    RiskEngine,
    liquidation_price,
    min_equity_for_order,
    min_order_notional,
)
from hlperp.rounding import floor_sz
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


def test_sizing_refuses_order_below_venue_minimum(monkeypatch):
    """A $7 account sizes to $0.35: the venue rejects it, so we must refuse first."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    d = r.size("buy", equity=7.0, px=83_000.0, exchange_max_leverage=10, sz_decimals=5)
    assert d.allowed is False
    assert "minimum" in d.reason or "rounds to 0" in d.reason
    # The 5-decimal lot means a whole lot is not $10 but ~$10.79.
    assert abs(min_order_notional(83_000.0, 5) - 0.00013 * 83_000.0) < 1e-6


def test_sizing_allows_order_once_equity_clears_minimum(monkeypatch):
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    px = 83_000.0
    need = min_equity_for_order(0.01, 5, px, 5)
    below = r.size("buy", equity=need - 20, px=px, exchange_max_leverage=10, sz_decimals=5)
    at = r.size("buy", equity=need + 1, px=px, exchange_max_leverage=10, sz_decimals=5)
    assert below.allowed is False
    assert at.allowed is True
    # The size must survive lot truncation at $10 or above.
    assert floor_sz(at.size, 5) * px >= MIN_ORDER_NOTIONAL


def test_sizing_delta_is_lot_truncated_not_rounded_up(monkeypatch):
    """The returned size must never exceed the notional the risk budget allowed."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    px = 1_234.56
    d = r.size("buy", equity=10_000.0, px=px, exchange_max_leverage=10, sz_decimals=3)
    assert d.allowed
    assert floor_sz(d.size, 3) * px <= 10_000 * 0.01 * 5 + 1e-6


def test_min_notional_uses_decimal_not_float():
    """A px/lot whose float quotient lands exactly on an integer must not be short.

    ``10.0 / (0.6666666666666666 * 1)`` is ``15.000000000000002`` in floats, so the
    old ``math.ceil`` produced 16 lots; but the same expression can also land on or
    below an integer and understate the floor. Decimal ceiling is exact.
    """
    assert min_order_notional(0.6666666666666666, 0) > 10.0
    # Exactly $10 at a whole $1 lot is reachable: 10 lots of $1.
    assert abs(min_order_notional(1.0, 0) - 10.0) < 1e-12
    # 10 lots of $1 is only $10 at exactly $1; at $0.999 it takes 11 lots.
    assert abs(min_order_notional(0.999, 0) - 11 * 0.999) < 1e-12


def test_decision_notional_matches_truncated_size(monkeypatch):
    """The decision's notional must describe the size actually sent, post-truncation."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.01, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=50, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    px = 1_234.567
    d = r.size("buy", equity=10_000.0, px=px, exchange_max_leverage=10, sz_decimals=3)
    assert d.allowed
    assert abs(d.notional - d.size * px) < 1e-9
    # And the untruncated delta was strictly larger, so this is a real tightening.
    assert d.notional <= 10_000 * 0.01 * 5 + 1e-6


def test_min_equity_honours_max_notional_cap(monkeypatch):
    """When the cap binds, the equity needed is higher than risk_pct*leverage implies."""
    px, d = 83_000.0, 5
    floor_ntl = min_order_notional(px, d)

    # Cap generous: uncapped figure is right.
    uncapped = min_equity_for_order(0.01, 5, px, d, max_notional_pct=100.0)
    assert abs(uncapped - floor_ntl / 0.05) < 1e-6

    # Cap binds at 1%/equity: per-equity fraction is 0.01, so twice the equity.
    capped = min_equity_for_order(0.01, 5, px, d, max_notional_pct=1.0)
    assert abs(capped - floor_ntl / 0.01) < 1e-6
    assert capped > uncapped


def test_sizing_and_min_equity_agree_when_cap_binds(monkeypatch):
    """The engine must allow exactly when equity clears the helper's threshold."""
    cfg = _cfg(monkeypatch, HL_RISK_PCT=0.10, HL_LEVERAGE=5, HL_MAX_LEVERAGE=10,
               HL_MAX_NOTIONAL_PCT=1.0, HL_MAX_DRAWDOWN_PCT=99, HL_MIN_LIQ_DISTANCE_PCT=0)
    r = RiskEngine(cfg)
    px, d = 83_000.0, 5
    need = min_equity_for_order(cfg.risk_pct, cfg.leverage, px, d, cfg.max_notional_pct)
    below = r.size("buy", equity=need - 1.0, px=px, exchange_max_leverage=10, sz_decimals=d)
    at = r.size("buy", equity=need + 1.0, px=px, exchange_max_leverage=10, sz_decimals=d)
    assert below.allowed is False and "minimum" in below.reason
    assert at.allowed is True

    # With the old uncapped formula the cap case looks large enough but is refused.
    naive = min_order_notional(px, d) / (cfg.risk_pct * cfg.leverage)
    refused = r.size("buy", equity=naive * 1.5, px=px, exchange_max_leverage=10, sz_decimals=d)
    assert refused.allowed is False
