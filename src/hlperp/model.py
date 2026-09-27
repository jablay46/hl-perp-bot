"""Model layer: a typed decision about the next move.

``MomentumModel`` is a deterministic stand-in so the pipeline runs with no API
key. ``OpenAIModel`` asks a real LLM the same typed question. Both answer the
same :class:`~hlperp.types.Decision`, so swapping them changes nothing else.

Note the deliberate difference from ``jev-trader``: the question is framed for a
perpetual book, and the state includes funding, open interest and mark/oracle
basis, which are the signals that matter on perps.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.request
from typing import Protocol

from .types import Decision, MarketState

log = logging.getLogger("hlperp.model")


class Model(Protocol):
    name: str

    def decide(self, state: MarketState) -> Decision: ...


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


class MomentumModel:
    """Momentum + book imbalance + taker flow, pulled toward flat."""

    name = "momentum"

    def decide(self, state: MarketState) -> Decision:
        t0 = time.perf_counter()
        r = state.returns_bps
        flow = state.trades.get("cvd_ratio") or 0.0
        signal = (
            r.get("last20", 0.0) / 6.0
            + state.book_imbalance * 0.8
            + float(flow) * 1.0
            + (state.funding_apr / 100.0) * 0.5
        )
        up = _sigmoid(signal)
        action = "buy" if up >= 0.5 else "sell"
        return Decision(
            action=action,
            probabilities={"buy": up, "sell": 1 - up, "hold": 0.0},
            up=up,
            latency_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=len(state.recent_mids) // 4,
            reason=f"mom20={r.get('last20', 0):.2f}bps imb={state.book_imbalance:.2f} flow={flow:.2f}",
        )


_SYSTEM = (
    "You are a disciplined perpetual futures trader on Hyperliquid. You answer "
    "one question per tick with a probability, nothing else. Funding and "
    "liquidation risk matter: a position held across a high funding rate bleeds."
)

_JSON_HINT = (
    'Answer ONLY with JSON: {"up": <float 0..1>, "reason": "<short>"} where "up" is '
    "the probability the mid is higher after the horizon. Above 0.5 means long, "
    "below 0.5 means short."
)


class OpenAIModel:
    """A real LLM via any OpenAI-compatible chat completions endpoint."""

    def __init__(self, api_key: str, base_url: str, model_id: str, horizon: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.horizon = horizon
        self.name = f"openai:{model_id}"
        self._fallback = MomentumModel()

    def decide(self, state: MarketState) -> Decision:
        t0 = time.perf_counter()
        prompt = _JSON_HINT + "\n\nMarket state:\n" + json.dumps(state.to_dict(), indent=None)
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {self.api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            up = max(0.0, min(1.0, float(parsed["up"])))
            usage = body.get("usage", {}).get("prompt_tokens", 0)
            return Decision(
                action="buy" if up >= 0.5 else "sell",
                probabilities={"buy": up, "sell": 1 - up, "hold": 0.0},
                up=up,
                latency_ms=(time.perf_counter() - t0) * 1000,
                input_tokens=int(usage),
                reason=str(parsed.get("reason", ""))[:120],
            )
        except Exception as exc:
            log.warning("LLM call failed (%s); falling back to momentum", exc)
            d = self._fallback.decide(state)
            d.reason = f"fallback: {exc}"[:120]
            return d


def create_model(cfg) -> Model:
    if cfg.model == "openai" and cfg.openai_api_key:
        return OpenAIModel(cfg.openai_api_key, cfg.openai_base_url, cfg.model_id, cfg.horizon)
    return MomentumModel()
