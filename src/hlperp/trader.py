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
from .types import AccountState, Fill, MarketState, OrderResult, Position

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
        # Live fill reconciliation state.
        self._seen_fills: set[tuple] = set()
        self._last_fill_ts = int(time.time() * 1000) - 60_000
        # Bracket state: the position size we last armed a TP/SL for.
        self._bracket_size = 0.0

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

    # -- live fill reconciliation -----------------------------------------
    def reconcile_fills(self) -> list[Fill]:
        """Pull the exchange's own fill log and fold unseen fills into totals.

        The REST response is the source of truth for live P&L: it carries the
        actual fee (already net of the maker rebate) and the closed PnL, neither
        of which can be read off the order acknowledgement. Fills are keyed by
        (time, oid, hash, tid) so a repeated poll does not double count, while two
        partial fills from one order in the same millisecond stay distinct. ``tid``
        is present on ``userFills``; the key degrades to the old triple when absent.
        """
        rows = self.account.fills(self._last_fill_ts)
        out: list[Fill] = []
        for r in rows:
            ts = int(r.get("time", 0))
            oid = int(r.get("oid", 0) or 0)
            tid = r.get("tid")
            key = (ts, oid, str(r.get("hash", "")), int(tid) if tid is not None else None)
            if key in self._seen_fills:
                continue
            self._seen_fills.add(key)
            if ts > self._last_fill_ts:
                self._last_fill_ts = ts
            # Filter before applying: a fill for another coin must not reach totals.
            if r.get("coin", self.cfg.coin) != self.cfg.coin:
                continue
            fill = Fill(
                coin=r.get("coin", self.cfg.coin),
                side="buy" if r.get("side") == "B" else "sell",
                px=float(r.get("px", 0.0)),
                sz=float(r.get("sz", 0.0)),
                fee=float(r.get("fee", 0.0)),
                closed_pnl=float(r.get("closedPnl", 0.0)),
                ts=ts,
                oid=oid,
                crossed=bool(r.get("crossed", False)),
                hash=str(r.get("hash", "")),
            )
            self._apply_fill(fill)
            out.append(fill)
        return out

    # -- take-profit / stop-loss ------------------------------------------
    def arm_bracket(self, state: AccountState) -> None:
        """Arm reduce-only TP/SL triggers once the position reaches its target.

        Trigger prices are clamped inside the liquidation price so the stop cannot
        sit beyond liquidation, where it would never fire. Re-armed only when the
        position size changes, so a resting bracket is not churned every tick.
        """
        if not self.cfg.tp_sl_enabled or not self.broker.supports_brackets:
            return
        pos, _ = self._position_view(state)
        if pos is None or pos.size <= 0:
            if self._bracket_size:
                self.broker.cancel_brackets(self.cfg.coin)
                self._bracket_size = 0.0
            return
        if abs(pos.size - self._bracket_size) <= 1e-9:
            return
        mark = self.market.ctx.mark_px if self.market.ctx else pos.entry_px
        long = pos.side == "long"
        buf = self.cfg.trigger_buffer_bps / 10_000
        tp = mark * ((1 + self.cfg.tp_pct) if long else (1 - self.cfg.tp_pct))
        sl = mark * ((1 - self.cfg.sl_pct) if long else (1 + self.cfg.sl_pct))
        # Keep the stop strictly inside liquidation; a stop past it never triggers.
        liq = pos.liquidation_px
        if liq is not None:
            sl = max(sl, liq * (1 + buf)) if long else min(sl, liq * (1 - buf))
        # Both triggers must be on the correct side of the live mark.
        if long:
            tp = max(tp, mark * (1 + buf))
            sl = min(sl, mark * (1 - buf))
        else:
            tp = min(tp, mark * (1 - buf))
            sl = max(sl, mark * (1 + buf))
        self.broker.cancel_brackets(self.cfg.coin)
        ok = self.broker.place_bracket(self.cfg.coin, long, pos.size, tp, sl)
        self._bracket_size = pos.size if ok else 0.0

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
            mark = self.market.ctx.mark_px if self.market.ctx else p.entry_px
            value = abs(p.size) * mark
            pos = Position(
                coin=self.cfg.coin,
                side="long" if p.size > 0 else "short",
                size=abs(p.size),
                entry_px=p.entry_px,
                position_value=value,
                unrealized_pnl=self.broker.unrealized(self.cfg.coin, mark),
                margin_used=value / max(1, self.cfg.leverage),
                liquidation_px=None,
                leverage=float(self.cfg.leverage),
                leverage_type="paper",
            )
            return pos, value
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
        if not isinstance(self.broker, PaperBroker):
            self.reconcile_fills()
        self.arm_bracket(state)

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
                exchange_max_leverage=self.market.max_leverage,
                current_position=pos_view,
                sz_decimals=self.market.sz_decimals,
            )
            if size_dec.allowed:
                # The risk engine may flip the side (e.g. an existing long that
                # needs unwinding); honour the order it actually wants.
                order_side = size_dec.side or ("buy" if action == "buy" else "sell")
                if order_side != ("buy" if action == "buy" else "sell"):
                    sig = self.strategy.signal_for(order_side, book, self.market.sz_decimals)
                self._cancel_resting()
                result = self.broker.place(sig, size_dec.size, book.mid, self.market.sz_decimals)
                self.totals["orders"] += 1
                if result.status in ("rejected", "error"):
                    self.totals["rejected"] += 1
                order_dict = asdict(result)
                order_dict["risk"] = {
                    "notional": round(size_dec.notional, 2),
                    "liq_distance_pct": size_dec.liq_distance_pct,
                }
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
        order = event.order or {}
        status = order.get("status", "-")
        why = ""
        if status in ("rejected", "error"):
            why = " (" + str(order.get("error") or "rejected")[:80] + ")"
        elif d and d.get("risk_block"):
            why = " (risk: " + str(d["risk_block"])[:80] + ")"
        if not t.get("funded", True):
            why += " [account unfunded]"
        log.info(
            "#%d %s mid=%.1f spread=%.2fbps funding=%.1f%%APR %s ord=%s%s eq=%.2f pnl=%.3f%%%s",
            t["ticks"], self.cfg.coin, event.mid, event.spread_bps, event.funding_apr,
            (f"{d['action']} p_up={d['probabilities']['buy']:.2f} {d['latency_ms']:.0f}ms"
             if d else "no-decision"),
            status, why, t["equity"], t["pnl_pct"],
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
        start = self.totals["starting_equity"] or state.account_value
        equity = self.totals["last_equity"]
        # An unfunded account has equity 0; reporting a -100% PnL against a zero
        # baseline is noise, not information. Surface it as unfunded instead.
        funded = bool(start and start > 0)
        pnl = (equity - start) if funded else 0.0
        pnl_pct = (pnl / start * 100) if funded else 0.0
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
            "pnl_pct": round(pnl_pct, 4),
            "funded": funded,
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
