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
  cli.py       fetch / validate / backtest / run / report                          (axe 6)
strategies/    strategy packages (Senpi-format)
config/        settings.example.toml → copy to settings.toml (git-ignored)
tests/         pytest; fixtures are real recorded Hyperliquid payloads
```

## Dev

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/pytest --cov=hl_agent
.venv/Scripts/ruff check src tests && .venv/Scripts/mypy
```

## Data notes

Hyperliquid serves at most ~5000 bars per interval and keeps a bounded history
(1h ≈ 7 months, 4h ≈ 2+ years, 1d since listing). `CandleStore.sync` accumulates
bars over time; run it regularly to extend the local 1h history.
