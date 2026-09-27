"""Command line entry point.

    python -m hlperp paper     # real data, simulated fills (default)
    python -m hlperp live      # real orders, requires HL_ALLOW_LIVE=true
    python -m hlperp doctor    # connectivity, balances and configuration report
    python -m hlperp backtest  # offline replay of candles and funding
    python -m hlperp funding   # move USDC spot <-> perp (deposits land in spot)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .account import Account
from .config import ConfigError, load_config
from .execution import LiveBroker, PaperBroker
from .market import MarketData
from .model import create_model
from .server import Server
from .trader import Trader


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build(cfg):
    market = MarketData(cfg.network, cfg.coin)
    account = Account(cfg.network, cfg.account_address)
    model = create_model(cfg)

    if cfg.is_live:
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange

        wallet = EthAccount.from_key(cfg.signing_key)
        exchange = Exchange(wallet, base_url=cfg.base_url, account_address=cfg.account_address)
        broker = LiveBroker(exchange, market.asset_index, market.sz_decimals)
        logging.info("LIVE mode: signer=%s account=%s agent=%s",
                     wallet.address, cfg.account_address, cfg.is_agent_wallet)
    else:
        broker = PaperBroker(cfg.coin)
        logging.info("PAPER mode: real data, simulated fills, no keys")

    return market, account, broker, model


def cmd_doctor(cfg) -> int:
    print(f"network      {cfg.network}")
    print(f"mode         {cfg.mode}   (live={cfg.is_live})")
    print(f"base_url     {cfg.base_url}")
    print(f"coin         {cfg.coin}")
    ok = True
    try:
        market = MarketData(cfg.network, cfg.coin)
        book = market.fetch_book()
        ctx = market.fetch_ctx()
        print(f"asset_index  {market.asset_index}  szDecimals={market.sz_decimals}  "
              f"maxLeverage={market.max_leverage}")
        print(f"book         bid={book.best_bid} ask={book.best_ask} mid={book.mid} "
              f"spread={book.spread_bps:.2f}bps")
        print(f"ctx          mark={ctx.mark_px} oracle={ctx.oracle_px} "
              f"funding_hourly={ctx.funding_hourly} funding_apr={ctx.funding_apr:.2f}%")
    except Exception as exc:
        ok = False
        print(f"market data  FAILED: {exc}")
    if cfg.account_address:
        acct = Account(cfg.network, cfg.account_address)
        st = acct.state(cfg.coin)
        print(f"account      value={st.account_value} withdrawable={st.withdrawable} "
              f"position={st.position.side if st.position else 'flat'}")
        spot = acct.spot_usdc()
        print(f"spot USDC    {spot}")
        role = acct.hypercore_mainnet_role()
        if role is not None:
            print(f"mainnet role {role}")
            if role == "missing" and cfg.network == "testnet":
                print("             -> this address has NO HyperCore mainnet state. A")
                print("                CCTP-forwarded testnet deposit fails silently and")
                print("                strands the USDC on HyperEVM. Deposit from the UI")
                print("                (CoreDepositWallet) instead. See README.")
        if st.account_value <= 0 and spot <= 0:
            print("             -> no perp balance and no spot USDC: nothing to trade")
        elif st.account_value <= 0 and spot > 0:
            print("             -> USDC is on the SPOT balance, not perp. Run:")
            print(f"                python -m hlperp funding --amount {spot:.6f}")
        for e in acct.recent_ledger(24 * 3_600_000)[-5:]:
            d = e.get("delta", {})
            print(f"ledger       {d.get('type')} token={d.get('token')} "
                  f"amount={d.get('amount')} usdcValue={d.get('usdcValue')} fee={d.get('fee')}")
    else:
        print("account      (none configured, paper equity used)")
    print("result       " + ("OK" if ok else "PROBLEMS FOUND"))
    return 0 if ok else 1


def cmd_funding(cfg, amount: float | None, to_perp: bool) -> int:
    """Move USDC between the spot and perp balances of the same account.

    A Hyperliquid deposit credits the *spot* balance; it does not become perp
    collateral on its own, which is why an account can show funds and still be
    unable to place an order. This is the missing step.
    """
    if not cfg.signing_key:
        print("funding needs a key (HL_AGENT_PRIVATE_KEY or HL_PRIVATE_KEY)", file=sys.stderr)
        return 2
    from eth_account import Account as EthAccount
    from hyperliquid.exchange import Exchange

    if amount is None:
        if to_perp:
            amount = Account(cfg.network, cfg.account_address).spot_usdc()
        else:
            amount = Account(cfg.network, cfg.account_address).perp_usdc()
    if amount <= 0:
        print(f"nothing to transfer (amount={amount})", file=sys.stderr)
        return 1

    wallet = EthAccount.from_key(cfg.signing_key)
    exchange = Exchange(wallet, base_url=cfg.base_url, account_address=cfg.account_address)
    direction = "spot -> perp" if to_perp else "perp -> spot"
    print(f"transferring {amount} USDC {direction} for {cfg.account_address}")
    res = exchange.usd_class_transfer(amount, to_perp)
    print(res)
    if str(res.get("status")) != "ok":
        return 1
    acct = Account(cfg.network, cfg.account_address)
    print(f"after: perp={acct.perp_usdc()} spot={acct.spot_usdc()}")
    return 0


def cmd_run(cfg, mode_override: str | None, seconds: float | None) -> int:
    if mode_override and mode_override != cfg.mode:
        # Re-validate with the requested mode by re-loading env is overkill; the
        # interlock is enforced here explicitly.
        if mode_override == "live" and not cfg.allow_live:
            print("Refusing live: set HL_ALLOW_LIVE=true", file=sys.stderr)
            return 2
        object.__setattr__(cfg, "mode", mode_override)

    market, account, broker, model = _build(cfg)
    if cfg.is_live:
        # An unfunded account produces rejects that look like a broken strategy.
        # Say so once, clearly, before the loop starts.
        try:
            start_state = account.state(cfg.coin)
            if start_state.account_value <= 0:
                print(
                    f"WARNING: live account {cfg.account_address} has accountValue="
                    f"{start_state.account_value}. No order can be placed. Fund it first "
                    f"(testnet faucet) or the strategy will only log rejects.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"WARNING: could not read account state: {exc}", file=sys.stderr)
    server = Server(
        cfg.port,
        meta={
            "coin": cfg.coin, "mode": cfg.mode, "network": cfg.network,
            "model": model.name, "live": cfg.is_live,
        },
        history_getter=lambda: trader.history,
    )

    trader = Trader(cfg, market, account, broker, model, on_event=server.event,
                    interval_s=cfg.interval_s)
    try:
        server.start()
    except OSError as exc:
        print(f"dashboard port {cfg.port} unavailable ({exc}); continuing without it", file=sys.stderr)

    if isinstance(broker, PaperBroker):
        market.start(on_trade=lambda t: trader.on_print(t.coin, t.px, t.ts, t.side == "buy"))
    else:
        market.start()

    if cfg.is_live:
        broker.set_leverage(cfg.coin, cfg.leverage, cfg.margin_mode == "cross")

    start = time.time()
    try:
        while not trader._stop:
            trader.tick()
            for f in getattr(broker, "drain_fills", lambda: [])():
                trader._apply_fill(f)
                server.fill(f)
            if seconds and time.time() - start >= seconds:
                break
            time.sleep(cfg.interval_s)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        trader.stop()
        market.stop()
    return 0


def cmd_backtest(cfg, interval: str, hours: float) -> int:
    import json

    from .backtest import Backtester

    bt = Backtester(cfg)
    try:
        result = bt.run(interval=interval, lookback_ms=int(hours * 3_600_000))
    except Exception as exc:
        print(f"backtest failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="hl-perp-bot")
    parser.add_argument("command",
                        choices=["paper", "live", "doctor", "backtest", "funding"],
                        nargs="?", default="paper")
    parser.add_argument("--seconds", type=float, default=None, help="run for this long then exit")
    parser.add_argument("--interval", default="1m", help="backtest candle interval")
    parser.add_argument("--hours", type=float, default=6.0, help="backtest lookback in hours")
    parser.add_argument("--amount", type=float, default=None,
                        help="funding: USDC amount, defaults to the full source balance")
    parser.add_argument("--to-spot", action="store_true",
                        help="funding: move perp -> spot instead of spot -> perp")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "backtest":
        return cmd_backtest(cfg, args.interval, args.hours)
    if args.command == "funding":
        return cmd_funding(cfg, args.amount, to_perp=not args.to_spot)
    override = "live" if args.command == "live" else "paper"
    return cmd_run(cfg, override, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
