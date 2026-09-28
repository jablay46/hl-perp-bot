"""LLM model layer: lenient JSON parsing and the OpenAI-compatible client.

The client is exercised against a real local HTTP server rather than a mocked
urllib, so the request/response path that runs in production is the one tested.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hlperp.model import MomentumModel, OpenAIModel, _extract_json


@pytest.mark.parametrize("text,expected_up", [
    ('{"up": 0.7, "reason": "trend"}', 0.7),
    ('```json\n{"up": 0.2, "reason": "fade"}\n```', 0.2),
    ('Sure! Here is my answer:\n{"up": 0.55, "reason": "ok"}\nHope that helps.', 0.55),
])
def test_extract_json_tolerates_wrapping(text, expected_up):
    assert _extract_json(text)["up"] == expected_up


def test_extract_json_rejects_non_json():
    with pytest.raises(ValueError):
        _extract_json("I think the market will go up.")
    with pytest.raises(ValueError):
        _extract_json("")


def _serve(response: dict, status: int = 200):
    """Start a one-shot OpenAI-shaped endpoint and return (base_url, stop)."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.server.last_body = json.loads(self.rfile.read(length) or b"{}")
            payload = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # keep test output clean
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{httpd.server_port}", httpd


def _state():
    from hlperp.types import MarketState

    return MarketState(
        coin="BTC", ts=0, mid=100.0, mark_px=100.0, oracle_px=100.0, spread_bps=1.0,
        funding_hourly=0.0001, funding_apr=0.876, open_interest=1.0, day_ntl_vlm=1e6,
        book_imbalance=0.1, depth_usd={}, returns_bps={"last20": 1.0},
        recent_mids="100 101", trades={"cvd_ratio": 0.2}, recent_trades=[],
        position_side="flat", position_size=0.0, unrealized_pnl=0.0,
        account_value=10_000.0, allowed={"buy": True, "sell": True},
    )


def test_openai_model_parses_a_clean_reply():
    base, httpd = _serve({"choices": [{"message": {"content": '{"up": 0.8, "reason": "bid"}'}}],
                          "usage": {"prompt_tokens": 42}})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.action == "buy" and abs(d.up - 0.8) < 1e-9
        assert d.input_tokens == 42 and d.reason == "bid"
        # json_mode defaults on, so response_format must have been sent.
        assert httpd.last_body["response_format"] == {"type": "json_object"}
    finally:
        httpd.shutdown()


def test_openai_model_omits_json_mode_when_disabled():
    """Most OpenRouter free models reject response_format."""
    base, httpd = _serve({"choices": [{"message": {"content": '{"up": 0.3, "reason": "x"}'}}]})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60, json_mode=False)
        d = m.decide(_state())
        assert d.action == "sell" and abs(d.up - 0.3) < 1e-9
        assert "response_format" not in httpd.last_body
    finally:
        httpd.shutdown()


def test_openai_model_uses_reasoning_field_and_fenced_json():
    """Some reasoning models put the answer in `reasoning`, wrapped in fences."""
    base, httpd = _serve({"choices": [{"message": {
        "content": None, "reasoning": '```json\n{"up": 0.6, "reason": "think"}\n```'}}]})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.action == "buy" and abs(d.up - 0.6) < 1e-9
    finally:
        httpd.shutdown()


def test_openai_model_falls_back_to_momentum_on_garbage():
    base, httpd = _serve({"choices": [{"message": {"content": "not json at all"}}]})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.reason.startswith("fallback:")
        # The fallback is the deterministic model, so it still returns a decision.
        assert d.action in ("buy", "sell")
        assert isinstance(m._fallback, MomentumModel)
    finally:
        httpd.shutdown()


def test_openai_model_falls_back_on_http_error():
    base, httpd = _serve({"error": "rate limited"}, status=429)
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.reason.startswith("fallback:")
    finally:
        httpd.shutdown()


def test_create_model_wires_openrouter_headers(monkeypatch):
    from hlperp.config import load_config
    from hlperp.model import create_model

    monkeypatch.setenv("HL_MODEL", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("HL_MODEL_ID", "google/gemma-4-31b-it:free")
    monkeypatch.setenv("HL_LLM_JSON_MODE", "false")
    cfg = load_config()
    model = create_model(cfg)
    assert model.name == "openai:google/gemma-4-31b-it:free"
    assert model.json_mode is False
    assert model.extra_headers["X-Title"] == "hl-perp-bot"
    assert "HTTP-Referer" in model.extra_headers


def test_create_model_defaults_to_momentum_without_a_key(monkeypatch):
    from hlperp.config import load_config
    from hlperp.model import create_model

    monkeypatch.setenv("HL_MODEL", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load_config()
    assert isinstance(create_model(cfg), MomentumModel)
