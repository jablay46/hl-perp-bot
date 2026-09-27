"""Command line entry point.

    python -m hlperp.cli paper     # real data, simulated fills (default)
    python -m hlperp.cli live      # real orders, requires HL_ALLOW_LIVE=true
    python -m hlperp.cli doctor    # connectivity and configuration report
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
        broker = LiveBroker(exchange, market.asset_index)
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
        st = Account(cfg.network, cfg.account_address).state(cfg.coin)
        print(f"account      value={st.account_value} withdrawable={st.withdrawable} "
              f"position={st.position.side if st.position else 'flat'}")
    else:
        print("account      (none configured, paper equity used)")
    print("result       " + ("OK" if ok else "PROBLEMS FOUND"))
    return 0 if ok else 1


def cmd_run(cfg, mode_override: str | None, seconds: float | None) -> int:
    if mode_override and mode_override != cfg.mode:
        # Re-validate with the requested mode by re-loading env is overkill; the
        # interlock is enforced here explicitly.
        if mode_override == "live" and not cfg.allow_live:
            print("Refusing live: set HL_ALLOW_LIVE=true", file=sys.stderr)
            return 2
        object.__setattr__(cfg, "mode", mode_override)

    market, account, broker, model = _build(cfg)
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


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="hl-perp-bot")
    parser.add_argument("command", choices=["paper", "live", "doctor"], nargs="?", default="paper")
    parser.add_argument("--seconds", type=float, default=None, help="run for this long then exit")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return cmd_doctor(cfg)
    override = "live" if args.command == "live" else "paper"
    return cmd_run(cfg, override, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
