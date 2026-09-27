"""Market data adapter: order book, asset context, candles and a live feed.

Uses the official SDK's ``Info`` surface for request/response snapshots and a
raw WebSocket for the lowest-latency ``l2Book`` / ``trades`` / ``activeAssetCtx``
streams. Everything here is read-only: this module cannot move funds.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .types import AssetCtx, Book, Level, TradePrint

log = logging.getLogger("hlperp.market")

WS_URLS = {
    "testnet": "wss://api.hyperliquid-testnet.xyz/ws",
    "mainnet": "wss://api.hyperliquid.xyz/ws",
}


def _levels(raw: list[dict]) -> list[Level]:
    return [Level(px=float(x["px"]), sz=float(x["sz"])) for x in raw]


class MarketData:
    def __init__(self, network: str, coin: str) -> None:
        self.network = network
        self.coin = coin
        base = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
        # skip_ws=True: we manage our own socket for the hot stream.
        self.info = Info(base, skip_ws=True)
        self._lock = threading.Lock()
        self._book: Optional[Book] = None
        self._mids: dict[str, float] = {}
        self._ctx: Optional[AssetCtx] = None
        self._trades: deque[TradePrint] = deque(maxlen=500)
        self._stop = threading.Event()
        self._sub_on_book: Optional[Callable[[Book], None]] = None
        self._sub_on_trade: Optional[Callable[[TradePrint], None]] = None

        meta = self.info.meta()
        self.universe = {a["name"]: a for a in meta["universe"]}
        if coin not in self.universe:
            raise ValueError(f"{coin} is not in the Hyperliquid universe")
        self.asset_index: int = self.info.name_to_asset(coin)
        self.sz_decimals: int = self.universe[coin]["szDecimals"]
        self.max_leverage: int = self.universe[coin]["maxLeverage"]

    # -- snapshots ---------------------------------------------------------
    def fetch_book(self) -> Book:
        raw = self.info.l2_snapshot(self.coin)
        return Book(
            coin=self.coin,
            ts=int(raw.get("time", time.time() * 1000)),
            bids=_levels(raw["levels"][0]),
            asks=_levels(raw["levels"][1]),
        )

    def fetch_ctx(self) -> AssetCtx:
        meta, ctxs = self.info.meta_and_asset_ctxs()
        idx = next(i for i, a in enumerate(meta["universe"]) if a["name"] == self.coin)
        c = ctxs[idx]
        return AssetCtx(
            coin=self.coin,
            mark_px=float(c["markPx"]),
            mid_px=float(c["midPx"]) if c.get("midPx") else None,
            oracle_px=float(c["oraclePx"]),
            funding_hourly=float(c.get("funding", 0.0)),
            open_interest=float(c.get("openInterest", 0.0)),
            day_ntl_vlm=float(c.get("dayNtlVlm", 0.0)),
            prev_day_px=float(c.get("prevDayPx", 0.0)),
            premium=float(c["premium"]) if c.get("premium") is not None else None,
        )

    def fetch_candles(self, interval: str = "1m", lookback_ms: int = 3_600_000) -> list[dict]:
        now = int(time.time() * 1000)
        return self.info.candles_snapshot(self.coin, interval, now - lookback_ms, now)

    # -- live accessors ----------------------------------------------------
    @property
    def book(self) -> Optional[Book]:
        with self._lock:
            return self._book

    @property
    def ctx(self) -> Optional[AssetCtx]:
        with self._lock:
            return self._ctx

    @property
    def mid(self) -> Optional[float]:
        with self._lock:
            if self._mids.get(self.coin):
                return self._mids[self.coin]
            if self._book and self._book.mid:
                return self._book.mid
        return None

    def recent_trades(self, n: int = 50) -> list[TradePrint]:
        with self._lock:
            return list(self._trades)[-n:]

    def refresh_ctx(self) -> None:
        try:
            ctx = self.fetch_ctx()
            with self._lock:
                self._ctx = ctx
        except Exception as exc:  # pragma: no cover - network
            log.warning("ctx refresh failed: %s", exc)

    # -- websocket ---------------------------------------------------------
    def start(
        self,
        on_book: Optional[Callable[[Book], None]] = None,
        on_trade: Optional[Callable[[TradePrint], None]] = None,
    ) -> None:
        self._sub_on_book = on_book
        self._sub_on_trade = on_trade
        self.refresh_ctx()
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def _send_subs(self, ws) -> None:
        subs = [
            {"type": "l2Book", "coin": self.coin},
            {"type": "trades", "coin": self.coin},
            {"type": "activeAssetCtx", "coin": self.coin},
        ]
        for s in subs:
            ws.send(json.dumps({"method": "subscribe", "subscription": s}))

    def _ws_loop(self) -> None:
        import websockets.sync.client as ws_client

        url = WS_URLS[self.network]
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with ws_client.connect(url, open_timeout=10, close_timeout=5) as ws:
                    self._send_subs(ws)
                    backoff = 1.0
                    last_ping = time.time()
                    while not self._stop.is_set():
                        try:
                            msg = ws.recv(timeout=5)
                        except TimeoutError:
                            if time.time() - last_ping > 20:
                                ws.send(json.dumps({"method": "ping"}))
                                last_ping = time.time()
                            continue
                        self._handle(json.loads(msg))
            except Exception as exc:  # pragma: no cover - network
                log.warning("ws disconnected (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, m: dict) -> None:
        if m.get("channel") == "l2Book":
            d = m["data"]
            book = Book(
                coin=d["coin"],
                ts=d["time"],
                bids=_levels(d["levels"][0]),
                asks=_levels(d["levels"][1]),
            )
            with self._lock:
                self._book = book
            cb = getattr(self, "_sub_on_book", None)
            if cb:
                cb(book)
        elif m.get("channel") == "trades":
            cb = getattr(self, "_sub_on_trade", None)
            with self._lock:
                for t in m["data"]:
                    tp = TradePrint(
                        coin=t["coin"],
                        px=float(t["px"]),
                        sz=float(t["sz"]),
                        side=t["side"] if t["side"] in ("buy", "sell") else "buy",
                        ts=int(t["time"]),
                        crossed=True,
                    )
                    self._trades.append(tp)
                    if cb:
                        cb(tp)
        elif m.get("channel") == "activeAssetCtx":
            d = m["data"]
            c = d["ctx"]
            with self._lock:
                self._ctx = AssetCtx(
                    coin=d["coin"],
                    mark_px=float(c["markPx"]),
                    mid_px=float(c["midPx"]) if c.get("midPx") else None,
                    oracle_px=float(c["oraclePx"]),
                    funding_hourly=float(c.get("funding", 0.0)),
                    open_interest=float(c.get("openInterest", 0.0)),
                    day_ntl_vlm=float(c.get("dayNtlVlm", 0.0)),
                    prev_day_px=float(c.get("prevDayPx", 0.0)),
                    premium=float(c["premium"]) if c.get("premium") is not None else None,
                )

    def stop(self) -> None:
        self._stop.set()
        try:
            self.info.disconnect_websocket()
        except Exception:
            pass
