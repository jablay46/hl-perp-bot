"""Offline backtest: replay candles and funding through the live strategy.

The live loop reacts to a level-2 book and a trade tape. Neither exists in
history, so this runner reconstructs the minimum the model actually consumes —
mid returns, funding and (optionally) taker flow — from candles, and drives the
same :class:`~hlperp.risk.RiskEngine`, :class:`~hlperp.model.Model` and fee
model. A maker order placed on one candle is checked against the next candle's
range, which is a realistic one-bar latency for a resting quote.

This is deliberately conservative: it never assumes we get filled at a price
better than our limit, it charges funding on open notional at the historical
rate, and it charges taker fees on any crossing order.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass

from .config import Config
from .execution import PaperBroker
from .model import Model, create_model
from .risk import RiskEngine
from .rounding import MAX_DECIMALS_PERP
from .strategy import _returns_bps
from .types import Fill, MarketState, Position, Signal

log = logging.getLogger("hlperp.backtest")


@dataclass
class BacktestResult:
    candles: int
    decisions: int
    orders: int
    fills: int
    fees: float
    funding: float
    realized: float
    start_equity: float
    end_equity: float
    max_drawdown_pct: float
    trades: int
    win_rate: float

    def as_dict(self) -> dict:
        return {
            "candles": self.candles,
            "decisions": self.decisions,
            "orders": self.orders,
            "fills": self.fills,
            "fees": round(self.fees, 4),
            "funding": round(self.funding, 4),
            "realized": round(self.realized, 4),
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.end_equity, 2),
            "return_pct": round((self.end_equity / self.start_equity - 1) * 100, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
        }


@dataclass
class _FundingPoint:
    ts: int
    hourly: float


class Backtester:
    def __init__(self, cfg: Config, model: Model | None = None, base_equity: float = 10_000.0,
                 sz_decimals: int | None = None) -> None:
        self.cfg = cfg
        self.model = model or create_model(cfg)
        self.base_equity = base_equity
        self.sz_decimals = sz_decimals
        self.risk = RiskEngine(cfg)
        self.broker = PaperBroker(cfg.coin, participation=1.0)

    # -- data --------------------------------------------------------------
    def _info(self):
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        base = constants.TESTNET_API_URL if self.cfg.network == "testnet" else constants.MAINNET_API_URL
        return Info(base, skip_ws=True)

    def resolve_sz_decimals(self) -> int:
        """The asset's lot precision, so sizes are truncated the way the venue does.

        A hardcoded value is worse than none: an interval finer than the real lot
        size truncates every order to zero and the backtest reports no fills while
        looking like it ran. Offline (candles injected, no network) fall back to the
        perp ceiling of 6, which never over-truncates.
        """
        if self.sz_decimals is not None:
            return self.sz_decimals
        try:
            meta = self._info().meta()
        except Exception as exc:  # pragma: no cover - network
            # Do not cache the fallback: a transient failure must not lock this
            # object to a coarse lot for the rest of its life.
            log.warning(
                "could not fetch asset metadata (%s); falling back to %d decimals for "
                "sizing. Orders may be over-counted versus the real venue lot size.",
                exc, MAX_DECIMALS_PERP,
            )
            return MAX_DECIMALS_PERP
        # An unknown coin is a caller error, not a network problem: raising here
        # beats silently sizing every order at the wrong precision.
        for asset in meta["universe"]:
            if asset["name"] == self.cfg.coin:
                self.sz_decimals = int(asset["szDecimals"])
                return self.sz_decimals
        raise ValueError(
            f"{self.cfg.coin!r} is not in the exchange's perp universe; "
            f"cannot determine its lot size"
        )

    def fetch_candles(self, interval: str, lookback_ms: int) -> list[dict]:
        end_ms = int(_time.time() * 1000)
        return self._info().candles_snapshot(self.cfg.coin, interval, end_ms - lookback_ms, end_ms)

    def fetch_funding(self, start_ms: int, end_ms: int) -> list[_FundingPoint]:
        try:
            rows = self._info().funding_history(self.cfg.coin, start_ms, end_ms)
        except Exception as exc:  # pragma: no cover - network
            log.warning("funding history unavailable (%s); assuming zero funding", exc)
            return []
        return [_FundingPoint(int(r["time"]), float(r.get("fundingRate", 0.0))) for r in rows]

    # -- replay ------------------------------------------------------------
    def run(self, interval: str = "1m", lookback_ms: int = 6 * 3_600_000,
            candles: list[dict] | None = None,
            funding: list[_FundingPoint] | None = None) -> BacktestResult:
        rows = candles if candles is not None else self.fetch_candles(interval, lookback_ms)
        rows = sorted(rows, key=lambda r: int(r["t"]))
        if len(rows) < 3:
            raise ValueError(f"need at least 3 candles, got {len(rows)}")
        if funding is None:
            funding = self.fetch_funding(int(rows[0]["t"]), int(rows[-1]["t"]))
        funding = sorted(funding, key=lambda p: p.ts)

        mids: list[float] = []
        equity = self.base_equity
        sz_decimals = self.resolve_sz_decimals()
        self.risk.observe_equity(equity)
        peak = max_dd = 0.0
        fees = funding_paid = realized = 0.0
        fills = orders = decisions = entries = wins = losses = 0

        pending_px: float | None = None
        pending_side: str | None = None
        fi = 0
        last_ts = int(rows[0]["t"])

        for i, row in enumerate(rows):
            ts = int(row["t"])
            close, high, low = float(row["c"]), float(row["h"]), float(row["l"])
            mids.append(close)

            # 1) Resolve last candle's resting order against this candle's range.
            if pending_px is not None and pending_side is not None:
                touched = low <= pending_px if pending_side == "buy" else high >= pending_px
                if touched:
                    was_flat = self.broker.position(self.cfg.coin).size == 0
                    for f in self.broker.on_print(self.cfg.coin, pending_px, ts, pending_side == "buy"):
                        fills += 1
                        fees += f.fee
                        realized += f.closed_pnl
                        if was_flat:
                            entries += 1
                            was_flat = False
                        elif f.closed_pnl > 0:
                            wins += 1
                        elif f.closed_pnl < 0:
                            losses += 1
                pending_px = pending_side = None
            self.broker.cancel_all(self.cfg.coin)  # never carry an unfilled quote

            # 2) Accrue funding on the open position for this candle.
            while fi < len(funding) and funding[fi].ts <= ts:
                fi += 1
            pos_size = self.broker.position(self.cfg.coin).size
            if fi > 0 and pos_size != 0:
                held_h = min((ts - last_ts) / 3_600_000, 1.0)
                notional = self.broker.position_value(self.cfg.coin, close)
                cost = notional * funding[fi - 1].hourly * (1.0 if pos_size > 0 else -1.0) * held_h
                funding_paid += cost
                self.broker.charge_funding(cost)
            last_ts = ts

            # 3) Mark to market and enforce the kill switch.
            equity = self.broker.equity(self.base_equity, self.cfg.coin, close)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)
            self.risk.observe_equity(equity)
            if self.risk.check_kill_switch(equity):
                break
            if i < 2:
                continue

            # 4) Decide and size, then rest a maker quote for the next candle.
            decision = self.model.decide(self._state(close, mids, funding, fi))
            decisions += 1
            want = decision.action
            if want == "hold":
                continue
            size_dec = self.risk.size(
                want, equity, close,
                exchange_max_leverage=self.cfg.max_leverage,
                current_position=self._position_view(close),
                sz_decimals=sz_decimals,
            )
            if not size_dec.allowed:
                continue
            order_side = size_dec.side or want
            half = self.cfg.spread_bps / 10_000
            px = close * (1 - half) if order_side == "buy" else close * (1 + half)

            tif = "ALO" if self.cfg.order_tif == "ALO" else "IOC"
            if tif == "ALO" and ((order_side == "buy" and px >= close) or (order_side == "sell" and px <= close)):
                continue  # post-only would cross: rejected
            res = self.broker.place(Signal(self.cfg.coin, order_side, tif, px),
                                    size_dec.size, close, sz_decimals)
            orders += 1
            if tif == "ALO" and res.status == "resting":
                pending_px, pending_side = px, order_side
            elif res.status == "filled" and res.raw.get("fill"):
                f = Fill(**res.raw["fill"])
                fills += 1
                fees += f.fee
                realized += f.closed_pnl

        end_equity = self.broker.equity(self.base_equity, self.cfg.coin, float(rows[-1]["c"]))
        closed = wins + losses
        return BacktestResult(
            candles=len(rows), decisions=decisions, orders=orders, fills=fills,
            fees=fees, funding=funding_paid, realized=realized,
            start_equity=self.base_equity, end_equity=end_equity,
            max_drawdown_pct=max_dd, trades=entries,
            win_rate=(wins / closed if closed else 0.0),
        )

    def _state(self, close: float, mids: list[float], funding: list[_FundingPoint], fi: int) -> MarketState:
        hourly = funding[fi - 1].hourly if fi > 0 else 0.0
        pos = self.broker.position(self.cfg.coin)
        return MarketState(
            coin=self.cfg.coin, ts=0, mid=close, mark_px=close, oracle_px=close,
            spread_bps=self.cfg.spread_bps, funding_hourly=hourly,
            funding_apr=hourly * 24 * 365, open_interest=0.0, day_ntl_vlm=0.0,
            book_imbalance=0.0, depth_usd={},
            returns_bps=_returns_bps(mids, {"last1": 1, "last5": 5, "last20": 20, "last100": 100}),
            recent_mids=" ".join(f"{m:.2f}" for m in mids[-60:]),
            trades={"count": 0, "buy_sz": 0.0, "sell_sz": 0.0, "cvd": 0.0, "cvd_ratio": 0.0,
                    "vwap": close, "last_px": close, "last_side": "buy"},
            recent_trades=[],
            position_side="long" if pos.size > 0 else ("short" if pos.size < 0 else "flat"),
            position_size=abs(pos.size), unrealized_pnl=self.broker.unrealized(self.cfg.coin, close),
            account_value=self.broker.equity(self.base_equity, self.cfg.coin, close),
            allowed={"buy": True, "sell": True},
        )

    def _position_view(self, mark_px: float) -> Position | None:
        pos = self.broker.position(self.cfg.coin)
        if pos.size == 0:
            return None
        return Position(
            coin=self.cfg.coin, side="long" if pos.size > 0 else "short",
            size=abs(pos.size), entry_px=pos.entry_px,
            position_value=abs(pos.size) * mark_px,
            unrealized_pnl=self.broker.unrealized(self.cfg.coin, mark_px),
            margin_used=abs(pos.size) * mark_px / max(1, self.cfg.leverage),
            liquidation_px=None, leverage=float(self.cfg.leverage), leverage_type="backtest",
        )
