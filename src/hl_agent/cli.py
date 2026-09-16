"""``hl-agent`` command line.

    fetch        download / extend the local candle cache
    validate     dry-run every scanner of a package (``senpi validate`` semantics)
    backtest     replay a package over the cache, write events + metrics to a run dir
    walkforward  the same over N independent folds
    report       metrics from a run dir (backtest or live)
    run          trade live (testnet by default; mainnet needs --i-accept-real-money)
    status       account snapshot + kill-switch state
    stop         drop the STOP file so a running agent flattens and exits

Settings come from ``config/settings.toml`` (see ``settings.example.toml``); secrets only
from the environment (``HL_AGENT_PRIVATE_KEY``, ``HL_AGENT_ADDRESS``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hl_agent.data.binance_client import BinanceClient
from hl_agent.data.history import CandleStore
from hl_agent.data.hyperliquid_client import HyperliquidClient
from hl_agent.data.models import AccountState, Instrument
from hl_agent.engine.loop import Engine, Event
from hl_agent.execution.backtest import HOUR_MS, BacktestResult, EquityPoint, run_backtest
from hl_agent.execution.live import (
    BrokerError,
    ExchangeApi,
    HlBroker,
    InfoApi,
    LiveMarketSource,
    Network,
    load_address,
    load_signer,
)
from hl_agent.execution.runner import (
    STOP_FILE,
    LiveRunner,
    LoopConfig,
    RefusedError,
    check_network,
)
from hl_agent.execution.sim import SimConfig
from hl_agent.execution.walkforward import walk_forward
from hl_agent.strategy.package import load_package
from hl_agent.strategy.sources import ReplaySource
from hl_agent.telemetry.events import EventLog
from hl_agent.telemetry.metrics import Metrics, compute, from_result
from hl_agent.telemetry.report import render_comparison, render_text, to_json

DEFAULT_SETTINGS = Path("config/settings.toml")
INSTRUMENTS_FILE = "instruments.json"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


class CliError(Exception):
    pass


# ---- settings ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    network: Network = Network.TESTNET
    address: str = ""
    max_leverage: int = 3
    min_notional_usd: float = 10.0
    taker_pct: float = 0.035
    cache_dir: Path = Path("data/cache")
    runs_dir: Path = Path("runs")

    @classmethod
    def load(cls, path: Path | None) -> Settings:
        raw: dict[str, Any] = {}
        p = path or DEFAULT_SETTINGS
        if p.exists():
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        elif path is not None:
            raise CliError(f"settings file not found: {path}")
        net = raw.get("network", {}).get("name", "testnet")
        try:
            network = Network(net)
        except ValueError as exc:
            raise CliError(f"network must be testnet or mainnet, got {net!r}") from exc
        risk, fees, data = raw.get("risk", {}), raw.get("fees", {}), raw.get("data", {})
        return cls(
            network=network,
            address=str(raw.get("account", {}).get("address", "")),
            max_leverage=min(3, int(risk.get("max_leverage", 3))),  # 3x is a hard ceiling
            min_notional_usd=float(risk.get("min_notional_usd", 10.0)),
            taker_pct=float(fees.get("taker_pct", 0.035)),
            cache_dir=Path(data.get("cache_dir", "data/cache")),
            runs_dir=Path(data.get("runs_dir", "runs")),
        )


# ---- helpers -----------------------------------------------------------------------


def parse_when(text: str) -> int:
    """``2026-03-01`` or ``2026-03-01T12:00`` (UTC) or raw epoch ms."""
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CliError(f"bad date {text!r}, use YYYY-MM-DD") from exc
    return int(dt.replace(tzinfo=dt.tzinfo or UTC).timestamp() * 1000)


def fmt_when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def package_env(package: Path, address: str) -> dict[str, str]:
    """Environment for ``${VAR}`` substitution: real env first, then every unset variable the
    recipe mentions falls back to the configured address (recipes only reference wallets)."""
    env = dict(os.environ)
    text = (package / "runtime.yaml").read_text(encoding="utf-8") if package.is_dir() else ""
    if package.is_file():
        text = package.read_text(encoding="utf-8")
    for var in _VAR.findall(text):
        env.setdefault(var, address or "0x0")
    return env


def save_instruments(store_dir: Path, instruments: Sequence[Instrument]) -> None:
    rows = [
        {
            "name": i.name,
            "size_decimals": i.size_decimals,
            "max_leverage": i.max_leverage,
            "only_isolated": i.only_isolated,
            "delisted": i.delisted,
        }
        for i in instruments
    ]
    (store_dir / INSTRUMENTS_FILE).write_text(json.dumps(rows, indent=1), encoding="utf-8")


def load_instruments(store_dir: Path, assets: Sequence[str] | None = None) -> list[Instrument]:
    p = store_dir / INSTRUMENTS_FILE
    if not p.exists():
        raise CliError(f"{p} missing: run `hl-agent fetch` first")
    rows = json.loads(p.read_text(encoding="utf-8"))
    out = [Instrument(**r) for r in rows]
    if assets:
        wanted = set(assets)
        out = [i for i in out if i.name in wanted]
        missing = wanted - {i.name for i in out}
        if missing:
            raise CliError(f"unknown instruments: {sorted(missing)}")
    return out


def cached_assets(store_dir: Path, interval: str) -> list[str]:
    return sorted(
        p.name[: -len(f"_{interval}.parquet")].replace("_", ":")
        for p in store_dir.glob(f"*_{interval}.parquet")
    )


def cache_window(store: CandleStore, assets: Sequence[str], interval: str) -> tuple[int, int]:
    firsts, lasts = [], []
    for a in assets:
        f = store.frame(a, interval)
        if f.is_empty():
            raise CliError(f"no {interval} candles cached for {a}: run `hl-agent fetch`")
        col = f["open_ms"]
        firsts.append(int(str(col.min())))
        lasts.append(int(str(col.max())))
    return max(firsts), min(lasts)


def run_dir(settings: Settings, name: str) -> Path:
    d = settings.runs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_run(out: Path, result: BacktestResult, metrics: Metrics) -> None:
    (out / "events.jsonl").unlink(missing_ok=True)  # a re-run replaces, never appends
    EventLog(out / "events.jsonl").write_all(result.events)
    with (out / "equity.jsonl").open("w", encoding="utf-8") as fh:
        for p in result.equity:
            fh.write(json.dumps({"time_ms": p.time_ms, "account_value": p.account_value}) + "\n")
    (out / "metrics.json").write_text(to_json(metrics), encoding="utf-8")


def read_equity(path: Path) -> list[EquityPoint]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw = json.loads(line)
            out.append(EquityPoint(int(raw["time_ms"]), float(raw["account_value"])))
    return out


# ---- commands ----------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace, s: Settings) -> int:
    """Hyperliquid (mainnet history) first, so its bars own every window it still serves;
    Binance then only backfills what Hyperliquid has already forgotten."""
    since = parse_when(args.since)
    store = CandleStore(s.cache_dir)
    with HyperliquidClient(Network.MAINNET.url) as client:
        instruments = client.instruments()
        save_instruments(s.cache_dir, instruments)
        known = {i.name for i in instruments}
        unknown = [a for a in args.assets if a not in known]
        if unknown:
            raise CliError(f"unknown asset(s) {unknown}")
        if args.source in ("hyperliquid", "both"):
            for asset in args.assets:
                for interval in args.intervals:
                    added = store.sync(client, asset, interval, since)
                    _print_window(store, asset, interval, "hl", added)
    if args.source in ("binance", "both"):
        with BinanceClient() as binance:
            for asset in args.assets:
                if ":" in asset:
                    print(f"{asset:<8}     skipped: no Binance market for HIP-3 assets")
                    continue
                for interval in args.intervals:
                    added = store.backfill(binance, asset, interval, since)
                    _print_window(store, asset, interval, "binance", added)
    return 0


def _print_window(store: CandleStore, asset: str, interval: str, tag: str, added: int) -> None:
    lo, hi = cache_window(store, [asset], interval)
    print(f"{asset:<8}{interval:<4}{tag:<8} +{added:<6} {fmt_when(lo)} -> {fmt_when(hi)}")


def _replay(s: Settings, assets: Sequence[str] | None) -> tuple[ReplaySource, list[Instrument]]:
    store = CandleStore(s.cache_dir)
    names = list(assets) if assets else cached_assets(s.cache_dir, "1h")
    instruments = load_instruments(s.cache_dir, names)
    src = ReplaySource(store, instruments, _flat)
    return src, instruments


def _flat() -> AccountState:
    return AccountState(100.0, 100.0, 0.0, ())


def cmd_validate(args: argparse.Namespace, s: Settings) -> int:
    package = Path(args.package)
    src, instruments = _replay(s, args.assets)
    _, last = cache_window(CandleStore(s.cache_dir), [i.name for i in instruments], "1h")
    src.now_ms = last
    pkg = load_package(
        package, src, freeze_time=True, enforce_timeout=True, env=package_env(package, s.address)
    )
    print(
        f"{pkg.spec.name}: slots {pkg.engine_config.strategy.slots}, "
        f"margin {pkg.engine_config.strategy.margin_pct}%, "
        f"leverage cap {pkg.engine_config.strategy.max_leverage}x"
    )
    bad = 0
    for name, rep in pkg.validate(last).items():
        verdict = "OK" if rep.ok and rep.proven else "UNPROVEN" if rep.ok else "FAIL"
        bad += verdict == "FAIL"
        extra = f" {rep.error}" if rep.error else f" signals={len(rep.signals)}"
        print(f"  {name:<28}{verdict:<10}{rep.elapsed_s:6.2f}s{extra}")
    return 1 if bad else 0


def _window(args: argparse.Namespace, s: Settings, assets: Sequence[str]) -> tuple[int, int]:
    first, last = cache_window(CandleStore(s.cache_dir), assets, "1h")
    start = parse_when(args.start) if args.start else first + args.warmup_hours * HOUR_MS
    end = parse_when(args.end) if args.end else last
    if end <= start:
        raise CliError(f"empty window {fmt_when(start)} -> {fmt_when(end)}")
    return start, end


def _sim(args: argparse.Namespace, s: Settings) -> SimConfig:
    return SimConfig(
        taker_fee_bps=s.taker_pct * 100 if args.fee_bps is None else args.fee_bps,
        slippage_bps=args.slippage_bps,
        funding_hourly=args.funding_hourly,
    )


def cmd_backtest(args: argparse.Namespace, s: Settings) -> int:
    package = Path(args.package)
    store = CandleStore(s.cache_dir)
    instruments = load_instruments(s.cache_dir, args.assets or cached_assets(s.cache_dir, "1h"))
    start, end = _window(args, s, [i.name for i in instruments])
    print(
        f"backtest {package.name}: {fmt_when(start)} -> {fmt_when(end)}, "
        f"{len(instruments)} assets, {args.cash:.0f} USD"
    )
    result = run_backtest(
        package,
        store,
        instruments,
        start_ms=start,
        end_ms=end,
        step_ms=args.step_hours * HOUR_MS,
        initial_cash=args.cash,
        sim=_sim(args, s),
        max_leverage=s.max_leverage,
        env=package_env(package, s.address),
    )
    metrics = from_result(result)
    print(render_text(metrics, title=result.strategy))
    out = run_dir(s, args.out or f"bt-{package.name}-{datetime.now(UTC):%Y%m%d-%H%M%S}")
    write_run(out, result, metrics)
    print(f"written to {out}")
    return 0


def cmd_walkforward(args: argparse.Namespace, s: Settings) -> int:
    package = Path(args.package)
    store = CandleStore(s.cache_dir)
    instruments = load_instruments(s.cache_dir, args.assets or cached_assets(s.cache_dir, "1h"))
    start, end = _window(args, s, [i.name for i in instruments])
    wf = walk_forward(
        package,
        store,
        instruments,
        start_ms=start,
        end_ms=end,
        folds=args.folds,
        step_ms=args.step_hours * HOUR_MS,
        initial_cash=args.cash,
        sim=_sim(args, s),
        max_leverage=s.max_leverage,
        env=package_env(package, s.address),
    )
    rows = [(f"fold{f.index} {fmt_when(f.start_ms)[:10]}", f.metrics) for f in wf.folds]
    print(render_comparison(rows))
    print(
        f"profitable folds {wf.profitable_folds}/{len(wf.folds)}, "
        f"compounded {wf.compounded_return_pct:+.2f}%, "
        f"worst fold DD {wf.worst_fold_drawdown_pct:.1f}%"
    )
    if args.out:
        out = run_dir(s, args.out)
        for f in wf.folds:
            write_run(run_dir(s, f"{args.out}/fold{f.index}"), f.result, f.metrics)
        print(f"written to {out}")
    return 0


def cmd_report(args: argparse.Namespace, s: Settings) -> int:
    d = Path(args.run)
    if not d.is_absolute() and not d.exists():
        d = s.runs_dir / args.run
    events = EventLog(d / "events.jsonl").read()
    equity = read_equity(d / "equity.jsonl")
    if not events and not equity:
        raise CliError(f"nothing to report in {d}")
    initial = equity[0].account_value if equity else args.cash
    metrics = compute(events, equity, initial=initial)
    print(render_text(metrics, title=d.name))
    if args.json:
        print(to_json(metrics))
    return 0


@dataclass(frozen=True, slots=True)
class Venue:
    """Live wiring; ``build_venue`` is the only place that talks to the real SDK, so tests
    swap it for fakes."""

    market: LiveMarketSource
    exchange: ExchangeApi | None = None
    info: InfoApi | None = None


def build_venue(s: Settings, address: str, *, trading: bool) -> Venue:
    with HyperliquidClient(s.network.url) as probe:
        dexs = ["", *probe.perp_dexs()]
    market = LiveMarketSource(HyperliquidClient(s.network.url), address, dexs=dexs)
    if not trading:
        return Venue(market)
    from hyperliquid.exchange import Exchange  # type: ignore[import-untyped]
    from hyperliquid.info import Info  # type: ignore[import-untyped]

    signer = load_signer()  # raises before anything else happens if the key is missing
    exchange = Exchange(signer, base_url=s.network.url, account_address=address)
    return Venue(market, exchange, Info(base_url=s.network.url, skip_ws=True))


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _address(s: Settings) -> str:
    return s.address or load_address()


def cmd_status(args: argparse.Namespace, s: Settings) -> int:
    address = _address(s)
    market = build_venue(s, address, trading=False).market
    market.refresh(_now_ms())
    acct = market.account()
    print(f"network {s.network.value}  address {address}")
    print(
        f"account value {acct.account_value:.2f}  withdrawable {acct.withdrawable:.2f}  "
        f"margin used {acct.total_margin_used:.2f}"
    )
    for p in acct.positions:
        print(
            f"  {p.asset:<10}{p.direction.value:<6}{p.size:<12g}entry {p.entry_price:<12g}"
            f"{p.leverage}x  upnl {p.unrealized_pnl:+.2f} ({p.roe_pct:+.1f}%)"
        )
    if s.runs_dir.exists():
        for stop in sorted(s.runs_dir.glob(f"*/{STOP_FILE}")):
            print(f"STOP present in {stop.parent}")
    return 0


def cmd_stop(args: argparse.Namespace, s: Settings) -> int:
    d = run_dir(s, args.name)
    (d / STOP_FILE).touch()
    print(f"STOP written to {d / STOP_FILE}; the agent will flatten on its next tick")
    return 0


class RecordingRunner(LiveRunner):
    """``LiveRunner`` that also appends one equity point per tick."""

    def __init__(self, *args: Any, market: LiveMarketSource, equity_path: Path, **kw: Any):
        super().__init__(*args, **kw)
        self._mkt = market
        self._equity_path = equity_path

    def tick(self) -> list[Event]:
        events = super().tick()
        value = self._mkt.account().account_value
        with self._equity_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time_ms": self._mkt.now_ms, "account_value": value}) + "\n")
        return events


def cmd_run(args: argparse.Namespace, s: Settings) -> int:
    check_network(s.network, accept_real_money=args.i_accept_real_money)
    package = Path(args.package)
    address = _address(s)
    out = run_dir(s, args.name or package.name)
    if (out / STOP_FILE).exists():
        raise CliError(f"{out / STOP_FILE} exists: remove it to start")
    venue = build_venue(s, address, trading=True)
    if venue.exchange is None or venue.info is None:
        raise CliError("venue has no trading endpoint")
    market = venue.market
    market.refresh(_now_ms())
    broker = HlBroker(
        venue.exchange, venue.info, address, market=market, taker_fee_bps=s.taker_pct * 100
    )
    pkg = load_package(
        package,
        market,
        state_dir=out,
        enforce_timeout=True,
        max_leverage=s.max_leverage,
        env=package_env(package, address),
    )
    engine = Engine(pkg.engine_config, market, broker, pkg.source, now_ms=market.now_ms)
    log = EventLog(out / "events.jsonl")

    def sink(e: Event) -> None:
        log.write(e)
        print(f"{fmt_when(e.time_ms)} {e.kind:<10}{e.asset:<10}{e.reason}")

    runner = RecordingRunner(
        engine,
        market,
        LoopConfig(interval_s=args.interval, run_dir=out),
        sink=sink,
        market=market,
        equity_path=out / "equity.jsonl",
    )
    print(
        f"{s.network.value} | {pkg.spec.name} | {address} | every {args.interval:.0f}s | "
        f"kill switch: {out / STOP_FILE}"
    )
    why = runner.run(max_ticks=args.max_ticks)
    print(f"stopped: {why} after {runner.ticks} ticks, {runner.errors} errors")
    return 0 if why in ("stop_file", "max_ticks") else 1


# ---- parser ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hl-agent",
        description=(__doc__ or "").split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--settings", type=Path, default=None, help="config/settings.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download candles into the cache")
    f.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL"])
    f.add_argument("--intervals", nargs="+", default=["1h", "4h", "1d"])
    f.add_argument("--since", default="2024-01-01")
    f.add_argument(
        "--source",
        choices=["hyperliquid", "binance", "both"],
        default="both",
        help="binance only backfills bars older than the cached Hyperliquid history",
    )
    f.set_defaults(fn=cmd_fetch)

    v = sub.add_parser("validate", help="dry-run every scanner of a package")
    v.add_argument("package")
    v.add_argument("--assets", nargs="+", default=None)
    v.set_defaults(fn=cmd_validate)

    def sim_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("package")
        sp.add_argument("--assets", nargs="+", default=None)
        sp.add_argument("--start", default=None, help="YYYY-MM-DD (UTC)")
        sp.add_argument("--end", default=None)
        sp.add_argument("--warmup-hours", type=int, default=400)
        sp.add_argument("--cash", type=float, default=100.0)
        sp.add_argument("--step-hours", type=int, default=1)
        sp.add_argument("--fee-bps", type=float, default=None)
        sp.add_argument("--slippage-bps", type=float, default=5.0)
        sp.add_argument("--funding-hourly", type=float, default=0.0)
        sp.add_argument("--out", default=None, help="run name under runs/")

    b = sub.add_parser("backtest", help="replay a package over the cache")
    sim_args(b)
    b.set_defaults(fn=cmd_backtest)

    w = sub.add_parser("walkforward", help="independent folds over the cache")
    sim_args(w)
    w.add_argument("--folds", type=int, default=3)
    w.set_defaults(fn=cmd_walkforward)

    r = sub.add_parser("report", help="metrics from a run directory")
    r.add_argument("run")
    r.add_argument("--cash", type=float, default=100.0, help="initial equity if unknown")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_report)

    st = sub.add_parser("status", help="account snapshot")
    st.set_defaults(fn=cmd_status)

    sp = sub.add_parser("stop", help="write the STOP file for a run")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_stop)

    ru = sub.add_parser("run", help="trade live (testnet unless settings say mainnet)")
    ru.add_argument("package")
    ru.add_argument("--name", default=None, help="run name under runs/")
    ru.add_argument("--interval", type=float, default=60.0)
    ru.add_argument("--max-ticks", type=int, default=None)
    ru.add_argument("--i-accept-real-money", action="store_true")
    ru.set_defaults(fn=cmd_run)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.settings)
        return int(args.fn(args, settings))
    except (CliError, BrokerError, RefusedError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
