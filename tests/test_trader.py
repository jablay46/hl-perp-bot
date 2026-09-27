from __future__ import annotations

import time

from hlperp.account import Account
from hlperp.config import load_config
from hlperp.execution import PaperBroker
from hlperp.model import MomentumModel
from hlperp.trader import Trader
from hlperp.types import AccountState, AssetCtx, Book, Level, Signal, TradePrint


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
    # ALO must rest at the near touch, never crossing.
    assert e.order["status"] == "resting"
    assert e.order["px"] < market.book.best_ask
    assert e.order["px"] >= market.book.best_bid
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
    # A seller hits our resting bid: a market sell print at our price.
    n_before = trader.totals["fills"]
    trader.on_print("BTC", resting.px, int(time.time() * 1000), is_buy=False)
    assert trader.totals["fills"] > n_before
    assert trader.totals["fees"] > 0
    assert broker.position("BTC").size > 0


def test_wrong_side_print_does_not_fill(monkeypatch):
    """A print that does not reach our resting bid must not fill it."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel())
    trader.tick()
    resting = broker.open_orders("BTC")[0]
    # A print above our resting bid never fills a bid.
    trader.on_print("BTC", resting.px + 1.0, int(time.time() * 1000), is_buy=True)
    assert trader.totals["fills"] == 0
    assert broker.position("BTC").size == 0


def test_risk_blocks_order_when_liquidation_too_close(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_LEVERAGE=10,
               HL_MAX_LEVERAGE=10, HL_MAINTENANCE_LEVERAGE=10,
               HL_MIN_LIQ_DISTANCE_PCT=15, HL_MAX_DRAWDOWN_PCT=99)
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


def test_totals_view_flags_unfunded_account(monkeypatch):
    """A zero-balance account must not report a bogus -100% PnL."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    trader = Trader(cfg, FakeMarket(), Account("testnet", None), PaperBroker("BTC"), MomentumModel())
    trader.totals["starting_equity"] = 0.0
    trader.totals["last_equity"] = 0.0
    view = trader._totals_view(AccountState(0.0, 0.0, 0.0, 0.0, None))
    assert view["funded"] is False
    assert view["pnl_pct"] == 0.0 and view["pnl"] == 0.0
    assert view["equity"] == 0.0


class FakeLiveAccount(Account):
    """Account stub that returns a controlled fill log for reconciliation."""

    def __init__(self, rows):
        self.rows = rows
        self.address = "0xabc"

    def fills(self, start_ms):
        return [r for r in self.rows if r["time"] >= start_ms]


def _live_trader(cfg, rows):
    return Trader(cfg, FakeMarket(), FakeLiveAccount(rows), PaperBroker("BTC"), MomentumModel())


def test_reconcile_fills_is_idempotent(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MIN_LIQ_DISTANCE_PCT=0,
               HL_MAX_DRAWDOWN_PCT=99)
    rows = [{"time": int(time.time() * 1000), "oid": 1, "hash": "0x1", "coin": "BTC",
             "side": "B", "px": 100.0, "sz": 0.5, "fee": 0.01, "closedPnl": 0.0,
             "crossed": True}]
    trader = _live_trader(cfg, rows)
    first = trader.reconcile_fills()
    assert len(first) == 1 and trader.totals["fills"] == 1
    # A second poll of the same rows must not double count.
    assert trader.reconcile_fills() == []
    assert trader.totals["fills"] == 1
    assert abs(trader.totals["fees"] - 0.01) < 1e-12


def test_reconcile_ignores_other_coins(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_MIN_LIQ_DISTANCE_PCT=0,
               HL_MAX_DRAWDOWN_PCT=99)
    rows = [{"time": int(time.time() * 1000), "oid": 1, "hash": "0x2", "coin": "ETH",
             "side": "B", "px": 10.0, "sz": 1.0, "fee": 0.0, "closedPnl": 0.0,
             "crossed": True}]
    trader = _live_trader(cfg, rows)
    assert trader.reconcile_fills() == []
    assert trader.totals["fills"] == 0


def test_arm_bracket_places_and_does_not_churn(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_TP_SL="true",
               HL_TP_PCT=0.02, HL_SL_PCT=0.01, HL_MIN_LIQ_DISTANCE_PCT=0,
               HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel())
    # Open a long at the mark, then arm a bracket around it.
    broker.place(Signal("BTC", "buy", "IOC", 100.1), 1.0, 100.1, 2)
    state = Account("testnet", None).state("BTC")
    trader.arm_bracket(state)
    legs = broker._brackets.get("BTC")
    assert legs and len(legs) == 2
    tp = next(l.px for l in legs if l.kind == "tp")
    sl = next(l.px for l in legs if l.kind == "sl")
    assert tp > 100.1 and sl < 100.1
    # Re-arming with an unchanged size must not churn the bracket.
    before = list(broker._brackets["BTC"])
    trader.arm_bracket(state)
    assert [l.px for l in broker._brackets["BTC"]] == [l.px for l in before]

