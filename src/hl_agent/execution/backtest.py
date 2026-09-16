"""Drive a strategy package through history: clock -> sim venue -> engine, bar by bar.

Every tick is the same three calls the live runner makes (``mark`` the venue, ``step`` the
engine, record events), so a strategy that backtests here runs live unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hl_agent.data.history import CandleStore
from hl_agent.data.models import AccountState, Instrument
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.loop import Engine, Event
from hl_agent.execution.sim import DEFAULT_SIM, SimBroker, SimConfig
from hl_agent.strategy.package import LoadedPackage, load_package
from hl_agent.strategy.sources import ReplaySource

HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class EquityPoint:
    time_ms: int
    account_value: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    start_ms: int
    end_ms: int
    initial_cash: float
    final_account: AccountState
    events: Sequence[Event] = field(default_factory=tuple)
    equity: Sequence[EquityPoint] = field(default_factory=tuple)
    fees_paid: float = 0.0
    funding_paid: float = 0.0

    @property
    def closed_trades(self) -> list[Event]:
        return [e for e in self.events if e.kind == "closed"]

    @property
    def net_pnl(self) -> float:
        return self.final_account.account_value - self.initial_cash


@dataclass(frozen=True, slots=True)
class BacktestSetup:
    """Everything wired and ready; ``run`` walks the clock."""

    package: LoadedPackage
    source: ReplaySource
    broker: SimBroker
    engine: Engine


def prepare(
    package_path: Path | str,
    store: CandleStore,
    instruments: Sequence[Instrument],
    *,
    start_ms: int,
    initial_cash: float = 100.0,
    sim: SimConfig = DEFAULT_SIM,
    price_interval: str = "1h",
    max_leverage: int = 3,
    env: dict[str, str] | None = None,
) -> BacktestSetup:
    broker_ref: list[SimBroker] = []
    source = ReplaySource(
        store, instruments, lambda: broker_ref[0].account(), price_interval=price_interval
    )
    source.now_ms = start_ms
    broker = SimBroker(source, sim, initial_cash)
    broker_ref.append(broker)
    package = load_package(
        package_path, source, freeze_time=True, env=env, max_leverage=max_leverage
    )
    engine = Engine(package.engine_config, broker, broker, package.source, now_ms=start_ms)
    return BacktestSetup(package, source, broker, engine)


def run(
    setup: BacktestSetup,
    *,
    end_ms: int,
    step_ms: int = HOUR_MS,
    flatten_at_end: bool = True,
) -> BacktestResult:
    src, broker, engine = setup.source, setup.broker, setup.engine
    start_ms = -(-src.now_ms // step_ms) * step_ms  # ticks on the bar grid, never off it
    initial = broker.cash
    events: list[Event] = []
    equity: list[EquityPoint] = []
    now = start_ms
    while now <= end_ms:
        src.now_ms = now
        broker.mark(now)
        events.extend(engine.step(now))
        equity.append(EquityPoint(now, broker.account().account_value))
        now += step_ms
    if flatten_at_end:
        events.extend(engine.close_all(end_ms, CloseReason.MANUAL_CLOSE))
    final = broker.account()
    equity.append(EquityPoint(end_ms, final.account_value))  # the flattened, fee-paid value
    src.now_ms = now  # a second ``run`` on the same setup resumes at the next tick
    return BacktestResult(
        strategy=setup.package.spec.name,
        start_ms=start_ms,
        end_ms=end_ms,
        initial_cash=initial,
        final_account=final,
        events=tuple(events),
        equity=tuple(equity),
        fees_paid=broker.fees_paid,
        funding_paid=broker.funding_paid,
    )


def run_backtest(
    package_path: Path | str,
    store: CandleStore,
    instruments: Sequence[Instrument],
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int = HOUR_MS,
    initial_cash: float = 100.0,
    sim: SimConfig = DEFAULT_SIM,
    price_interval: str = "1h",
    max_leverage: int = 3,
    env: dict[str, str] | None = None,
    flatten_at_end: bool = True,
) -> BacktestResult:
    setup = prepare(
        package_path,
        store,
        instruments,
        start_ms=start_ms,
        initial_cash=initial_cash,
        sim=sim,
        price_interval=price_interval,
        max_leverage=max_leverage,
        env=env,
    )
    return run(setup, end_ms=end_ms, step_ms=step_ms, flatten_at_end=flatten_at_end)
