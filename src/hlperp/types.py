"""Wire and domain types shared across the bot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

Side = Literal["buy", "sell"]
PositionSide = Literal["long", "short", "flat"]
Action = Literal["buy", "sell", "hold"]


@dataclass
class Level:
    px: float
    sz: float


@dataclass
class Book:
    coin: str
    ts: int
    bids: list[Level]
    asks: list[Level]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].px if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> Optional[float]:
        if not self.mid or self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_ask - self.best_bid) / self.mid * 10_000


@dataclass
class AssetCtx:
    """Per-asset context from ``metaAndAssetCtxs``."""

    coin: str
    mark_px: float
    mid_px: Optional[float]
    oracle_px: float
    funding_hourly: float
    open_interest: float
    day_ntl_vlm: float
    prev_day_px: float
    premium: Optional[float]

    @property
    def funding_apr(self) -> float:
        """Annualised funding, using the hourly rate actually charged."""
        return self.funding_hourly * 24 * 365


@dataclass
class Position:
    coin: str
    side: PositionSide
    size: float
    entry_px: float
    position_value: float
    unrealized_pnl: float
    margin_used: float
    liquidation_px: Optional[float]
    leverage: float
    leverage_type: str  # "cross" | "isolated"

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "long" else (-self.size if self.side == "short" else 0.0)


@dataclass
class AccountState:
    account_value: float
    withdrawable: float
    total_margin_used: float
    total_ntl_pos: float
    position: Optional[Position]


@dataclass
class TradePrint:
    coin: str
    px: float
    sz: float
    side: Side
    ts: int
    crossed: bool


@dataclass
class Fill:
    coin: str
    side: Side
    px: float
    sz: float
    fee: float
    closed_pnl: float
    ts: int
    oid: int
    crossed: bool
    hash: str


@dataclass
class Decision:
    action: Action
    probabilities: dict[str, float]
    up: float
    latency_ms: float
    input_tokens: int
    reason: str = ""


@dataclass
class Signal:
    """What the strategy wants done this tick, before risk sizing."""

    coin: str
    side: Side
    tif: str  # ALO | IOC | GTC
    limit_px: float
    reduce_only: bool = False
    reason: str = ""


@dataclass
class OrderResult:
    coin: str
    side: Side
    px: float
    sz: float
    tif: str
    oid: Optional[int]
    status: str  # resting | filled | simulated | rejected | error
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class MarketState:
    """Compact, typed view of the market handed to the model."""

    coin: str
    ts: int
    mid: float
    mark_px: float
    oracle_px: float
    spread_bps: float
    funding_hourly: float
    funding_apr: float
    open_interest: float
    day_ntl_vlm: float
    book_imbalance: float
    depth_usd: dict[str, dict[str, float]]
    returns_bps: dict[str, float]
    recent_mids: str
    trades: dict[str, float | int | None]
    recent_trades: list[str]
    position_side: PositionSide
    position_size: float
    unrealized_pnl: float
    account_value: float
    allowed: dict[str, bool]

    def to_dict(self) -> dict:
        return asdict(self)
