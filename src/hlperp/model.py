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
import urllib.error
import urllib.request
from typing import Optional, Protocol

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


def _extract_json(text: str) -> dict:
    """Parse the model's reply into a dict.

    Free and reasoning models routinely wrap JSON in prose or code fences even
    when asked not to, so this falls back to the outermost brace pair rather
    than failing the whole tick.
    """
    if not text:
        raise ValueError("empty completion")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(text[start:end + 1])


_RETRYABLE = {429, 500, 502, 503, 504}


def _retry_after(headers, attempt: int, base: float) -> float:
    """Seconds to wait before retrying. Honours Retry-After when present."""
    if headers is not None:
        raw = headers.get("retry-after") or headers.get("x-ratelimit-reset-requests")
        if raw:
            try:
                return min(30.0, float(raw))
            except ValueError:
                pass
    return min(30.0, base * (2 ** attempt))


class OpenAIModel:
    """A real LLM via any OpenAI-compatible chat completions endpoint.

    Works with OpenAI, OpenRouter, Together, Groq, vLLM and similar. OpenRouter
    free models are supported but vary in quality: most do not implement
    ``response_format``, so ``json_mode`` is configurable and the reply is parsed
    leniently either way.

    429 and 5xx are retried with exponential backoff (honouring ``Retry-After``)
    before the model degrades to momentum, because free tiers rate limit
    aggressively.
    """

    def __init__(self, api_key: str, base_url: str, model_id: str, horizon: int,
                 json_mode: bool = True, extra_headers: dict | None = None,
                 max_retries: int = 2, retry_base_s: float = 0.5) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.horizon = horizon
        self.json_mode = json_mode
        self.extra_headers = extra_headers or {}
        self.max_retries = max(0, max_retries)
        self.retry_base_s = retry_base_s
        self.name = f"openai:{model_id}"
        self.last_status: Optional[int] = None
        self._fallback = MomentumModel()

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
                **self.extra_headers,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            self.last_status = resp.status
            return json.loads(resp.read())

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
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                body = self._post(payload)
                message = body["choices"][0]["message"]
                content = message.get("content") or message.get("reasoning") or ""
                parsed = _extract_json(content)
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
            except urllib.error.HTTPError as exc:
                self.last_status = exc.code
                last_error = exc
                if exc.code not in _RETRYABLE or attempt == self.max_retries:
                    break
                delay = _retry_after(exc.headers, attempt, self.retry_base_s)
                log.warning("LLM HTTP %s; retry %s/%s in %.1fs",
                            exc.code, attempt + 1, self.max_retries, delay)
                time.sleep(delay)
            except Exception as exc:
                last_error = exc
                break

        status = getattr(last_error, "code", None)
        detail = f"rate limited (HTTP {status})" if status == 429 else str(last_error)
        log.warning("LLM call failed (%s); falling back to momentum", detail)
        d = self._fallback.decide(state)
        d.reason = f"fallback: {detail}"[:120]
        return d


def create_model(cfg) -> Model:
    if cfg.model == "openai" and cfg.openai_api_key:
        headers = {}
        if "openrouter.ai" in cfg.openai_base_url:
            # OpenRouter attribution headers; optional but recommended.
            headers["HTTP-Referer"] = "https://github.com/jablay46/hl-perp-bot"
            headers["X-Title"] = "hl-perp-bot"
        return OpenAIModel(
            cfg.openai_api_key, cfg.openai_base_url, cfg.model_id, cfg.horizon,
            json_mode=cfg.llm_json_mode, extra_headers=headers,
            max_retries=cfg.llm_max_retries, retry_base_s=cfg.llm_retry_base_s,
        )
    return MomentumModel()
