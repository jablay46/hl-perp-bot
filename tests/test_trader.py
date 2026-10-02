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
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    events = []
    trader = Trader(cfg, market, Account("testnet", None), PaperBroker("BTC"),
                    MomentumModel(min_signal=0), on_event=events.append)
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
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    assert len(broker.open_orders("BTC")) == 1
    resting = broker.open_orders("BTC")[0]
    # PaperBroker matches on price, not on the aggressor: a print at the quote's own
    # price reaches it whichever side the model chose to rest.
    n_before = trader.totals["fills"]
    trader.on_print("BTC", resting.px, int(time.time() * 1000), is_buy=resting.side == "buy")
    assert trader.totals["fills"] > n_before
    assert trader.totals["fees"] > 0
    assert broker.position("BTC").size != 0


def test_wrong_side_print_does_not_fill(monkeypatch):
    """A print that does not reach our resting quote must not fill it.

    Matching is price-based, so the unreachable price depends on the quote's side:
    a print above a resting bid, or below a resting ask, is the wrong side.
    """
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    resting = broker.open_orders("BTC")[0]
    away = resting.px + 1.0 if resting.side == "buy" else resting.px - 1.0
    trader.on_print("BTC", away, int(time.time() * 1000), is_buy=False)
    assert trader.totals["fills"] == 0
    assert broker.position("BTC").size == 0


def test_risk_blocks_order_when_liquidation_too_close(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_LEVERAGE=10,
               HL_MAX_LEVERAGE=10, HL_MAINTENANCE_LEVERAGE=10,
               HL_MIN_LIQ_DISTANCE_PCT=15, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
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
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    resting = broker.open_orders("BTC")[0]
    trader.on_print("BTC", resting.px, int(time.time() * 1000), is_buy=resting.side == "buy")
    assert broker.position("BTC").size != 0
    trader._last_funding_ts -= 3_600_000  # simulate an hour passing
    trader.tick()
    # Longs pay positive funding, shorts receive it, so the sign follows the side.
    pos = broker.position("BTC")
    assert trader.totals["funding"] * (1.0 if pos.size > 0 else -1.0) > 0


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


class _FakeAccountWithFills:
    """Returns a fill log that mixes coins, the way a real multi-asset account does."""

    def __init__(self, rows):
        self._rows = rows

    def fills(self, start_ms):
        return self._rows


def test_reconcile_fills_ignores_other_coins(monkeypatch):
    """A fill for a different coin must not leak into this bot's totals."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    rows = [
        {"coin": "ETH", "side": "B", "px": 3000.0, "sz": 1.0, "fee": 1.5, "closedPnl": 99.0,
         "time": int(time.time() * 1000), "oid": 1, "hash": "0xeth", "crossed": True},
        {"coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0, "fee": 0.05, "closedPnl": 0.0,
         "time": int(time.time() * 1000), "oid": 2, "hash": "0xbtc", "crossed": False},
    ]
    trader = Trader(cfg, market, _FakeAccountWithFills(rows), PaperBroker("BTC"), MomentumModel())
    out = trader.reconcile_fills()
    assert [f.coin for f in out] == ["BTC"]
    assert trader.totals["fills"] == 1
    assert abs(trader.totals["fees"] - 0.05) < 1e-12


def test_reconcile_fills_keeps_partial_fills_distinct_by_tid(monkeypatch):
    """Two partial fills in one millisecond from one order must both count.

    (time, oid, hash) alone would collapse them; ``tid`` is present on userFills.
    """
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    ts = int(time.time() * 1000)
    common = {"coin": "BTC", "side": "B", "px": 100.0, "sz": 0.5, "closedPnl": 0.0,
              "time": ts, "oid": 7, "hash": "0xabc", "crossed": False}
    rows = [{**common, "tid": 111, "fee": 0.01}, {**common, "tid": 222, "fee": 0.02}]
    trader = Trader(cfg, market, _FakeAccountWithFills(rows), PaperBroker("BTC"), MomentumModel())
    out = trader.reconcile_fills()
    assert len(out) == 2          # same (time, oid, hash) but different tid
    assert trader.totals["fills"] == 2
    assert abs(trader.totals["fees"] - 0.03) < 1e-12

    # A repeated poll of the same rows must not double count.
    assert trader.reconcile_fills() == []


def test_reconcile_fills_without_tid_still_dedups(monkeypatch):
    """When tid is absent the key degrades to (time, oid, hash)."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99)
    market = FakeMarket()
    ts = int(time.time() * 1000)
    row = {"coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0, "fee": 0.05,
           "closedPnl": 0.0, "time": ts, "oid": 9, "hash": "0xdef", "crossed": False}
    trader = Trader(cfg, market, _FakeAccountWithFills([row]), PaperBroker("BTC"), MomentumModel())
    assert len(trader.reconcile_fills()) == 1
    assert trader.reconcile_fills() == []  # identical row is deduped


def test_signal_for_raises_instead_of_asserting_on_missing_mid(monkeypatch):
    """An assert would vanish under -O and build an order at nan; must raise."""
    from hlperp.strategy import Strategy
    from hlperp.types import Book

    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper")
    market = FakeMarket()
    strat = Strategy(cfg, market, MomentumModel())
    empty = Book(coin="BTC", ts=0, bids=[], asks=[])
    try:
        strat.signal_for("buy", empty, 2)
    except ValueError as exc:
        assert "mid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")



