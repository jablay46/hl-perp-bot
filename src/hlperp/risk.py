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
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Optional

from .config import Config
from .rounding import floor_sz

log = logging.getLogger("hlperp.risk")

# Hyperliquid rejects any order whose value is below this, with ``MinTradeNtl``.
# The boundary is exact: 10 is accepted, 9.99 is not. Compared as Decimal, never
# as float, so the boundary does not fall on the wrong side of binary rounding.
MIN_ORDER_NOTIONAL = 10.0
_MIN_ORDER_NOTIONAL_D = Decimal("10")


def _lot_floor_d(size: float | str, sz_decimals: int) -> Decimal:
    """Truncate a size to a whole number of lots, in Decimal (no float drift)."""
    quantum = Decimal(1).scaleb(-int(sz_decimals))
    return Decimal(str(size)).quantize(quantum, rounding=ROUND_DOWN)


def min_order_notional(px: float, sz_decimals: int) -> float:
    """The smallest order value Hyperliquid will accept at this lot size.

    Not simply $10: the size must be a whole number of lots, so the minimum is the
    first lot multiple whose value reaches $10. At BTC's 5 decimals and $83k that
    is 0.00013 BTC = $10.79, which is why an account sized for exactly $10 is
    rejected.
    """
    if px <= 0 or sz_decimals < 0:
        return MIN_ORDER_NOTIONAL
    px_d = Decimal(str(px))
    lot = Decimal(1).scaleb(-int(sz_decimals))
    # Smallest whole number of lots whose notional reaches $10. Ceiling on a
    # Decimal quotient cannot land one lot short the way float division can.
    lots = (_MIN_ORDER_NOTIONAL_D / (px_d * lot)).to_integral_value(rounding=ROUND_CEILING)
    return float(lots * lot * px_d)


def effective_target_notional(equity: float, risk_pct: float, leverage: int,
                              max_notional_pct: float) -> float:
    """The target exposure :meth:`RiskEngine.size` aims at, cap included.

    Kept in one place so the sizing path and the preflight warning cannot compute
    two different targets and disagree about whether an account is tradeable.
    """
    return min(equity * risk_pct * leverage, equity * max_notional_pct / 100.0)


def min_equity_for_order(risk_pct: float, leverage: int, px: float, sz_decimals: int,
                         max_notional_pct: float = 100.0) -> float:
    """Equity needed before a single order clears the venue minimum.

    ``max_notional_pct`` matters: when it binds, raising equity does not raise the
    target size proportionally, so the equity needed is higher than the raw
    ``risk_pct * leverage`` figure suggests. The cap is linear in equity, so the
    requirement is a threshold on the capped fraction.
    """
    per_equity = min(risk_pct * leverage, max_notional_pct / 100.0)
    if per_equity <= 0:
        return float("inf")
    return min_order_notional(px, sz_decimals) / per_equity


