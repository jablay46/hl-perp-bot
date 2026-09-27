from hlperp.config import ConfigError, load_config


def test_defaults_are_paper(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("HL_"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.mode == "paper"
    assert cfg.is_live is False
    assert cfg.network == "testnet"


def test_live_requires_interlock(monkeypatch):
    monkeypatch.setenv("HL_MODE", "live")
    monkeypatch.setenv("HL_ALLOW_LIVE", "false")
    monkeypatch.setenv("HL_AGENT_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "22" * 20)
    try:
        load_config()
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "HL_ALLOW_LIVE" in str(exc)


def test_live_requires_agent_wallet_on_mainnet(monkeypatch):
    monkeypatch.setenv("HL_MODE", "live")
    monkeypatch.setenv("HL_ALLOW_LIVE", "true")
    monkeypatch.setenv("HL_NETWORK", "mainnet")
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("HL_AGENT_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "22" * 20)
    try:
        load_config()
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "agent" in str(exc).lower()


def test_leverage_bounds(monkeypatch):
    monkeypatch.setenv("HL_LEVERAGE", "50")
    monkeypatch.setenv("HL_MAX_LEVERAGE", "10")
    try:
        load_config()
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "HL_LEVERAGE" in str(exc)
