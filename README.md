# hl-agent

Autonomous, risk-managed trading agent for Hyperliquid, with a backtester that runs the
exact same engine as live trading. Strategy contract is Senpi-compatible: a Senpi
`scan.py` / `runtime.yaml` package runs here unmodified.

## Layout

```
src/hl_agent/
  data/        Hyperliquid read API, typed models, Parquet candle cache, Senpi payload shapes
  strategy/    scan(inputs, ctx) contract, state store, package loader, MCP shim   (axe 2)
  engine/      sizing, guardrails, DSL exits, dedup, step loop — pure, no I/O      (axe 3)
  execution/   Broker interface: simulated (backtest) and Hyperliquid (live)       (axe 4)
  telemetry/   event log, metrics, reports                                         (axe 5)
  cli.py       fetch / validate / backtest / walkforward / run / report / status  (axe 6)
  copy/        copy-trading: leaderboard discovery + mirror source                 (axe 8)
strategies/    strategy packages (Senpi-format)
config/        settings.example.toml → copy to settings.toml (git-ignored)
config/strategies/copy   exit ladder + rails used by `run --copy` (no scanners)
tests/         pytest; fixtures are real recorded Hyperliquid payloads
```

## Usage

```bash
cp config/settings.example.toml config/settings.toml   # network, address, risk caps
hl-agent fetch --assets BTC ETH SOL --intervals 1h 4h 1d --since 2023-01-01
#   Hyperliquid mainnet history first, then Binance USDT-perp klines backfill older bars
hl-agent validate strategies/compass                        # dry-run every scanner
hl-agent backtest strategies/compass --start 2026-04-01 --out compass-q2
hl-agent walkforward strategies/compass --folds 3
hl-agent report compass-q2 --json                           # metrics from runs/<name>
```

Live (testnet by default — set `[network] name = "mainnet"` **and** pass
`--i-accept-real-money` for real funds):

```bash
set HL_AGENT_PRIVATE_KEY=0x...     # agent-wallet key, never stored on disk
hl-agent status                    # account snapshot for [account].address
hl-agent run strategies/compass --name compass-live --interval 60
hl-agent stop compass-live         # kill switch: the agent flattens on its next tick
hl-agent report compass-live
```

Backtest and live share one engine; a run directory (`runs/<name>/`) always holds
`events.jsonl`, `equity.jsonl` and, for backtests, `metrics.json`.

### Copy-trading

Mirror one Hyperliquid address (Senpi's copy blueprint, on public data only):

```bash
hl-agent traders --top 20                 # candidates from HL's public leaderboard
hl-agent mirror-sim 0xTRADER --budget 999 # what we would open now, and at 100 $
hl-agent run config/strategies/copy --copy 0xTRADER --name copy-live --poll 300
```

`traders` blends three leaderboard views (7d ROI, 30d ROI, 30d PnL) over accounts with
10k-5M equity and a window ROI between 0 and +500 % (anything else is a lottery ticket or
a vault), then dry-runs a mirror of each live book: *fit* is the share of their notional
still within `--slippage` of their entry, `opens x/y` how many lines a 100 $ budget
copies. The trader is always read on mainnet, whatever network we trade on.

The mirror copies their *allocation* (margin as a share of equity, scaled down when they
use more than 90 %) and their leverage up to our `max_leverage` cap; their stops are not
copied — the package's DSL ladder, guard rails and kill switch apply. Books are polled
every `--poll` seconds: new positions are entered while still within the slippage band,
closed ones are closed (`source_closed`), flipped ones close now and re-enter next tick.
Margin resizes are ignored in v1. `--no-initial` skips the book found at start-up.

## Dev

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/pytest --cov=hl_agent
.venv/Scripts/ruff check src tests && .venv/Scripts/mypy
```

## Data notes

Hyperliquid serves at most ~5000 bars per interval and keeps a bounded history
(1h ≈ 7 months, 4h ≈ 2+ years, 1d since listing). `CandleStore.sync` accumulates
bars over time; `CandleStore.backfill` fills everything older from Binance
USDT-perp klines (`data/binance_client.py`, public endpoint, no key). Hyperliquid
bars are never overwritten, so live-venue data always wins where both exist.
