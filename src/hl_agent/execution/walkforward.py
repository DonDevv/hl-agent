"""Walk-forward evaluation: the window is cut into consecutive folds and each fold is a
fresh backtest (new cash, new state, new guard rails), so one lucky month cannot carry the
whole result and a strategy must work in *every* regime it meets.

The strategies here have no tunable parameters to fit in-sample, so this is the honest
"does it keep working out of sample" check rather than an optimisation loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from hl_agent.data.history import CandleStore
from hl_agent.data.models import Instrument
from hl_agent.execution.backtest import HOUR_MS, BacktestResult, run_backtest
from hl_agent.execution.sim import DEFAULT_SIM, SimConfig
from hl_agent.telemetry.metrics import Metrics, from_result


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    start_ms: int
    end_ms: int
    result: BacktestResult
    metrics: Metrics


@dataclass(frozen=True, slots=True)
class WalkForward:
    folds: Sequence[Fold]

    @property
    def profitable_folds(self) -> int:
        return sum(1 for f in self.folds if f.metrics.net_pnl > 0)

    @property
    def compounded_return_pct(self) -> float:
        growth = 1.0
        for f in self.folds:
            growth *= 1.0 + f.metrics.return_pct / 100.0
        return (growth - 1.0) * 100.0

    @property
    def worst_fold_drawdown_pct(self) -> float:
        return max((f.metrics.drawdown.max_pct for f in self.folds), default=0.0)


def split(start_ms: int, end_ms: int, folds: int, step_ms: int = 1) -> list[tuple[int, int]]:
    """Consecutive folds whose starts sit on a ``step_ms`` grid, so a fold's ticks never drift
    off the bar boundaries (a tick at hh:51 would price off a bar 51 minutes stale while the
    scanner already sees fresher 15m bars: look-ahead)."""
    if folds < 1 or end_ms <= start_ms or step_ms < 1:
        raise ValueError("need at least one fold, a non-empty window and a positive step")
    width = (end_ms - start_ms + 1) // folds
    starts = [-(-(start_ms + i * width) // step_ms) * step_ms for i in range(folds)]
    return [(a, end_ms if i == folds - 1 else starts[i + 1] - 1) for i, a in enumerate(starts)]


def walk_forward(
    package_path: Path | str,
    store: CandleStore,
    instruments: Sequence[Instrument],
    *,
    start_ms: int,
    end_ms: int,
    folds: int = 4,
    step_ms: int = HOUR_MS,
    initial_cash: float = 100.0,
    sim: SimConfig = DEFAULT_SIM,
    price_interval: str = "1h",
    max_leverage: int = 3,
    env: dict[str, str] | None = None,
    force_leverage: int | None = None,
) -> WalkForward:
    out: list[Fold] = []
    for i, (a, b) in enumerate(split(start_ms, end_ms, folds, step_ms)):
        res = run_backtest(
            package_path,
            store,
            instruments,
            start_ms=a,
            end_ms=b,
            step_ms=step_ms,
            initial_cash=initial_cash,
            sim=sim,
            price_interval=price_interval,
            max_leverage=max_leverage,
            env=env,
            force_leverage=force_leverage,
        )
        out.append(Fold(i, a, b, res, from_result(res)))
    return WalkForward(tuple(out))
