# AGENTS.md

Repository memory for `hl-perp-bot`, an AI perpetual-futures bot for Hyperliquid
rebuilt from the architecture of `jarrodwatts/jev-trader` (a Monad/Kuru spot demo).

## Hard rules

- **Language and stack are fixed:** Python 3.10+, official `hyperliquid-python-sdk`
  (`Info` for reads, `Exchange` for signed writes). Do not swap in another client.
- **Default is safe:** `HL_MODE=paper`, `HL_NETWORK=testnet`. Live trading needs
  `HL_MODE=live` + `HL_ALLOW_LIVE=true` + a key, and never runs without explicit
  user permission.
- **Never commit keys** or `.env`. `.env.example` documents every knob.
- **Mainnet must use an agent/API wallet**, never a main private key.

## Layout

| Path | Role |
| --- | --- |
| `src/hlperp/market.py` | Info snapshots + own WebSocket (`l2Book`/`trades`/`activeAssetCtx`); read-only |
| `src/hlperp/account.py` | account state, open orders, funding history, `fills()` via `user_fills_by_time` |
| `src/hlperp/strategy.py` | builds `MarketState`; ALO/IOC quote construction |
| `src/hlperp/model.py` | `MomentumModel` (deterministic) and `OpenAIModel` (typed JSON) |
| `src/hlperp/risk.py` | delta sizing, liquidation maths, drawdown kill-switch |
| `src/hlperp/execution.py` | `LiveBroker` and `PaperBroker` behind one interface, incl. TP/SL |
| `src/hlperp/trader.py` | the tick loop, P&L, fill reconciliation, bracket arming |
| `src/hlperp/backtest.py` | offline candle + funding replay |
| `src/hlperp/server.py` | stdlib dashboard (snapshot, `/events` SSE) |

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m hlperp doctor
PYTHONPATH=src python -m hlperp paper --seconds 30
PYTHONPATH=src python -m hlperp backtest --interval 1m --hours 6
PYTHONPATH=src python -m hlperp funding            # USDC spot -> perp
PYTHONPATH=src python -m hlperp llm-check          # validate the LLM endpoint
```

There is no test config; tests import `hlperp` from `src`, so `PYTHONPATH=src`
is required (or install the package editable).

## Design decisions worth not re-litigating

- **Risk sizing is a target, not a per-tick order.** `RiskEngine.size` returns the
  delta from the current position. A repeat tick with an unchanged signal must
  not stack; a flip must size the full distance through flat.
- **Liquidation distance is a gate, not a target.** For fixed leverage the
  distance is `(1/lev) / (1 - 1/L)` with `L = HL_MAINTENANCE_LEVERAGE` (a real
  maintenance-margin assumption, *not* the exchange max leverage). If it is under
  `HL_MIN_LIQ_DISTANCE_PCT`, the order is refused outright.
- **Hyperliquid's trade `side` is the maker side, not the aggressor.** Verified
  empirically: prints far below the best bid still report `B`. Paper matching is
  therefore price-based; do not reintroduce aggressor-side matching.
- **ALO joins the near touch** (best bid for a buy, best ask for a sell). The
  best levels are already tick-valid, so rounding cannot push a post-only order
  across. Quoting at the mid fails when the spread is a single tick.
- **Live P&L comes from `user_fills_by_time`,** keyed by `(time, oid, hash)` so
  polling is idempotent. The order acknowledgement does not carry fees or closed
  PnL.
- **Brackets are grouped `positionTpsl`**, reduce-only, armed only when position
  size changes, with the stop clamped strictly inside liquidation.
- **A deposit lands on the spot balance, not perp collateral.** An account can
  hold USDC and still refuse every order with `accountValue=0`. The fix is an
  explicit `usd_class_transfer` (`python -m hlperp funding`). `doctor` prints both
  balances and the ledger so this is visible before a live run.
- **On testnet, a raw CCTP `cctp-forward` deposit fails silently unless the
  address already exists on HyperCore mainnet** (Circle's documented testnet
  limitation). The source transaction completes, the USDC is minted on HyperEVM,
  and HyperCore stays at zero. Deposit through the UI (CoreDepositWallet) or use
  the faucet. `doctor` reports `mainnet role` to catch this.

- **`HL_MODEL=openai` is a generic OpenAI-compatible client**, so OpenRouter and
  similar work by pointing `OPENAI_BASE_URL` at them. Most OpenRouter free models
  reject `response_format`, hence the `HL_LLM_JSON_MODE` switch, and replies are
  parsed leniently (bare/fenced/prose-wrapped JSON, or a `reasoning` field).
- **LLM failure degrades silently to `MomentumModel`** and the tick reason starts
  with `fallback:`. A broken model therefore looks like a working one unless you
  watch for that prefix.
- **429/5xx are retried before falling back** (`HL_LLM_MAX_RETRIES`,
  `HL_LLM_RETRY_BASE`), honouring `Retry-After` and capped at 30s. A 429 that
  survives retries becomes `fallback: rate limited (HTTP 429)`. `OpenAIModel` is
  shared across ticks, so `last_status` holds the most recent HTTP code.

## Gotchas

- `PaperBroker` fills only a `participation` slice per print (default 0.25), so a
  large resting order fills over several prints rather than in one.
- Funding accrues hourly in the live path and per-candle in the backtester; a
  long pays when the rate is positive.
- The dashboard port is best-effort: a busy port logs a warning and the bot keeps
  running without it.

## Sizing and the venue minimum

- **Hyperliquid rejects any order below $10 notional** (`MinTradeNtl`). The real
  floor is lot-aware: the size must be a whole number of lots, so the smallest
  acceptable order is the first lot multiple reaching $10 - $10.79 for BTC at 5
  decimals, not $10. `min_order_notional` / `min_equity_for_order` compute it.
- **Sizes are floor-truncated, never rounded up** (`floor_sz`). Rounding up can
  request more than the balance or the risk budget and the venue rejects the
  whole order. `round_sz` is an alias for `floor_sz`.
- **At the defaults (`HL_RISK_PCT=0.01`, `HL_LEVERAGE=5`) equity below ~$216
  cannot place a single BTC order.** `RiskEngine.size` refuses with the equity
  needed; `cmd_run` prints the same warning before the loop starts. This figure
  holds only while `HL_MAX_NOTIONAL_PCT` does not bind: when it does, the target
  grows slower with equity and the requirement is higher (at a 1% cap it is
  ~$1079). `min_equity_for_order` takes the cap, and `cmd_run` uses the shared
  `effective_target_notional` so the warning cannot disagree with sizing.
- **The backtester resolves `sz_decimals` from metadata**, falling back to 6
  offline. A hardcoded lot finer than the asset's truncates every size to zero
  and the run reports no fills while looking healthy. A network failure logs a
  WARNING and is *not* cached, so a later success still resolves the real lot; an
  unknown coin raises `ValueError` instead of silently using the fallback.
- **`PaperBroker` enforces the same $10 minimum and resets `entry_px` on a flip.**
  A flip through zero is a new position; keeping the old entry corrupts
  unrealized PnL and every later close. A *partial* reduce is the opposite case:
  the remainder keeps its original entry and only the closed size realizes PnL.
- **Never `assert` a market invariant** (`signal_for` mid). Asserts are stripped
  under `-O` and the code then builds an order at `nan`. Raise instead.

## Funding, hold, and exits

- **`funding_apr` is a fraction, not a percent.** It is the hourly rate
  annualised (`funding_hourly * 24 * 365`), so the floor is `0.1095`, not
  `10.95`. Every display must multiply by 100 before appending `%`; use
  `funding_apr_pct` rather than doing it by hand. Rendering the raw fraction with
  a `%` suffix under-reports by 100x, which is how a 10.95% rate once showed as
  `0.1%` in both `cli.py` and `server.py`.
- **Funding has a venue floor of 0.00125%/hr (10.95%/yr)**, the fixed interest
  component. A rate sitting exactly on the floor is mechanical, not directional:
  the live BTC market reads `0.0000125`/hr for long stretches. Any funding signal
  must be measured *relative to the floor* (`FUNDING_FLOOR_HOURLY`), or it fires
  on a flat market.
- **Carry tilts, it does not decide.** `_FUNDING_CARRY_WEIGHT` is calibrated so
  funding alone cannot force a side in a flat market. A weight that is too large
  makes the bot a one-way funding trade wearing a momentum label.
- **`hold` is a real action**, not a skipped tick. It sends no order and cancels
  the standing quote. The band is `HL_HOLD_SIGNAL` (default `0.3`; `0` disables).
  An unreadable LLM `action` must fall back to the probability, never to `hold`,
  or a garbled reply silently stops trading forever.
- **Entries rest post-only; exits are reduce-only and cross as taker.** With
  `HL_ORDER_TIF=ALO` an exit routed through the normal path can never fill. The
  exit is a full flatten clamped to the position, deliberately bypassing the
  entry-target sizing, because routing it through the target leaves a stub open.
- **`PaperBroker.on_print` matches on price, not the aggressor.** A test that
  passes `is_buy` expecting it to matter is wrong; the unreachable price depends
  on the resting quote's own side. Paper fills are also fractional, so a position
  can end a sub-lot remainder below the $10 minimum that no order can close.

## Opening a PR in this repo

- **`GITHUB_TOKEN` is a GitHub App token without pull-request write.** `gh pr
  create` and `POST /pulls` both return `403 Resource not accessible by
  integration`; the `create_pr` tool fails the same way. Use `GITHUB_ACCESS`
  (classic PAT, `repo` scope) for PR creation. Push works with `GITHUB_TOKEN`.
- After a PR merges, branch the next change from the updated `main` (or
  cherry-pick onto it) rather than reusing the merged branch, so the new PR does
  not carry commits that are already in `main`.

