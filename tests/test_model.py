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


def _serve_sequence(statuses, retry_after=None):
    """Serve a sequence of HTTP statuses; 200 returns a valid JSON decision."""
    class Handler(BaseHTTPRequestHandler):
        n = 0

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            code = statuses[min(Handler.n, len(statuses) - 1)]
            Handler.n += 1
            if code == 200:
                body = json.dumps({"choices": [{"message": {
                    "content": '{"up": 0.66, "reason": "recovered"}'}}],
                    "usage": {"prompt_tokens": 5}}).encode()
            else:
                body = b'{"error":{"message":"rate limited","code":429}}'
            self.send_response(code)
            if retry_after is not None:
                self.send_header("retry-after", str(retry_after))
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}", httpd


def test_retries_429_then_succeeds():
    base, httpd = _serve_sequence([429, 429, 200], retry_after=0)
    try:
        m = OpenAIModel("k", base, "x:free", horizon=60, json_mode=False,
                        max_retries=2, retry_base_s=0.0)
        d = m.decide(_state())
        assert d.reason == "recovered" and abs(d.up - 0.66) < 1e-9
        assert m.last_status == 200
    finally:
        httpd.shutdown()


def test_persistent_429_falls_back_with_a_clear_reason():
    base, httpd = _serve_sequence([429], retry_after=0)
    try:
        m = OpenAIModel("k", base, "x:free", horizon=60, json_mode=False,
                        max_retries=2, retry_base_s=0.0)
        d = m.decide(_state())
        assert d.reason == "fallback: rate limited (HTTP 429)"
        assert m.last_status == 429
    finally:
        httpd.shutdown()


def test_non_retryable_status_does_not_retry():
    """A 400 (e.g. response_format unsupported) must fail immediately."""
    base, httpd = _serve_sequence([400, 200])
    try:
        m = OpenAIModel("k", base, "x:free", horizon=60, json_mode=True,
                        max_retries=5, retry_base_s=0.0)
        d = m.decide(_state())
        assert d.reason.startswith("fallback:")
        # Never reached the 200: the two entries after 400 were not consumed.
        assert httpd.RequestHandlerClass.n == 1
    finally:
        httpd.shutdown()


def test_retry_after_header_is_honoured_and_capped():
    from hlperp.model import _retry_after

    class H(dict):
        pass

    assert _retry_after(H({"retry-after": "3"}), 0, 0.5) == 3.0
    assert _retry_after(H({"retry-after": "9999"}), 0, 0.5) == 30.0
    # No header: exponential on the base.
    assert _retry_after(H(), 0, 0.5) == 0.5
    assert _retry_after(H(), 2, 0.5) == 2.0
    assert _retry_after(None, 0, 1.0) == 1.0


