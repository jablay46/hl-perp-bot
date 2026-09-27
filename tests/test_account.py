"""Account balance helpers, with the exchange Info object faked out."""

from hlperp.account import Account


class FakeInfo:
    def __init__(self, user_state=None, spot_state=None, ledger=None):
        self._user = user_state or {}
        self._spot = spot_state or {}
        self._ledger = ledger or []

    def user_state(self, address):
        return self._user

    def spot_user_state(self, address):
        return self._spot

    def user_non_funding_ledger_updates(self, address, start):
        return [e for e in self._ledger if e["time"] >= start]


def _account(info):
    a = Account.__new__(Account)  # skip __init__: no network
    a.info = info
    a.address = "0xtest"
    a.paper_equity = 10_000.0
    return a


def test_perp_and_spot_balances_are_separate():
    info = FakeInfo(
        user_state={"withdrawable": "0.0", "marginSummary": {"accountValue": "0.0"}},
        spot_state={"balances": [{"coin": "USDC", "total": "12.5"}]},
    )
    acct = _account(info)
    # The whole point: funds can exist on spot while perp collateral is zero.
    assert acct.perp_usdc() == 0.0
    assert acct.spot_usdc() == 12.5


def test_spot_balance_zero_when_usdc_absent():
    info = FakeInfo(spot_state={"balances": [{"coin": "UETH", "total": "1.0"}]})
    acct = _account(info)
    assert acct.spot_usdc() == 0.0


def test_recent_ledger_uses_a_relative_window():
    import time

    now = int(time.time() * 1000)
    info = FakeInfo(ledger=[
        {"time": now - 2 * 3_600_000, "delta": {"type": "recent"}},
        {"time": now - 48 * 3_600_000, "delta": {"type": "old"}},
    ])
    acct = _account(info)
    # A 24h window keeps the recent entry and drops the 48h-old one.
    rows = acct.recent_ledger(24 * 3_600_000)
    assert [e["delta"]["type"] for e in rows] == ["recent"]
