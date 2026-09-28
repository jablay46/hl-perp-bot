"""Strategy: turn live market data into the typed state the model sees, then
into a :class:`Signal` the execution adapter can act on.

The quote logic mirrors ``jev-trader``'s post-only idea but adapted to a perp
book: a maker order is placed ``HL_SPREAD_BPS`` inside the touch, never
crossing. When the model is asked for a taker move, an IOC order crosses with
the venue's slippage guard.
"""

from __future__ import annotations

import time
from typing import Optional

from .config import Config
from .market import MarketData
from .model import Model
from .rounding import round_px
from .types import AccountState, MarketState, Signal


def _returns_bps(mids: list[float], lookbacks: dict[str, int]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not mids:
        return {k: 0.0 for k in lookbacks}
    last = mids[-1]
    for name, n in lookbacks.items():
        if len(mids) > n:
            base = mids[-1 - n]
            out[name] = (last - base) / base * 10_000 if base else 0.0
        else:
            out[name] = 0.0
    return out


def _depth_usd(book, bands: tuple[int, ...] = (10, 25, 50)) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    if not book.mid:
        return out
    for bps in bands:
        lo = book.mid * (1 - bps / 10_000)
        hi = book.mid * (1 + bps / 10_000)
        bid = sum(l.sz * l.px for l in book.bids if l.px >= lo)
        ask = sum(l.sz * l.px for l in book.asks if l.px <= hi)
        out[f"{bps}bps"] = {"bid": bid, "ask": ask}
    return out


def _imbalance(book, bps: int = 10) -> float:
    if not book.mid:
        return 0.0
    lo = book.mid * (1 - bps / 10_000)
    hi = book.mid * (1 + bps / 10_000)
    bid = sum(l.sz for l in book.bids if l.px >= lo)
    ask = sum(l.sz for l in book.asks if l.px <= hi)
    if bid + ask == 0:
        return 0.0
    return (bid - ask) / (bid + ask)


class Strategy:
    def __init__(self, cfg: Config, market: MarketData, model: Model) -> None:
        self.cfg = cfg
        self.market = market
        self.model = model
        self.mids: list[float] = []

    def build_state(
        self,
        account: AccountState,
        allowed: dict[str, bool],
    ) -> Optional[MarketState]:
        book = self.market.book
        ctx = self.market.ctx
        if book is None or ctx is None or book.mid is None:
            return None
        self.mids.append(book.mid)
        if len(self.mids) > 400:
            self.mids.pop(0)

        trades = self.market.recent_trades(200)
        buy = sum(t.sz for t in trades if t.side == "buy")
        sell = sum(t.sz for t in trades if t.side == "sell")
        cvd = buy - sell
        total = buy + sell
        window = int(self.cfg.horizon * 1000)
        now = int(time.time() * 1000)
        recent = [t for t in trades if now - t.ts <= window]
        vwap = (
            sum(t.px * t.sz for t in recent) / sum(t.sz for t in recent)
            if recent and sum(t.sz for t in recent) > 0
            else None
        )
        last = recent[-1] if recent else None

        pos = account.position
        return MarketState(
            coin=self.cfg.coin,
            ts=now,
            mid=book.mid,
            mark_px=ctx.mark_px,
            oracle_px=ctx.oracle_px,
            spread_bps=book.spread_bps or 0.0,
            funding_hourly=ctx.funding_hourly,
            funding_apr=ctx.funding_apr,
            open_interest=ctx.open_interest,
            day_ntl_vlm=ctx.day_ntl_vlm,
            book_imbalance=_imbalance(book),
            depth_usd=_depth_usd(book),
            returns_bps=_returns_bps(self.mids, {"last1": 1, "last5": 5, "last20": 20, "last100": 100}),
            recent_mids=" ".join(f"{m:.2f}" for m in self.mids[-60:]),
            trades={
                "count": len(recent),
                "buy_sz": buy,
                "sell_sz": sell,
                "cvd": cvd,
                "cvd_ratio": (cvd / total) if total else 0.0,
                "vwap": vwap,
                "last_px": last.px if last else None,
                "last_side": last.side if last else None,
            },
            recent_trades=[f"{t.ts} {t.side} {t.sz}@{t.px}" for t in trades[-12:]],
            position_side=pos.side if pos else "flat",
            position_size=pos.size if pos else 0.0,
            unrealized_pnl=pos.unrealized_pnl if pos else 0.0,
            account_value=account.account_value,
            allowed=allowed,
        )

    def signal_for(self, decision_action: str, book, sz_decimals: int) -> Signal:
        """Build the order intent for the model's side."""
        side = "buy" if decision_action == "buy" else "sell"
        mid = book.mid
        if mid is None:
            # An assert here would vanish under -O and then silently build an order
            # at nan. Raise instead so the caller sees a real failure.
            raise ValueError("signal_for called with a book that has no mid price")
        half = self.cfg.spread_bps / 10_000
        if self.cfg.order_tif == "ALO":
            # Join the near touch on our own side: the best bid for a buy, the
            # best ask for a sell. It is maker (post-only) and, unlike quoting at
            # the mid, it still rests when the spread is a single tick; best levels
            # are already tick-valid so rounding cannot push us across.
            # `spread_bps` only widens the quote when there is room to.
            half = self.cfg.spread_bps / 10_000
            if side == "buy":
                px = mid - mid * half
                if book.best_bid is not None:
                    px = max(px, book.best_bid)  # never behind the bid
                if book.best_ask is not None and px >= book.best_ask:
                    px = book.best_bid if book.best_bid is not None else px
            else:
                px = mid + mid * half
                if book.best_ask is not None:
                    px = min(px, book.best_ask)  # never through the ask
                if book.best_bid is not None and px <= book.best_bid:
                    px = book.best_ask if book.best_ask is not None else px
            px = round_px(px, sz_decimals)
            tif = "ALO"
        else:
            # Taker: cross the touch.
            px = book.best_ask if side == "buy" else book.best_bid
            px = round_px(px or mid, sz_decimals)
            tif = self.cfg.order_tif
        return Signal(coin=self.cfg.coin, side=side, tif=tif, limit_px=px,
                      reason=f"model={decision_action}")
