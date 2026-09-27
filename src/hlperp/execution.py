"""Execution adapter: place, cancel and simulate orders.

Two brokers share one interface so the trader loop is identical in paper and
live mode:

* :class:`LiveBroker` signs with the SDK ``Exchange`` surface. ALO places a
  post-only limit order that earns the maker fee or is cancelled rather than
  crossing; IOC takes.
* :class:`PaperBroker` keeps resting orders in memory and fills them when a real
  trade print crosses their price. No keys, no signing, no risk.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .rounding import round_px, round_sz, wire
from .types import Fill, OrderResult, Signal, Side

log = logging.getLogger("hlperp.exec")


@dataclass
class RestingOrder:
    oid: int
    coin: str
    side: Side
    px: float
    sz: float
    ts: int
    simulated: bool


class Broker(Protocol):
    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> None: ...
    def place(self, sig: Signal, sz: float, mark_px: float, sz_decimals: int) -> OrderResult: ...
    def cancel(self, coin: str, oid: int) -> None: ...
    def cancel_all(self, coin: str) -> None: ...
    def open_orders(self, coin: str) -> list[RestingOrder]: ...
    def on_print(self, coin: str, px: float, ts: int, is_buy: bool) -> list[Fill]: ...


# ---------------------------------------------------------------------------


class LiveBroker:
    def __init__(self, exchange, asset_index: int) -> None:
        self.ex = exchange
        self.asset_index = asset_index

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> None:
        resp = self.ex.update_leverage(leverage, coin, is_cross)
        log.info("update_leverage %s x%d cross=%s -> %s", coin, leverage, is_cross, resp)

    def place(self, sig: Signal, sz: float, mark_px: float, sz_decimals: int) -> OrderResult:
        px = round_px(sig.limit_px, sz_decimals)
        sz = round_sz(sz, sz_decimals)
        is_buy = sig.side == "buy"
        tif = sig.tif.lower()
        order_type = {"limit": {"tif": tif}}
        try:
            resp = self.ex.order(
                sig.coin, is_buy, sz, px, order_type, reduce_only=sig.reduce_only
            )
        except Exception as exc:  # pragma: no cover - network
            return OrderResult(sig.coin, sig.side, px, sz, sig.tif, None, "error", str(exc))

        try:
            statuses = resp["response"]["data"]["statuses"]
            st = statuses[0]
            if "resting" in st:
                return OrderResult(sig.coin, sig.side, px, sz, sig.tif, st["resting"]["oid"], "resting", None, resp)
            if "filled" in st:
                return OrderResult(sig.coin, sig.side, px, sz, sig.tif, st["filled"].get("oid"), "filled", None, resp)
            if "error" in st:
                return OrderResult(sig.coin, sig.side, px, sz, sig.tif, None, "rejected", st["error"], resp)
        except Exception as exc:
            return OrderResult(sig.coin, sig.side, px, sz, sig.tif, None, "error", str(exc), resp or {})
        return OrderResult(sig.coin, sig.side, px, sz, sig.tif, None, "error", "unknown status", resp or {})

    def cancel(self, coin: str, oid: int) -> None:
        try:
            self.ex.cancel(coin, oid)
        except Exception as exc:  # pragma: no cover - network
            log.warning("cancel %s failed: %s", oid, exc)

    def cancel_all(self, coin: str) -> None:
        try:
            oids = [o["oid"] for o in self.ex.info.open_orders(self.ex.account_address)]
            if oids:
                self.ex.bulk_cancel([{"coin": coin, "oid": oid} for oid in oids])
        except Exception as exc:  # pragma: no cover - network
            log.warning("cancel_all failed: %s", exc)

    def open_orders(self, coin: str) -> list[RestingOrder]:
        try:
            rows = self.ex.info.open_orders(self.ex.account_address)
        except Exception:  # pragma: no cover
            return []
        out = []
        for r in rows:
            if r.get("coin") != coin:
                continue
            out.append(
                RestingOrder(
                    oid=r["oid"],
                    coin=r["coin"],
                    side="buy" if r["side"] == "B" else "sell",
                    px=float(r["limitPx"]),
                    sz=float(r["sz"]),
                    ts=int(r["timestamp"]),
                    simulated=False,
                )
            )
        return out

    def on_print(self, coin: str, px: float, ts: int, is_buy: bool) -> list[Fill]:
        # Live fills arrive through the exchange, not from the tape.
        return []


# ---------------------------------------------------------------------------


@dataclass
class _PaperPosition:
    size: float = 0.0
    entry_px: float = 0.0
    realized: float = 0.0


class PaperBroker:
    """Simulated matching: a resting bid fills if a print trades at or below it."""

    def __init__(self, coin: str, maker_bps: float = 1.5, taker_bps: float = 4.5,
                 slippage_bps: float = 2.0, seed: int = 7) -> None:
        self.coin = coin
        self.maker_rate = maker_bps / 10_000
        self.taker_rate = taker_bps / 10_000
        self.slippage = slippage_bps / 10_000
        self._oid = 1000
        self._orders: dict[int, RestingOrder] = {}
        self._pos: dict[str, _PaperPosition] = {coin: _PaperPosition()}
        self._lock = threading.Lock()
        self.rng = random.Random(seed)
        self.fees_paid = 0.0
        self.funding_paid = 0.0

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> None:
        log.info("[paper] leverage %s x%d cross=%s", coin, leverage, is_cross)

    def place(self, sig: Signal, sz: float, mark_px: float, sz_decimals: int) -> OrderResult:
        px = round_px(sig.limit_px, sz_decimals)
        sz = round_sz(sz, sz_decimals)
        tif = sig.tif.upper()
        if tif == "ALO":
            # Post-only: if the price would cross, the venue cancels it instead.
            crossing = (sig.side == "buy" and px >= mark_px) or (sig.side == "sell" and px <= mark_px)
            if crossing:
                return OrderResult(sig.coin, sig.side, px, sz, tif, None, "rejected",
                                   "post-only order would cross", {})
            self._oid += 1
            order = RestingOrder(self._oid, sig.coin, sig.side, px, sz, int(time.time() * 1000), True)
            with self._lock:
                self._orders[order.oid] = order
            return OrderResult(sig.coin, sig.side, px, sz, tif, order.oid, "resting", None, {"simulated": True})

        # IOC: cross immediately with slippage.
        fill_px = px * (1 + self.slippage if sig.side == "buy" else 1 - self.slippage)
        fill_px = round_px(fill_px, sz_decimals)
        fill = self._apply_fill(sig.side, fill_px, sz, crossed=True)
        return OrderResult(sig.coin, sig.side, fill_px, sz, tif, None, "filled", None,
                           {"simulated": True, "fill": fill.__dict__})

    def cancel(self, coin: str, oid: int) -> None:
        with self._lock:
            self._orders.pop(oid, None)

    def cancel_all(self, coin: str) -> None:
        with self._lock:
            self._orders.clear()

    def open_orders(self, coin: str) -> list[RestingOrder]:
        with self._lock:
            return [o for o in self._orders.values() if o.coin == coin]

    def on_print(self, coin: str, px: float, ts: int, is_buy: bool) -> list[Fill]:
        """Fill resting orders whose price the print crossed."""
        fills: list[Fill] = []
        with self._lock:
            for oid in list(self._orders):
                o = self._orders[oid]
                if o.side == "buy" and px <= o.px:
                    fills.append(self._apply_fill("buy", o.px, o.sz, crossed=False, ts=ts))
                    del self._orders[oid]
                elif o.side == "sell" and px >= o.px:
                    fills.append(self._apply_fill("sell", o.px, o.sz, crossed=False, ts=ts))
                    del self._orders[oid]
        return fills

    def position(self, coin: str) -> _PaperPosition:
        return self._pos[coin]

    def charge_funding(self, cost: float) -> None:
        self.funding_paid += cost

    def unrealized(self, coin: str, mark_px: float) -> float:
        pos = self._pos[coin]
        if pos.size == 0:
            return 0.0
        direction = 1 if pos.size > 0 else -1
        return (mark_px - pos.entry_px) * abs(pos.size) * direction

    def equity(self, base: float, coin: str, mark_px: float) -> float:
        pos = self._pos[coin]
        return base + pos.realized - self.fees_paid - self.funding_paid + self.unrealized(coin, mark_px)

    def _apply_fill(self, side: Side, px: float, sz: float, crossed: bool, ts: Optional[int] = None) -> Fill:
        ts = ts or int(time.time() * 1000)
        pos = self._pos[self.coin]
        signed = sz if side == "buy" else -sz
        closed_pnl = 0.0
        new_size = pos.size + signed
        if pos.size != 0 and (pos.size > 0) != (signed > 0):
            closed_sz = min(abs(pos.size), abs(signed))
            direction = 1 if pos.size > 0 else -1
            closed_pnl = (px - pos.entry_px) * closed_sz * direction
            pos.realized += closed_pnl
        if new_size == 0:
            pos.entry_px = 0.0
        elif (pos.size > 0) == (signed > 0) or pos.size == 0:
            total = abs(pos.size) + abs(signed)
            pos.entry_px = (pos.entry_px * abs(pos.size) + px * abs(signed)) / total
        pos.size = new_size
        rate = self.taker_rate if crossed else self.maker_rate
        fee = sz * px * rate
        self.fees_paid += fee
        return Fill(self.coin, side, px, sz, fee, closed_pnl, ts, 0, crossed, "paper")
