"""Performance metrics from a stream of engine events + an equity curve.

Pure functions over ``Event`` objects so the same code scores a backtest result, a live
JSONL log, or one walk-forward fold. Nothing here knows about the venue.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from hl_agent.engine.loop import Event

if TYPE_CHECKING:  # annotations only: telemetry must not import the execution layer at runtime
    from hl_agent.execution.backtest import BacktestResult, EquityPoint


@dataclass(frozen=True, slots=True)
class Trade:
    asset: str
    direction: str
    scanner: str
    opened_ms: int
    closed_ms: int
    entry_price: float
    exit_price: float
    size: float
    leverage: float
    pnl_usd: float
    roe_pct: float
    high_water_roe: float
    reason: str

    @property
    def held_hours(self) -> float:
        return (self.closed_ms - self.opened_ms) / 3_600_000.0

    @property
    def month(self) -> str:
        return datetime.fromtimestamp(self.closed_ms / 1000, UTC).strftime("%Y-%m")


def trades_from_events(events: Iterable[Event]) -> list[Trade]:
    out: list[Trade] = []
    for e in events:
        if e.kind != "closed":
            continue
        p = e.payload
        held_ms = int(float(p.get("held_minutes", 0.0)) * 60_000)
        out.append(
            Trade(
                asset=e.asset,
                direction=str(p.get("direction", "")),
                scanner=str(p.get("scanner", "")),
                opened_ms=e.time_ms - held_ms,
                closed_ms=e.time_ms,
                entry_price=float(p.get("entry_price", 0.0)),
                exit_price=float(p.get("exit_price", 0.0)),
                size=float(p.get("size", 0.0)),
                leverage=float(p.get("leverage", 1.0)),
                pnl_usd=float(p.get("pnl_usd", 0.0)),
                roe_pct=float(p.get("roe_pct", 0.0)),
                high_water_roe=float(p.get("high_water_roe", 0.0)),
                reason=e.reason,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Drawdown:
    max_pct: float
    peak_ms: int
    trough_ms: int
    longest_ms: int  # longest stretch under a previous peak


def drawdown(equity: Sequence[EquityPoint]) -> Drawdown:
    if not equity:
        return Drawdown(0.0, 0, 0, 0)
    peak_v, peak_t = equity[0].account_value, equity[0].time_ms
    worst, worst_peak_t, worst_t = 0.0, peak_t, peak_t
    longest, under_since = 0, peak_t
    for p in equity:
        if p.account_value >= peak_v:
            longest = max(longest, p.time_ms - under_since)
            peak_v, peak_t, under_since = p.account_value, p.time_ms, p.time_ms
            continue
        dd = (peak_v - p.account_value) / peak_v * 100.0 if peak_v > 0 else 0.0
        if dd > worst:
            worst, worst_peak_t, worst_t = dd, peak_t, p.time_ms
    longest = max(longest, equity[-1].time_ms - under_since)
    return Drawdown(worst, worst_peak_t, worst_t, longest)


@dataclass(frozen=True, slots=True)
class Metrics:
    initial: float
    final: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float  # inf when no losses
    net_pnl: float
    return_pct: float
    expectancy: float  # mean pnl per trade, USD
    avg_win: float
    avg_loss: float
    payoff_ratio: float  # avg_win / |avg_loss|
    max_consecutive_losses: int
    avg_held_hours: float
    fees_paid: float
    funding_paid: float
    drawdown: Drawdown
    sharpe_daily: float  # annualised from daily equity returns
    by_reason: dict[str, int] = field(default_factory=dict)
    by_asset: dict[str, float] = field(default_factory=dict)  # net pnl per asset
    rejections: dict[str, int] = field(default_factory=dict)
    monthly: dict[str, float] = field(default_factory=dict)  # net pnl per YYYY-MM


def _daily_sharpe(equity: Sequence[EquityPoint]) -> float:
    if len(equity) < 3:
        return 0.0
    days: dict[int, float] = {}
    for p in equity:
        days[p.time_ms // 86_400_000] = p.account_value  # last value of each day
    values = list(days.values())
    rets = [b / a - 1.0 for a, b in pairwise(values) if a > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return mean / sd * math.sqrt(365.0) if sd > 0 else 0.0


def compute(
    events: Iterable[Event],
    equity: Sequence[EquityPoint],
    *,
    initial: float,
    final: float | None = None,
    fees_paid: float = 0.0,
    funding_paid: float = 0.0,
) -> Metrics:
    events = list(events)
    trades = trades_from_events(events)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_profit = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses)
    final_v = final if final is not None else (equity[-1].account_value if equity else initial)
    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t.pnl_usd <= 0 else 0
        worst_streak = max(worst_streak, streak)
    by_asset: dict[str, float] = defaultdict(float)
    monthly: dict[str, float] = defaultdict(float)
    for t in trades:
        by_asset[t.asset] += t.pnl_usd
        monthly[t.month] += t.pnl_usd
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    return Metrics(
        initial=initial,
        final=final_v,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades) * 100.0 if trades else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else math.inf,
        net_pnl=final_v - initial,
        return_pct=(final_v / initial - 1.0) * 100.0 if initial > 0 else 0.0,
        expectancy=sum(t.pnl_usd for t in trades) / len(trades) if trades else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=avg_win / -avg_loss if avg_loss < 0 else math.inf,
        max_consecutive_losses=worst_streak,
        avg_held_hours=sum(t.held_hours for t in trades) / len(trades) if trades else 0.0,
        fees_paid=fees_paid,
        funding_paid=funding_paid,
        drawdown=drawdown(equity),
        sharpe_daily=_daily_sharpe(equity),
        by_reason=dict(Counter(t.reason for t in trades)),
        by_asset=dict(sorted(by_asset.items())),
        rejections=dict(Counter(e.reason for e in events if e.kind == "rejected")),
        monthly=dict(sorted(monthly.items())),
    )


def from_result(result: BacktestResult) -> Metrics:
    return compute(
        result.events,
        result.equity,
        initial=result.initial_cash,
        final=result.final_account.account_value,
        fees_paid=result.fees_paid,
        funding_paid=result.funding_paid,
    )
