"""Account state: positions, margin, fees and funding history.

Reads only. In paper mode this returns a synthetic account so the whole pipeline
can run without a key.
"""

from __future__ import annotations

import logging
from typing import Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .types import AccountState, Position

log = logging.getLogger("hlperp.account")


def _position(raw: dict) -> Position:
    p = raw["position"]
    szi = float(p["szi"])
    lev = p.get("leverage", {})
    return Position(
        coin=p["coin"],
        side="long" if szi > 0 else ("short" if szi < 0 else "flat"),
        size=abs(szi),
        entry_px=float(p.get("entryPx") or 0.0),
        position_value=float(p.get("positionValue") or 0.0),
        unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
        margin_used=float(p.get("marginUsed") or 0.0),
        liquidation_px=float(p["liquidationPx"]) if p.get("liquidationPx") else None,
        leverage=float(lev.get("value", 0) or 0),
        leverage_type=str(lev.get("type", "cross")),
    )


class Account:
    def __init__(self, network: str, address: Optional[str], paper_equity: float = 10_000.0) -> None:
        base = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
        self.info = Info(base, skip_ws=True)
        self.address = address
        self.paper_equity = paper_equity

    def state(self, coin: str) -> AccountState:
        if not self.address:
            return self._paper_state(coin)
        try:
            raw = self.info.user_state(self.address)
        except Exception as exc:  # pragma: no cover - network
            log.warning("user_state failed: %s", exc)
            return self._paper_state(coin)

        summary = raw.get("marginSummary", {})
        pos = None
        for ap in raw.get("assetPositions", []):
            if ap["position"]["coin"] == coin:
                pos = _position(ap)
                break
        return AccountState(
            account_value=float(summary.get("accountValue", 0.0)),
            withdrawable=float(raw.get("withdrawable", 0.0)),
            total_margin_used=float(summary.get("totalMarginUsed", 0.0)),
            total_ntl_pos=float(summary.get("totalNtlPos", 0.0)),
            position=pos,
        )

    def open_orders(self) -> list[dict]:
        if not self.address:
            return []
        try:
            return self.info.open_orders(self.address)
        except Exception:  # pragma: no cover - network
            return []

    def funding_history(self, coin: str, start_ms: int) -> list[dict]:
        if not self.address:
            return []
        try:
            return self.info.user_funding_history(coin, start_ms)
        except Exception:  # pragma: no cover - network
            return []

    def fills(self, start_ms: int) -> list[dict]:
        """User fills since ``start_ms``. Used to reconcile live P&L."""
        if not self.address:
            return []
        try:
            return self.info.user_fills_by_time(self.address, start_ms)
        except Exception as exc:  # pragma: no cover - network
            log.warning("user_fills_by_time failed: %s", exc)
            return []

    def _paper_state(self, coin: str) -> AccountState:
        return AccountState(
            account_value=self.paper_equity,
            withdrawable=self.paper_equity,
            total_margin_used=0.0,
            total_ntl_pos=0.0,
            position=None,
        )
