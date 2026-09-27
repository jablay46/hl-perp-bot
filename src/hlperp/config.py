"""Environment-driven configuration with a live-trading interlock.

Live trading requires all three of: mode == "live", ``HL_ALLOW_LIVE=true``, and
a usable key. Paper mode never signs or sends anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:  # optional at import time so tests can run without the file
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _bool(key: str, default: bool = False) -> bool:
    return (os.getenv(key, str(default)).strip().lower() == "true")


def _float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v not in (None, "") else default


def _int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v not in (None, "") else default


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    network: str
    mode: str
    allow_live: bool
    coin: str
    leverage: int
    margin_mode: str
    order_tif: str
    spread_bps: float
    horizon: int
    account_address: Optional[str]
    agent_private_key: Optional[str]
    private_key: Optional[str]
    risk_pct: float
    max_leverage: int
    maintenance_leverage: float
    max_drawdown_pct: float
    min_liq_distance_pct: float
    max_notional_pct: float
    trigger_buffer_bps: float
    tp_sl_enabled: bool
    tp_pct: float
    sl_pct: float
    model: str
    openai_api_key: Optional[str]
    openai_base_url: str
    model_id: str
    port: int
    interval_s: float

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def base_url(self) -> str:
        return (
            "https://api.hyperliquid-testnet.xyz"
            if self.network == "testnet"
            else "https://api.hyperliquid.xyz"
        )

    @property
    def signing_key(self) -> Optional[str]:
        """The key used to sign. Agent wallet preferred; fall back to main key."""
        return self.agent_private_key or self.private_key

    @property
    def is_agent_wallet(self) -> bool:
        return bool(self.agent_private_key)

    def validate(self) -> None:
        if self.network not in ("testnet", "mainnet"):
            raise ConfigError(f"HL_NETWORK must be testnet|mainnet, got {self.network!r}")
        if self.mode not in ("paper", "live"):
            raise ConfigError(f"HL_MODE must be paper|live, got {self.mode!r}")
        if self.margin_mode not in ("cross", "isolated"):
            raise ConfigError("HL_MARGIN_MODE must be cross|isolated")
        if self.order_tif not in ("ALO", "IOC", "GTC"):
            raise ConfigError("HL_ORDER_TIF must be ALO|IOC|GTC")
        if self.max_leverage < 1:
            raise ConfigError("HL_MAX_LEVERAGE must be >= 1")
        if self.maintenance_leverage <= 0:
            raise ConfigError("HL_MAINTENANCE_LEVERAGE must be > 0")
        if self.leverage < 1 or self.leverage > self.max_leverage:
            raise ConfigError(
                f"HL_LEVERAGE={self.leverage} must be between 1 and HL_MAX_LEVERAGE={self.max_leverage}"
            )
        if self.tp_sl_enabled:
            if self.tp_pct <= 0 or self.sl_pct <= 0:
                raise ConfigError("HL_TP_PCT and HL_SL_PCT must be positive when TP/SL is enabled")
            take = self.tp_pct * (1 - self.trigger_buffer_bps / 10_000)
            stop = self.sl_pct * (1 + self.trigger_buffer_bps / 10_000)
            if stop >= take:
                raise ConfigError(
                    "HL_SL_PCT is too close to HL_TP_PCT: the stop trigger would sit at or "
                    "beyond the take-profit trigger once the buffer is applied. Widen the gap "
                    "or lower HL_TRIGGER_BUFFER_BPS."
                )
        if self.mode == "live":
            if not self.allow_live:
                raise ConfigError(
                    "Refusing live trading: set HL_ALLOW_LIVE=true to confirm you understand the risk"
                )
            if not self.signing_key:
                raise ConfigError("Live mode needs HL_AGENT_PRIVATE_KEY (or HL_PRIVATE_KEY)")
            if not self.account_address:
                raise ConfigError("Live mode needs HL_ACCOUNT_ADDRESS (the wallet you query)")
            if self.network != "testnet" and self.private_key and not self.agent_private_key:
                raise ConfigError(
                    "Mainnet live trading must use an agent/API wallet, not a main private key"
                )


def load_config() -> Config:
    cfg = Config(
        network=os.getenv("HL_NETWORK", "testnet").strip().lower(),
        mode=os.getenv("HL_MODE", "paper").strip().lower(),
        allow_live=_bool("HL_ALLOW_LIVE", False),
        coin=os.getenv("HL_COIN", "BTC").strip().upper(),
        leverage=_int("HL_LEVERAGE", 5),
        margin_mode=os.getenv("HL_MARGIN_MODE", "cross").strip().lower(),
        order_tif=os.getenv("HL_ORDER_TIF", "ALO").strip().upper(),
        spread_bps=_float("HL_SPREAD_BPS", 2.0),
        horizon=_int("HL_HORIZON", 60),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS") or None,
        agent_private_key=os.getenv("HL_AGENT_PRIVATE_KEY") or None,
        private_key=os.getenv("HL_PRIVATE_KEY") or None,
        risk_pct=_float("HL_RISK_PCT", 0.01),
        max_leverage=_int("HL_MAX_LEVERAGE", 10),
        maintenance_leverage=_float("HL_MAINTENANCE_LEVERAGE", 2.0),
        max_drawdown_pct=_float("HL_MAX_DRAWDOWN_PCT", 10.0),
        min_liq_distance_pct=_float("HL_MIN_LIQ_DISTANCE_PCT", 15.0),
        max_notional_pct=_float("HL_MAX_NOTIONAL_PCT", 50.0),
        trigger_buffer_bps=_float("HL_TRIGGER_BUFFER_BPS", 5.0),
        tp_sl_enabled=_bool("HL_TP_SL", False),
        tp_pct=_float("HL_TP_PCT", 0.02),
        sl_pct=_float("HL_SL_PCT", 0.01),
        model=os.getenv("HL_MODEL", "momentum").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model_id=os.getenv("HL_MODEL_ID", "gpt-4o-mini"),
        port=_int("HL_PORT", 8787),
        interval_s=_float("HL_INTERVAL", 2.0),
    )
    cfg.validate()
    return cfg
