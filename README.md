# hl-perp-bot

An AI-driven **perpetual futures** trading bot for [Hyperliquid](https://hyperliquid.xyz),
rebuilt from the architecture of [`jarrodwatts/jev-trader`](https://github.com/jarrodwatts/jev-trader)
(a Monad/Kuru spot demo) into a system that speaks perpetuals: leverage, margin,
liquidation distance, funding and reduce-only orders.

> **This is research software.** It trades real money only if you explicitly arm
> it. Paper mode is the default, testnet is the default network, and live trading
> needs three separate switches. Read [Going live](#going-live) before you touch
> a key.

---

## Why perps fit this shape better than the original

`jev-trader` was built for an on-chain **order book** (Kuru). Hyperliquid is also
an order book, so the core idea transfers: one decision per tick, a post-only
(A LO) quote placed inside the touch, honest P&L. What does **not** transfer is
the economics. A perp position is leveraged, pays funding every hour, and can be
liquidated. Those three facts are absent from the original and drive most of the
code here.

| Concern | `jev-trader` (Monad/Kuru spot) | this bot (Hyperliquid perps) |
| --- | --- | --- |
| Market access | raw `eth_call` to `getL2Book()` | `Info` REST + WebSocket `l2Book`/`trades`/`activeAssetCtx` |
| Execution | `eth_sendRawTransaction` per block | SDK `Exchange` actions (order/cancel/leverage) |
| Auth | `PRIVATE_KEY` | **agent/API wallet** (trades, cannot withdraw) + nonce |
| Tick | ~300 ms block | event-driven; decision interval configurable |
| Cost model | gas on the limit | maker/taker fees on notional |
| Capital | spot inventory | leverage, margin, **liquidation price** |
| Carry | none | **hourly funding** (capped 4%/hr) |
| Sizing | fixed order size | risk %, notional cap, liq-distance floor |
| Safety | position cap | kill-switch on drawdown, halt cancels all |

The honest read: the original is a latency showcase. This is an attempt at a
skeleton that could carry a real strategy, with the parts that actually kill
perp accounts (funding and liquidation) modelled rather than ignored.

---

## Architecture

```
                 ┌─────────────────────────────────────────────────────────┐
   WebSocket     │ market.py        Info + WS: book, trades, asset ctx    │
   + Info REST ─▶ │                                                     │
                 └───────────────┬─────────────────────────────────────────┘
                                 ▼
   account.py  ── clearinghouseState ──▶  AccountState (equity, position, liq px)
                                 │
                                 ▼
   strategy.py ── build MarketState (funding, OI, depth, returns, taker flow)
                                 │
                                 ▼
   model.py   ── momentum | OpenAI ──▶  Decision {action, P(up), latency}
                                 │
                                 ▼
   risk.py    ── size(), liq-distance floor, drawdown kill-switch ──▶ allow/deny
                                 │
                                 ▼
   execution.py ── LiveBroker (Exchange, ALO/IOC) | PaperBroker (simulated fill)
                                 │
                                 ▼
   trader.py  ── per-tick loop, fees + funding P&L ──▶ server.py (SSE dashboard)
```

### Files

| File | Responsibility |
| --- | --- |
| `config.py` | env config and the live-trading interlock |
| `types.py` | typed domain objects (`MarketState`, `Signal`, `Decision`, …) |
| `rounding.py` | Hyperliquid tick/lot rules (5 sig figs, `szDecimals`) |
| `market.py` | market data adapter: Info snapshots + WebSocket streams |
| `account.py` | positions, margin, fees, funding history |
| `model.py` | `MomentumModel` stand-in and `OpenAIModel` (typed JSON answer) |
| `strategy.py` | state assembly + ALO/IOC quote construction |
| `risk.py` | sizing, liquidation maths, drawdown kill-switch |
| `execution.py` | `LiveBroker` and `PaperBroker` behind one interface |
| `trader.py` | the loop and P&L (fees, funding, realized, unrealized) |
| `backtest.py` | offline candle + funding replay through the same strategy |
| `server.py` | stdlib dashboard: snapshot, `/events` SSE, single page |
| `cli.py` | `paper` / `live` / `doctor` / `backtest` entry points |

---

## How it works, one tick at a time

1. **Read** the book and asset context from the WebSocket; read the account from
   the Info endpoint.
2. **Assemble** a compact `MarketState`: mid, spread, funding (hourly and APR),
   open interest, book imbalance, depth by band, short-horizon returns, taker
   flow (CVD), and the current position.
3. **Ask** the model for `P(mid higher after horizon)`. Above 0.5 is long.
4. **Size** through the risk engine (see below). A denial is recorded, not
   traded around.
5. **Quote.** With `HL_ORDER_TIF=ALO`, place a post-only limit that joins the near
   touch on the model's side (best bid for a buy, best ask for a sell) and cancel
   the previous one, so the bot earns the maker fee instead of paying the taker
   fee and still rests when the spread is a single tick. With `IOC`, cross the
   touch.
6. **Account.** Maker fills arrive from the exchange (live) or from the trade
   tape (paper). Fees are charged per fill; funding accrues on open notional at
   the hourly rate. Both show up in the dashboard P&L.

### The risk engine

This is the layer the original has no equivalent of. It runs *between* the model
and the exchange.

- **Sizing is a target, not a per-tick order.** The target notional is
  `equity * risk_pct * leverage`, capped by `max_notional_pct` of equity, and the
  order is sized as the *delta* from the position already held. A repeat tick
  with an unchanged signal therefore does not stack a new position, and a flip
  from long to short sizes the full distance through flat.
- **Liquidation distance.** Uses the Hyperliquid formula:

  ```
  liq_price = price - side * margin_available / size / (1 - l * side)
      l   = 1 / maintenance_leverage
      side = +1 long, -1 short
  ```

  For a fixed leverage the distance collapses to `(1/lev) / (1 - 1/L)`, which is
  what the engine checks against `min_liq_distance_pct`. `L` is
  `HL_MAINTENANCE_LEVERAGE`, deliberately separate from the exchange's *max*
  leverage — a 10x position is only about 5.6% from liquidation at 10x
  maintenance, not the 10% you would get by assuming `L = max_leverage`.
- **Kill-switch.** Peak-to-trough drawdown past `max_drawdown_pct` halts trading
  and cancels every resting order.

### Take-profit / stop-loss

With `HL_TP_SL=true`, once a position reaches its target size the trader arms a
reduce-only bracket around the mark. Live mode sends both legs as one grouped
`positionTpsl` request, so when one fires the venue cancels the sibling instead
of leaving a naked stop behind. The stop trigger is clamped to stay strictly
inside the liquidation price, because a stop beyond liquidation would never
fire. Paper mode simulates the same triggers against the trade tape.

### Reconciling live fills

The order acknowledgement is not the source of truth for live P&L. Each tick in
live mode polls `user_fills_by_time` and folds unseen fills into the totals,
keyed by `(time, oid, hash)` so a repeated poll cannot double count. That is what
supplies the real fee (net of any maker rebate) and the realised closed PnL.

### Backtesting

`python -m hlperp backtest --interval 1m --hours 24` replays candles and funding
history through the same model, risk engine and fee model, entirely offline
apart from the one historical fetch. A maker order quoted on one candle is
checked against the next candle's range, funding is charged at the historical
hourly rate, and the report includes return, max drawdown and win rate. It is
deliberately conservative: no fill is assumed better than the quote.

### Funding, accounted honestly

Hyperliquid pays funding **hourly** at `funding = premium + clamp(interest −
premium, ±0.05%)`, capped at 4%/hour, in the direction long-pays-short when
positive. A 0.01%/hr rate annualises to roughly 88%, so a bot that holds across
funding without counting it is flying blind. Here it accrues on
`position_value * funding_hourly * sign * elapsed_hours` and appears as its own
line in the dashboard, next to fees.

---

## Install and run

Requires Python 3.10+.

```bash
cd hl-perp-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
python -m hlperp doctor        # connectivity + config report, no keys needed
python -m hlperp paper         # real testnet data, simulated fills
```

Dashboard: <http://localhost:8787> (`/snapshot` for JSON, `/events` for SSE).

```bash
python -m hlperp paper --seconds 60    # bounded run, useful for CI/smoke
```

### Configuration that matters

| Env | Default | Meaning |
| --- | --- | --- |
| `HL_NETWORK` | `testnet` | `testnet` or `mainnet` |
| `HL_MODE` | `paper` | `paper` (simulated) or `live` |
| `HL_COIN` | `BTC` | market |
| `HL_LEVERAGE` | `5` | requested leverage |
| `HL_ORDER_TIF` | `ALO` | `ALO` post-only maker, `IOC` taker, `GTC` resting |
| `HL_SPREAD_BPS` | `2.0` | how far inside the touch to quote |
| `HL_RISK_PCT` | `0.01` | fraction of equity risked per trade |
| `HL_MAINTENANCE_LEVERAGE` | `2.0` | maintenance-margin assumption for liq distance |
| `HL_MAX_DRAWDOWN_PCT` | `10` | kill-switch threshold |
| `HL_MIN_LIQ_DISTANCE_PCT` | `15` | refuse orders with liquidation closer than this |
| `HL_TP_SL` | `false` | arm a reduce-only take-profit / stop-loss bracket |
| `HL_TP_PCT` / `HL_SL_PCT` | `0.02` / `0.01` | bracket distances from the mark |
| `HL_INTERVAL` | `2.0` | seconds between decisions |
| `HL_MODEL` | `momentum` | `momentum` stand-in or `openai` |

---

## Going live

Live mode is behind three independent switches, all required:

1. `HL_MODE=live`
2. `HL_ALLOW_LIVE=true` — the explicit acknowledgement interlock.
3. A signing key (`HL_AGENT_PRIVATE_KEY`) and `HL_ACCOUNT_ADDRESS`.
   On mainnet the key **must** be an agent/API wallet, not a main private key
   (enforced in `config.py`).

Recommended sequence:

1. `doctor` against testnet.
2. `paper` for a while; watch fills, fees and funding on the dashboard.
3. Live on **testnet**, with a testnet agent wallet, funded from the faucet.
4. Only then mainnet, with the smallest size that is meaningful.

An agent wallet can place and cancel orders but **cannot withdraw**, which is the
single most important safety property here. Never reuse an agent address after
deregistering it — Hyperliquid prunes its nonce state and old signatures can be
replayed.

### A deposit funds spot, not perp

This trips up nearly every first live run. A USDC deposit credits your **spot**
balance; perp collateral is a separate balance and does not move on its own. An
account can therefore hold funds and still refuse every order with
`accountValue=0`. Move it across explicitly:

```bash
python -m hlperp doctor              # shows both balances and the ledger
python -m hlperp funding             # USDC spot -> perp, full spot balance
python -m hlperp funding --amount 5  # or a specific amount
python -m hlperp funding --to-spot   # perp -> spot, to withdraw
```

`doctor` is the quickest way to tell the two cases apart: it prints
`account value=...`, `spot USDC ...`, and the last few ledger entries (including
transfer fees), and it tells you the fix when the perp balance is zero but spot
is not.

### Hyperliquid constraints worth knowing

- **Nonces** are per signer; the 100 highest are kept and must fall within
  `(T − 2 days, T + 1 day)`.
- **Rate limits**: REST aggregates to 1200 weight/minute per IP; `l2Book`,
  `allMids`, `clearinghouseState` cost more; 10 WS connections, 1000 subs.
- **Tick/lot**: prices carry 5 significant figures and at most `6 − szDecimals`
  decimals; sizes round to `szDecimals`.
- **Order types**: `ALO` (post-only), `IOC`, `GTC`, plus trigger/tpsl support in
  the SDK (`bulk_orders(grouping="positionTpsl")`), which the trader now uses for
  brackets.

---

## Tests

```bash
PYTHONPATH=src python -m pytest -q
```

37 tests cover tick/lot rounding, the config interlock, liquidation maths, delta
sizing and the kill-switch, paper matching (partial fills, TP/SL triggers) and
fee accounting, live-fill reconciliation idempotence, the offline backtester, and
a deterministic end-to-end loop (decision → risk → order → fill → P&L).

---

## Roadmap (what a real strategy still needs)

- Walk-forward validation and parameter sweeps on top of the backtester.
- A funding/basis model, not just a momentum stand-in.
- Per-asset margin tiers rather than a single maintenance-leverage approximation.
- Reconnect reconciliation against `openOrders`/`clearinghouseState`.
- Multi-market, isolated margin per market.

---

## References

- Hyperliquid docs — Info/Exchange, WebSocket, signing, nonces & API wallets,
  funding, margin tiers, liquidations, rate limits.
- Official Python SDK: [`hyperliquid-dex/hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (v0.24.0).
- Reference bot: [`chainstacklabs/hyperliquid-trading-bot`](https://github.com/chainstacklabs/hyperliquid-trading-bot)
  (grid strategy, risk rules, grouped TPSL).
- Market-making framework: [`hummingbot/hummingbot`](https://github.com/hummingbot/hummingbot).
- Original inspiration: [`jarrodwatts/jev-trader`](https://github.com/jarrodwatts/jev-trader).

## Disclaimer

Not financial advice. Perpetual futures with leverage can lose more than you
deposit. The default model is a deterministic stand-in, not a profitable
strategy. Test on testnet first.