def test_llm_check_exit_codes(monkeypatch, capsys):
    """llm-check must fail loudly when the endpoint is unusable."""
    from hlperp.cli import cmd_llm_check
    from hlperp.config import load_config

    # momentum needs no endpoint, so it is always "fine".
    monkeypatch.setenv("HL_MODEL", "momentum")
    assert cmd_llm_check(load_config(), 1) == 0

    # openai without a key is a configuration error.
    monkeypatch.setenv("HL_MODEL", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cmd_llm_check(load_config(), 1) == 2

    # an unreachable endpoint means every call falls back: not ok.
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    assert cmd_llm_check(load_config(), 1) == 1
    assert "every call failed" in capsys.readouterr().out


# -- funding units, the floor, and the two funding regimes --------------------

def test_funding_apr_is_a_fraction_and_display_scales_it():
    """funding_apr is a fraction, so a display must multiply by 100.

    Rendering the fraction with a '%' suffix printed 0.1% for a 10.95% rate, off by
    100x, at every display site.
    """
    from hlperp.types import FUNDING_FLOOR_APR, funding_apr_pct

    assert abs(FUNDING_FLOOR_APR - 0.1095) < 1e-9
    assert abs(funding_apr_pct(FUNDING_FLOOR_APR) - 10.95) < 1e-9
    # The live venue reads exactly the floor, so this is not a hypothetical value.
    assert abs(funding_apr_pct(0.0000125 * 24 * 365) - 10.95) < 1e-6


def _funding_state(hourly: float):
    from hlperp.types import MarketState

    return MarketState(
        coin="BTC", ts=0, mid=100.0, mark_px=100.0, oracle_px=100.0, spread_bps=1.0,
        funding_hourly=hourly, funding_apr=hourly * 24 * 365, open_interest=1.0,
        day_ntl_vlm=1e6, book_imbalance=0.0, depth_usd={},
        returns_bps={"last1": 0.0, "last5": 0.0, "last20": 0.0, "last100": 0.0},
        recent_mids="100", trades={"cvd_ratio": 0.0}, recent_trades=[],
        position_side="flat", position_size=0.0, unrealized_pnl=0.0,
        account_value=10_000.0, allowed={"buy": True, "sell": True},
    )


def test_funding_at_the_floor_is_not_a_directional_signal():
    """At the floor funding is a mechanical constant, so it must not pick a side.

    The old term was a constant long bias even with a perfectly flat market, which is
    wrong twice over: it fired at the floor, and its magnitude was 100x off.
    """
    from hlperp.model import _funding_carry

    assert abs(_funding_carry(_funding_state(0.0001 / 8))) < 1e-12


def test_funding_above_the_floor_leans_against_the_crowded_side():
    from hlperp.model import _funding_carry

    crowded_long = _funding_carry(_funding_state(0.0003))
    crowded_short = _funding_carry(_funding_state(-0.0002))
    assert crowded_long < 0  # longs paying up, so the tilt is short
    assert crowded_short > 0  # shorts paying up, so the tilt is long


def test_momentum_holds_when_the_edge_does_not_cover_costs():
    """A quiet book with funding at the floor is not worth a round trip."""
    from hlperp.model import MomentumModel

    d = MomentumModel(min_signal=0.3).decide(_funding_state(0.0001 / 8))
    assert d.action == "hold"
    assert d.probabilities["hold"] == 1.0
    assert "hold" in d.reason


def test_momentum_trades_when_the_signal_is_real():
    from hlperp.model import MomentumModel

    state = _funding_state(0.0001 / 8)
    state.returns_bps = {"last1": 0.0, "last5": 0.0, "last20": 60.0, "last100": 60.0}
    d = MomentumModel(min_signal=0.3).decide(state)
    assert d.action == "buy"
    assert d.probabilities["hold"] == 0.0


def test_hold_signal_zero_disables_the_band():
    from hlperp.model import MomentumModel

    d = MomentumModel(min_signal=0.0).decide(_funding_state(0.0001 / 8))
    assert d.action in ("buy", "sell")


def test_openai_model_honours_an_explicit_hold():
    """An LLM that asks for a hold must not be turned into a trade."""
    base, httpd = _serve({"choices": [{"message": {
        "content": '{"up": 0.8, "action": "hold", "reason": "edge too small"}'}}]})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.action == "hold"
        assert d.probabilities["hold"] == 1.0
        assert not d.reason.startswith("fallback:")
    finally:
        httpd.shutdown()


def test_openai_model_ignores_an_unreadable_action_and_still_trades():
    """A garbled action must not silently become a hold, which would stop trading."""
    base, httpd = _serve({"choices": [{"message": {
        "content": '{"up": 0.9, "action": "HOLD!!", "reason": "?"}'}}]})
    try:
        m = OpenAIModel("k", base, "some:free", horizon=60)
        d = m.decide(_state())
        assert d.action == "buy"
        assert d.probabilities["hold"] == 0.0
    finally:
        httpd.shutdown()


def test_momentum_fallback_still_answers_on_a_bad_endpoint():
    """The deterministic fallback must keep the hold band, not bypass it."""
    from hlperp.model import MomentumModel

    d = MomentumModel(min_signal=0.3).decide(_funding_state(0.0001 / 8))
    assert d.action == "hold"

