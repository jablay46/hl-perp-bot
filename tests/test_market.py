"""Market-data helpers that must not need a socket or network to test."""

from hlperp.market import liveness_action, MarketData

PING = 20.0
STALE = 45.0


def test_liveness_recent_message_is_none():
    assert liveness_action(now=1000.0, last_msg=999.0, ping_sent=0.0,
                           ping_after=PING, stale_after=STALE) == "none"


def test_liveness_silence_before_ping_threshold_is_none():
    # 10s of silence is under PING_AFTER_S: nothing to do yet.
    assert liveness_action(now=1010.0, last_msg=1000.0, ping_sent=0.0,
                           ping_after=PING, stale_after=STALE) == "none"


def test_liveness_pings_after_quiet_period():
    # 25s silent, never pinged: send a keepalive.
    assert liveness_action(now=1025.0, last_msg=1000.0, ping_sent=0.0,
                           ping_after=PING, stale_after=STALE) == "ping"


def test_liveness_does_not_spam_pings():
    # 25s silent, but we pinged 5s ago: hold off until PING_AFTER_S has elapsed.
    assert liveness_action(now=1025.0, last_msg=1000.0, ping_sent=1020.0,
                           ping_after=PING, stale_after=STALE) == "none"


def test_liveness_pings_again_after_interval():
    assert liveness_action(now=1045.0, last_msg=1000.0, ping_sent=1020.0,
                           ping_after=PING, stale_after=STALE) == "ping"


def test_liveness_stale_wins_over_ping():
    # Half-open socket: long silence must reconnect, not just keep pinging.
    assert liveness_action(now=1050.0, last_msg=1000.0, ping_sent=1040.0,
                           ping_after=PING, stale_after=STALE) == "stale"


def test_liveness_stale_boundary_is_exclusive():
    assert liveness_action(now=1000.0 + STALE, last_msg=1000.0, ping_sent=0.0,
                           ping_after=PING, stale_after=STALE) == "ping"
    assert liveness_action(now=1000.0 + STALE + 0.01, last_msg=1000.0, ping_sent=0.0,
                           ping_after=PING, stale_after=STALE) == "stale"


def test_marketdata_thresholds_are_ordered():
    # A stale threshold at or below the ping threshold would reconnect before it
    # ever pinged, making the keepalive dead code.
    assert MarketData.STALE_AFTER_S > MarketData.PING_AFTER_S