@dataclass
class RiskDecision:
    allowed: bool
    size: float
    reason: str
    side: str = ""
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
    def required_distance_pct(self, leverage: float) -> float:
        """The liquidation distance implied by a leverage, as a percentage of price.

        Isolated margin, maintenance leverage ``L`` and entry leverage ``lev``:

            liq_price = entry * (1 - side * (1/lev) / (1 - side/L))

        so the distance ``|entry - liq| / entry`` is ``(1/lev) / (1 - 1/L)``, which
        is independent of size. A 10x position with 10x maintenance therefore sits
        about 5.6% from liquidation; an isolated 40x one is under 1.3% away.
        """
        if leverage <= 0:
            return 0.0
        l = 1.0 / self.cfg.maintenance_leverage
        if l >= 1.0:
            return 0.0
        return (1.0 / leverage) / (1.0 - l) * 100.0

    def size(
        self,
        side: str,
        equity: float,
        px: float,
        exchange_max_leverage: float,
        current_position: Optional[object] = None,
        sz_decimals: Optional[int] = None,
    ) -> RiskDecision:
        """Size the next order as the delta toward a target exposure.

        ``side`` is the requested direction (buy|sell). The target notional is
        ``equity * risk_pct * leverage``, capped by ``max_notional_pct`` of equity,
        and clipped so we never request more than the exchange's max leverage
        allows. The returned size is the *difference* from the position already
        held, so repeat ticks with an unchanged model do not stack.

        When ``sz_decimals`` is given the size is lot-truncated and checked against
        Hyperliquid's $10 minimum here, because the exchange would otherwise reject
        the order and the loop would look healthy while never trading.
        """
        if self.check_kill_switch(equity):
            return RiskDecision(False, 0.0, f"halted: {self.halt_reason}")
        if px <= 0:
            return RiskDecision(False, 0.0, "no price")

        direction = 1.0 if side == "buy" else -1.0

        # The mainnet maintenance margin is not 1 / max leverage; using max
        # leverage here would let a position size through that the book cannot
        # support. Refuse to trade if our maintenance assumption is inconsistent.
        if self.cfg.leverage > exchange_max_leverage:
            return RiskDecision(
                False, 0.0,
                f"HL_LEVERAGE={self.cfg.leverage} exceeds the exchange max {exchange_max_leverage:g} for this coin",
            )

        target_notional = effective_target_notional(
            equity, self.cfg.risk_pct, self.cfg.leverage, self.cfg.max_notional_pct
        )
        if target_notional <= 0:
            return RiskDecision(False, 0.0, "sized to zero")

        cur_signed = self._signed_notional(current_position)
        delta = direction * target_notional - cur_signed
        if abs(delta) < 1e-12:
            return RiskDecision(False, 0.0, "already at target exposure")

        order_side = "buy" if delta > 0 else "sell"
        size = abs(delta) / px
        if sz_decimals is not None:
            size = floor_sz(size, sz_decimals)
            if size <= 0:
                return RiskDecision(
                    False, 0.0,
                    f"size rounds to 0 at {sz_decimals} decimals (target ${abs(delta):.2f})",
                )
        # Both figures below must describe the order that is actually sent, i.e.
        # the lot-truncated size, not the untruncated delta. Using the raw delta
        # overstates the order and can report a notional the venue never saw.
        order_notional_d = _lot_floor_d(size, sz_decimals) * Decimal(str(px)) if sz_decimals is not None else Decimal(str(size)) * Decimal(str(px))
        order_notional = float(order_notional_d)
        if order_notional < MIN_ORDER_NOTIONAL:
            if sz_decimals is not None:
                need = min_equity_for_order(self.cfg.risk_pct, self.cfg.leverage, px,
                                            sz_decimals, self.cfg.max_notional_pct)
                hint = (
                    f"a whole lot is ${px * 10.0 ** -sz_decimals:.2f}, so the smallest "
                    f"acceptable order is ${min_order_notional(px, sz_decimals):.2f}; "
                    f"need about ${need:.0f} equity"
                )
            else:
                need = MIN_ORDER_NOTIONAL / max(
                    effective_target_notional(1.0, self.cfg.risk_pct, self.cfg.leverage,
                                              self.cfg.max_notional_pct), 1e-9)
                hint = f"need about ${need:.0f} equity"
            return RiskDecision(
                False, 0.0,
                f"order ${order_notional:.2f} is below Hyperliquid's "
                f"${MIN_ORDER_NOTIONAL:.0f} minimum ({hint} at HL_LEVERAGE="
                f"{self.cfg.leverage}, HL_RISK_PCT={self.cfg.risk_pct})",
                notional=order_notional,
            )
        # The exposure after this order, again from the actual truncated size.
        signed_notional = direction * order_notional
        resulting_notional = abs(cur_signed + signed_notional)
        # Resulting margin must fit inside equity at the requested leverage.
        if resulting_notional / self.cfg.leverage > equity:
            return RiskDecision(
                False, 0.0,
                f"resulting margin {resulting_notional / self.cfg.leverage:.2f} exceeds equity {equity:.2f}",
                notional=resulting_notional,
            )

        dist = self.required_distance_pct(self.cfg.leverage)
        if dist and dist < self.cfg.min_liq_distance_pct:
            return RiskDecision(
                False, 0.0,
                f"liquidation distance at {self.cfg.leverage}x is {dist:.1f}% "
                f"(< {self.cfg.min_liq_distance_pct}%)",
                notional=resulting_notional, liq_distance_pct=dist,
            )

        return RiskDecision(True, size, "ok", side=order_side,
                            notional=order_notional, liq_distance_pct=dist)

    @staticmethod
    def _signed_notional(position: Optional[object]) -> float:
        if position is None:
            return 0.0
        value = abs(float(getattr(position, "position_value", 0.0) or 0.0))
        side = getattr(position, "side", "flat")
        if side == "long":
            return value
        if side == "short":
            return -value
        return 0.0