# -- hold, and the post-only entry / taker exit split -------------------------

class _AlwaysHold:
    """A model that always holds, so the no-order path can be tested directly."""

    name = "hold"

    def decide(self, state):
        from hlperp.types import Decision

        return Decision(action="hold", probabilities={"buy": 0.5, "sell": 0.5, "hold": 1.0},
                        up=0.5, latency_ms=0.0, input_tokens=0, reason="test hold")


def test_hold_sends_no_order_and_pulls_a_resting_quote(monkeypatch):
    """A hold is a real answer: it must not order, and must cancel what it no longer wants."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    assert len(broker.open_orders("BTC")) == 1  # a quote is resting

    trader.strategy.model = _AlwaysHold()
    e = trader.tick()
    assert e.decision["action"] == "hold"
    assert e.order is None
    assert trader.totals["orders"] == 1  # unchanged by the hold
    assert trader.totals["holds"] == 1
    assert len(broker.open_orders("BTC")) == 0  # the standing quote was pulled
    assert e.decision.get("cancelled") is True


def test_hold_with_nothing_resting_is_not_a_cancellation(monkeypatch):
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99, HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, _AlwaysHold())
    e = trader.tick()
    assert e.order is None
    assert trader.totals["holds"] == 1
    assert "cancelled" not in e.decision


def test_exit_is_reduce_only_and_crosses_as_taker(monkeypatch):
    """An entry rests post-only; the exit that closes it must be reduce-only and IOC."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    entry = broker.open_orders("BTC")[0]
    trader.on_print("BTC", entry.px, int(time.time() * 1000), is_buy=entry.side == "buy")
    pos = broker.position("BTC")
    assert pos.size != 0
    entry_side = "buy" if pos.size > 0 else "sell"

    class _Against:
        name = "against"

        def decide(self, state):
            from hlperp.types import Decision

            against = "sell" if entry_side == "buy" else "buy"
            return Decision(action=against, probabilities={"buy": 0.1, "sell": 0.9, "hold": 0.0},
                            up=0.1, latency_ms=0.0, input_tokens=0, reason="flip")

    trader.strategy.model = _Against()
    trader.tick()
    assert trader.totals["orders"] == 2
    assert len(broker.open_orders("BTC")) == 0  # crossed as a taker, nothing rests
    # The exit removed the position down to at most one lot of dust. A live venue
    # always reports a lot-aligned position, so the remainder is a paper artifact of
    # the broker's fractional partial fills, and it sits below the $10 venue minimum
    # so it cannot be closed by any order.
    assert abs(broker.position("BTC").size) < 0.01


def test_exit_never_sizes_above_the_open_position(monkeypatch):
    """A reduce-only exit is clamped to the position so the venue cannot reject it."""
    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO",
               HL_SPREAD_BPS=2, HL_MIN_LIQ_DISTANCE_PCT=0, HL_MAX_DRAWDOWN_PCT=99,
               HL_HOLD_SIGNAL=0)
    market = FakeMarket()
    broker = PaperBroker("BTC")
    trader = Trader(cfg, market, Account("testnet", None), broker, MomentumModel(min_signal=0))
    trader.tick()
    entry = broker.open_orders("BTC")[0]
    trader.on_print("BTC", entry.px, int(time.time() * 1000), is_buy=entry.side == "buy")
    pos = broker.position("BTC")
    action = "sell" if pos.size > 0 else "buy"
    sig = trader.strategy.signal_for(action, market.book, 2, reduce_only=True, taker=True)
    pos_view, _ = trader._position_view(AccountState(0.0, 0.0, 0.0, 0.0, None))
    dec = trader._size_for(action, True, pos_view, sig, None)
    assert dec.allowed
    assert dec.size <= abs(pos.size) + 1e-12


def test_signal_for_taker_override_ignores_the_alo_default(monkeypatch):
    """With HL_ORDER_TIF=ALO an explicit taker still crosses, because ALO cannot fill."""
    from hlperp.strategy import Strategy

    cfg = _cfg(monkeypatch, HL_COIN="BTC", HL_MODE="paper", HL_ORDER_TIF="ALO")
    market = FakeMarket()
    strat = Strategy(cfg, market, MomentumModel())
    resting = strat.signal_for("buy", market.book, 2)
    crossing = strat.signal_for("buy", market.book, 2, reduce_only=True, taker=True)
    assert resting.tif == "ALO" and crossing.tif == "IOC"
    assert resting.limit_px < market.book.best_ask  # never crosses
    assert crossing.limit_px >= market.book.best_ask  # crosses the touch
    assert crossing.reduce_only is True and resting.reduce_only is False

