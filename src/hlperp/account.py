"""Account state: positions, margin, fees and funding history.

Reads only. In paper mode this returns a synthetic account so the whole pipeline
can run without a key.
"""

from __future__ import annotations

import logging
import time
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

    def perp_usdc(self) -> float:
        """Transferable perp balance. Zero means no perp order can be placed."""
        if not self.address:
            return self.paper_equity
        try:
            raw = self.info.user_state(self.address)
        except Exception as exc:  # pragma: no cover - network
            log.warning("perp balance check failed: %s", exc)
            return 0.0
        return float(raw.get("withdrawable", 0.0) or 0.0)

    def spot_usdc(self) -> float:
        """Spot USDC balance. Deposits land here and must be moved to perp."""
        if not self.address:
            return 0.0
        try:
            raw = self.info.spot_user_state(self.address)
        except Exception as exc:  # pragma: no cover - network
            log.warning("spot balance check failed: %s", exc)
            return 0.0
        for b in raw.get("balances", []):
            if b.get("coin") == "USDC":
                return float(b.get("total", 0.0) or 0.0)
        return 0.0

    def recent_ledger(self, lookback_ms: int) -> list[dict]:
        if not self.address:
            return []
        start = int(time.time() * 1000) - lookback_ms
        try:
            return self.info.user_non_funding_ledger_updates(self.address, start)
        except Exception as exc:  # pragma: no cover - network
            log.warning("ledger fetch failed: %s", exc)
            return []

    def _paper_state(self, coin: str) -> AccountState:
        return AccountState(
            account_value=self.paper_equity,
            withdrawable=self.paper_equity,
            total_margin_used=0.0,
            total_ntl_pos=0.0,
            position=None,
        )
