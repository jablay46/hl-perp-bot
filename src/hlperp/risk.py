"""Risk engine: sizing, leverage checks, liquidation distance and kill-switch.

This is the layer ``jev-trader`` does not have. Every order passes through
:meth:`RiskEngine.size` before it reaches the exchange, and the engine can halt
trading outright when drawdown breaches the configured limit.

Liquidation price uses the Hyperliquid formula:

    liq_price = price - side * margin_available / position_size / (1 - l * side)

with ``l = 1 / MAINTENANCE_LEVERAGE`` and ``side = +1`` for long, ``-1`` for
short. Maintenance leverage is conservatively approximated for a single tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config

log = logging.getLogger("hlperp.risk")


@dataclass
class RiskDecision:
    allowed: bool
    size: float
    reason: str
    notional: float = 0.0
    liq_distance_pct: Optional[float] = None


def liquidation_price(
    entry_px: float,
    size: float,
    side: str,
    margin_available: float,
    maintenance_leverage: float,
) -> Optional[float]:
    if size <= 0 or maintenance_leverage <= 0:
        return None
    s = 1.0 if side == "long" else -1.0
    l = 1.0 / maintenance_leverage
    denom = 1.0 - l * s
    if denom == 0:
        return None
    return entry_px - s * margin_available / size / denom


class RiskEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.halted = False
        self.peak_equity: Optional[float] = None
        self.halt_reason: str = ""

    # -- account level -----------------------------------------------------
    def observe_equity(self, equity: float) -> None:
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def drawdown_pct(self, equity: float) -> float:
        if not self.peak_equity:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

    def check_kill_switch(self, equity: float) -> bool:
        if self.halted:
            return True
        dd = self.drawdown_pct(equity)
        if dd >= self.cfg.max_drawdown_pct:
            self.halted = True
            self.halt_reason = f"drawdown {dd:.1f}% >= limit {self.cfg.max_drawdown_pct}%"
            log.error("KILL SWITCH: %s", self.halt_reason)
        return self.halted

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    # -- order level -------------------------------------------------------
    def size(
        self,
        side: str,
        equity: float,
        px: float,
        maintenance_leverage: float,
        current_position: Optional[object] = None,
    ) -> RiskDecision:
        """Size a new exposure. ``side`` is buy|sell (the direction of the order)."""
        if self.check_kill_switch(equity):
            return RiskDecision(False, 0.0, f"halted: {self.halt_reason}")
        if px <= 0:
            return RiskDecision(False, 0.0, "no price")

        risk_usd = equity * self.cfg.risk_pct
        notional = risk_usd * self.cfg.leverage

        # Hard cap: never let one position exceed a fraction of equity.
        max_notional = equity * (self.cfg.max_notional_pct / 100.0)
        notional = min(notional, max_notional)

        size = notional / px
        if size <= 0:
            return RiskDecision(False, 0.0, "sized to zero")

        # Liquidation distance: refuse exposure whose liquidation sits closer
        # than the configured floor, which is what actually kills accounts.
        margin_available = notional / self.cfg.leverage
        liq = liquidation_price(px, size, "long" if side == "buy" else "short", margin_available, maintenance_leverage)
        if liq is not None:
            dist = abs(px - liq) / px * 100.0
            if dist < self.cfg.min_liq_distance_pct:
                return RiskDecision(
                    False, 0.0, f"liquidation only {dist:.1f}% away (< {self.cfg.min_liq_distance_pct}%)",
                    notional=notional, liq_distance_pct=dist,
                )
        else:
            dist = None

        return RiskDecision(True, size, "ok", notional=notional, liq_distance_pct=dist)
