"""The trading loop.

One decision per interval, driven by the market's own tick. Everything the model
can see is in :class:`~hlperp.types.MarketState`; everything it cannot do is
enforced by the :class:`~hlperp.risk.RiskEngine` between the decision and the
order.

P&L is honest: fees are charged on every fill and funding accrues on open
notional. That funding term is the part ``jev-trader`` has no concept of and the
part that decides whether a perp strategy survives.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .account import Account
from .config import Config
from .execution import Broker, PaperBroker
from .market import MarketData
from .model import Model
from .risk import RiskEngine
from .strategy import Strategy
from .types import AccountState, Fill, MarketState, OrderResult

log = logging.getLogger("hlperp.trader")


@dataclass
class TEvent:
    """One row of the dashboard feed."""

    ts: int
    coin: str
    mid: float
    mark_px: float
    spread_bps: float
    funding_apr: float
    decision: Optional[dict] = None
    order: Optional[dict] = None
    fills: list = field(default_factory=list)
    position: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""


class Trader:
    def __init__(
        self,
        cfg: Config,
        market: MarketData,
        account: Account,
        broker: Broker,
        model: Model,
        on_event: Optional[Callable[[TEvent], None]] = None,
        interval_s: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.market = market
        self.account = account
        self.broker = broker
        self.risk = RiskEngine(cfg)
        self.strategy = Strategy(cfg, market, model)
        self.on_event = on_event
        self.interval_s = interval_s

        self.history: list[TEvent] = []
        self.totals = {
            "ticks": 0, "decisions": 0, "orders": 0, "rejected": 0,
            "fills": 0, "fees": 0.0, "funding": 0.0, "realized": 0.0,
            "starting_equity": 0.0, "last_equity": 0.0,
        }
        self._pending: dict[int, OrderResult] = {}
        self._last_funding_ts = int(time.time() * 1000)
        self._stop = False
        self.base_equity = 10_000.0

    # -- market callbacks --------------------------------------------------
    def on_print(self, coin: str, px: float, ts: int, is_buy: bool) -> None:
        """Paper mode: a real trade print may fill our simulated resting order."""
        fills = self.broker.on_print(coin, px, ts, is_buy)
        for f in fills:
            self._apply_fill(f)

    def _apply_fill(self, f: Fill) -> None:
        self.totals["fills"] += 1
        self.totals["fees"] += f.fee
        self.totals["realized"] += f.closed_pnl
        log.info(
            "FILL %s %s %.6f@%.2f fee=%.4f closed=%.4f%s",
            f.coin, f.side, f.sz, f.px, f.fee, f.closed_pnl, "" if f.crossed else " (maker)",
        )

    # -- funding -----------------------------------------------------------
    def _accrue_funding(self, state: AccountState, ctx) -> None:
        """Charge funding on open notional at the hourly rate, pro rata."""
        now = int(time.time() * 1000)
        elapsed_h = (now - self._last_funding_ts) / 3_600_000
        self._last_funding_ts = now
        pos, value = self._position_view(state)
        if not pos or value <= 0 or elapsed_h <= 0:
            return
        # Longs pay when funding is positive; shorts receive.
        sign = 1.0 if pos.side == "long" else -1.0
        cost = value * ctx.funding_hourly * sign * elapsed_h
        self.totals["funding"] += cost
        charge = getattr(self.broker, "charge_funding", None)
        if charge:
            charge(cost)

    def _position_view(self, state: AccountState):
        """The position to reason about: live account, or the paper book."""
        if isinstance(self.broker, PaperBroker):
            p = self.broker.position(self.cfg.coin)
            if p.size == 0:
                return None, 0.0
            side = "long" if p.size > 0 else "short"
            mark = self.market.ctx.mark_px if self.market.ctx else p.entry_px
            dummy = type("P", (), {"side": side, "size": abs(p.size), "entry_px": p.entry_px,
                                   "position_value": abs(p.size) * mark, "unrealized_pnl": 0.0,
                                   "margin_used": 0.0, "liquidation_px": None, "leverage": self.cfg.leverage,
                                   "leverage_type": "paper"})()
            return dummy, abs(p.size) * mark
        if state.position is None:
            return None, 0.0
        return state.position, state.position.position_value

    # -- main tick ---------------------------------------------------------
    def tick(self) -> Optional[TEvent]:
        self.totals["ticks"] += 1
        book = self.market.book
        ctx = self.market.ctx
        if book is None or ctx is None or book.mid is None:
            return None

        state = self.account.state(self.cfg.coin)
        pos_view, pos_value = self._position_view(state)
        equity = self._equity(state, ctx.mark_px)
        if self.totals["starting_equity"] == 0.0:
            self.totals["starting_equity"] = equity
        self.totals["last_equity"] = equity
        self.risk.observe_equity(equity)
        self._accrue_funding(state, ctx)
        # Recompute after funding so the kill switch sees the true equity.
        equity = self._equity(state, ctx.mark_px)
        self.totals["last_equity"] = equity

        halted = self.risk.check_kill_switch(equity)
        if halted:
            # Safety: pull every resting order the moment we halt.
            try:
                self.broker.cancel_all(self.cfg.coin)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("cancel_all on halt failed: %s", exc)

        # Position caps pick the reducing side only.
        already_long = bool(pos_view and pos_view.side == "long")
        already_short = bool(pos_view and pos_view.side == "short")
        cap = equity * self.cfg.max_notional_pct / 100.0
        at_cap = pos_value >= cap
        allowed = {"buy": not halted, "sell": not halted}
        if at_cap and already_long:
            allowed["buy"] = False
        if at_cap and already_short:
            allowed["sell"] = False

        mstate = self.strategy.build_state(state, allowed)
        decision_dict = None
        order_dict = None

        if not halted and mstate is not None:
            decision = self.market_model_decide(mstate)
            self.totals["decisions"] += 1
            action = decision.action
            if not allowed.get(action, True):
                action = "sell" if action == "buy" else "buy"
            decision_dict = {
                "action": decision.action,
                "executed_action": action,
                "probabilities": decision.probabilities,
                "latency_ms": decision.latency_ms,
                "reason": decision.reason,
                "capped": action != decision.action,
            }
            sig = self.strategy.signal_for(action, book, self.market.sz_decimals)
            size_dec = self.risk.size(
                "buy" if action == "buy" else "sell",
                self.totals["last_equity"],
                sig.limit_px,
                maintenance_leverage=self.market.max_leverage,
                current_position=state.position,
            )
            if size_dec.allowed:
                self._cancel_resting()
                result = self.broker.place(sig, size_dec.size, book.mid, self.market.sz_decimals)
                self.totals["orders"] += 1
                if result.status in ("rejected", "error"):
                    self.totals["rejected"] += 1
                order_dict = asdict(result)
                if result.status == "filled" and result.raw.get("fill"):
                    self._apply_fill(Fill(**result.raw["fill"]))
            else:
                decision_dict["risk_block"] = size_dec.reason
                self.totals["rejected"] += 1

        event = TEvent(
            ts=int(time.time() * 1000),
            coin=self.cfg.coin,
            mid=book.mid,
            mark_px=ctx.mark_px,
            spread_bps=book.spread_bps or 0.0,
            funding_apr=ctx.funding_apr,
            decision=decision_dict,
            order=order_dict,
            position={
                "side": pos_view.side if pos_view else "flat",
                "size": pos_view.size if pos_view else 0.0,
                "entry_px": pos_view.entry_px if pos_view else 0.0,
                "unrealized": (self.broker.unrealized(self.cfg.coin, ctx.mark_px)
                               if isinstance(self.broker, PaperBroker)
                               else (pos_view.unrealized_pnl if pos_view else 0.0)),
                "liquidation_px": pos_view.liquidation_px if pos_view else None,
            },
            totals=self._totals_view(state),
            halted=halted,
            halt_reason=self.risk.halt_reason,
        )
        self.history.append(event)
        if len(self.history) > 2000:
            self.history.pop(0)
        if self.on_event:
            self.on_event(event)
        d = event.decision
        t = event.totals
        log.info(
            "#%d %s mid=%.1f spread=%.2fbps funding=%.1f%%APR %s ord=%s eq=%.2f pnl=%.3f%%%s",
            t["ticks"], self.cfg.coin, event.mid, event.spread_bps, event.funding_apr,
            (f"{d['action']} p_up={d['probabilities']['buy']:.2f} {d['latency_ms']:.0f}ms"
             if d else "no-decision"),
            (event.order or {}).get("status", "-"), t["equity"], t["pnl_pct"],
            " HALTED" if event.halted else "",
        )
        return event

    def market_model_decide(self, mstate: MarketState):
        return self.strategy.model.decide(mstate)

    def _cancel_resting(self) -> None:
        for o in self.broker.open_orders(self.cfg.coin):
            self.broker.cancel(self.cfg.coin, o.oid)

    def _equity(self, state: AccountState, mark_px: float) -> float:
        if isinstance(self.broker, PaperBroker):
            return self.broker.equity(self.base_equity, self.cfg.coin, mark_px)
        return state.account_value

    def _totals_view(self, state: AccountState) -> dict:
        start = self.totals["starting_equity"] or state.account_value or 1.0
        equity = self.totals["last_equity"]
        pnl = equity - start
        return {
            "ticks": self.totals["ticks"],
            "decisions": self.totals["decisions"],
            "orders": self.totals["orders"],
            "rejected": self.totals["rejected"],
            "fills": self.totals["fills"],
            "fees": round(self.totals["fees"], 6),
            "funding": round(self.totals["funding"], 6),
            "realized": round(self.totals["realized"], 6),
            "equity": round(equity, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / start * 100, 4),
        }

    def run_forever(self) -> None:
        log.info("trader loop starting (interval %.1fs)", self.interval_s)
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("tick failed: %s", exc)
            time.sleep(self.interval_s)

    def stop(self) -> None:
        self._stop = True
