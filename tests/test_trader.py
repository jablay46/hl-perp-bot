"""Integration test for the trader loop using fakes for market and account.

Deterministic: real market data is not needed to prove that a decision flows
through risk sizing into an order, and that a crossing print fills a resting
maker order and moves P&L.
"""

from __future__ import annotations

import time

from hlperp.account import Account
from hlperp.config import load_config
from hlperp.execution import PaperBroker
from hlperp.market import MarketData
from hlperp.model import MomentumModel
from hlperp.trader import Trader
from hlperp.types import AssetCtx, Book, Level, TradePrint


class FakeMarket:
    sz_decimals = 2
    max_leverage = 10

    def __init__(self) -> None:
        self.book = Book(
            coin="BTC", ts=0,
            bids=[Level(100.0, 5.0), Level(99.5, 5.0)],
            asks=[Level(100.2, 5.0), Level(100.5, 5.0)],
        )
        self.ctx = AssetCtx("BTC", mark_px=100.1, mid_px=100.1, oracle_px=100.0,
                            funding_hourly=0.0001, open_interest=1.0,
                            day_ntl_vlm=1e6, prev_day_px=99.0, premium=0.001)
        self._trades: list[TradePrint] = []

    def recent_trades(self, n: int = 50):
        return self._trades[-n:]


def _cfg(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return load_config()


def test_tick_produces_decision_and_order(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    events = []
    trader = Trader(cfg, market, Account("testnet", None), PaperBroker("BTC"),
                    MomentumModel(), on_event=events.append)
    e = trader.tick()
    assert e is not None
    assert e.decision is not None
    assert e.order is not None
    # ALO must rest inside the touch, never crossing.
    assert e.order["status"] == "resting"
    assert e.order["px"] < market.book.best_ask
    assert len(events) == 1


def test_crossing_print_fills_resting_maker_and_updates_pnl(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel())
    trader.tick()
    assert len(broker.open_orders("BTC")) == 1
    resting = broker.open_orders("BTC")[0]
    # A seller hits our resting bid.
    trader.on_print("BTC", resting.px - 0.01, int(time.time() * 1000), is_buy=False)
    assert trader.totals["fills"] == 1
    assert trader.totals["fees"] > 0
    assert broker.position("BTC").size > 0


def test_risk_blocks_order_when_liquidation_too_close(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_LEVERAGE=10,
               HL_MAX_LEVERAGE=10, HL_MIN_LIQ_DISTANCE_PCT=15, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel())
    trader.tick()
    assert len(broker.open_orders("BTC")) == 0
    assert trader.totals["rejected"] >= 1


def test_kill_switch_halts_and_stops_ordering(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MAX_DRAWDOWN_PCT=1,
               HL_MIN_LIQ_DISTANCE_PCT=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    acct = Account("testnet", None)
    trader = Trader(cfg, market, acct, broker, MomentumModel())
    trader.tick()
    # Equity collapses (paper equity is derived from the broker's base).
    trader.base_equity = 100.0
    e = trader.tick()
    assert e.halted is True
    assert "drawdown" in e.halt_reason
    # Resting orders are pulled and no new one is placed once halted.
    assert len(broker.open_orders("BTC")) == 0


def test_funding_accrues_on_open_position(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel())
    trader.tick()
    resting = broker.open_orders("BTC")[0]
    trader.on_print("BTC", resting.px - 0.01, int(time.time() * 1000), is_buy=False)
    assert broker.position("BTC").size > 0
    trader._last_funding_ts -= 3_600_000  # simulate an hour passing
    trader.tick()
    assert trader.totals["funding"] > 0
