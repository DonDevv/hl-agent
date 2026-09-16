from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hl_agent.data.history import CandleStore
from hl_agent.engine.loop import Event
from hl_agent.execution.backtest import EquityPoint, run_backtest
from hl_agent.execution.walkforward import split, walk_forward
from hl_agent.telemetry.events import EventLog, from_dict, to_dict
from hl_agent.telemetry.metrics import compute, drawdown, from_result, trades_from_events
from hl_agent.telemetry.report import render_comparison, render_text, to_json
from tests.strategy.conftest import INSTRUMENTS, STRATEGIES, H

D = 86_400_000


def closed(
    t: int, asset: str, pnl: float, reason: str = "hard_timeout", held_h: float = 5
) -> Event:
    return Event(
        t,
        "closed",
        asset,
        reason,
        {
            "direction": "LONG",
            "scanner": "s",
            "entry_price": 100.0,
            "exit_price": 100.0 + pnl,
            "size": 1.0,
            "leverage": 2,
            "pnl_usd": pnl,
            "roe_pct": pnl * 2,
            "high_water_roe": max(0.0, pnl * 2),
            "held_minutes": held_h * 60,
        },
    )


EVENTS = [
    Event(0, "opened", "BTC", "submitted", {}),
    closed(1 * D, "BTC", 10.0, "exchange_sl_hit"),
    closed(2 * D, "ETH", -4.0),
    closed(3 * D, "ETH", -6.0),
    closed(35 * D, "SOL", 5.0, "weak_peak_cut"),
    Event(4 * D, "rejected", "BTC", "no_slots", {}),
    Event(5 * D, "rejected", "BTC", "no_slots", {}),
]
EQUITY = [
    EquityPoint(0, 100.0),
    EquityPoint(1 * D, 110.0),
    EquityPoint(2 * D, 106.0),
    EquityPoint(3 * D, 100.0),
    EquityPoint(20 * D, 104.0),
    EquityPoint(35 * D, 105.0),
]


def test_metrics_from_events() -> None:
    m = compute(EVENTS, EQUITY, initial=100.0, fees_paid=0.5)
    assert m.trades == 4 and m.wins == 2 and m.losses == 2 and m.win_rate == 50.0
    assert m.gross_profit == 15.0 and m.gross_loss == 10.0 and m.profit_factor == 1.5
    assert m.final == 105.0 and m.net_pnl == 5.0 and m.return_pct == pytest.approx(5.0)
    assert m.expectancy == pytest.approx(1.25) and m.avg_win == 7.5 and m.avg_loss == -5.0
    assert m.payoff_ratio == 1.5 and m.max_consecutive_losses == 2 and m.avg_held_hours == 5
    assert m.by_reason == {"exchange_sl_hit": 1, "hard_timeout": 2, "weak_peak_cut": 1}
    assert m.by_asset == {"BTC": 10.0, "ETH": -10.0, "SOL": 5.0}
    assert m.rejections == {"no_slots": 2}
    assert m.monthly == {"1970-01": 0.0, "1970-02": 5.0}
    dd = m.drawdown
    assert dd.max_pct == pytest.approx(100 * 10 / 110) and dd.peak_ms == D and dd.trough_ms == 3 * D
    assert dd.longest_ms == 34 * D  # under the day-1 peak until the end
    assert m.sharpe_daily != 0.0
    assert trades_from_events(EVENTS)[0].opened_ms == D - 5 * H

    empty = compute([], [], initial=100.0)
    assert empty.trades == 0 and math.isinf(empty.profit_factor) and empty.drawdown.max_pct == 0
    assert drawdown([]).longest_ms == 0


def test_report_renderers() -> None:
    m = compute(EVENTS, EQUITY, initial=100.0)
    text = render_text(m, title="demo")
    assert "== demo ==" in text and "win rate 50.0%" in text and "1970-02 +5.00" in text
    assert "no_slots 2" in text and "max DD      9.1%" in text
    empty = compute([], [], initial=100.0)
    assert "inf" in render_text(empty)
    data = json.loads(to_json(empty))
    assert data["profit_factor"] is None and data["drawdown"]["max_pct"] == 0
    table = render_comparison([("a", m), ("b", empty)])
    assert table.splitlines()[2].startswith("a") and "+5.00%" in table


def test_event_log_round_trip_and_torn_tail(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "runs" / "x.jsonl")
    assert log.write_all(EVENTS) == len(EVENTS)
    log.write(closed(40 * D, "BTC", 1.0))
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"time_ms": 1, "kind": "cl')  # crash mid-write
    back = log.read()
    assert len(back) == len(EVENTS) + 1 and back[1] == EVENTS[1]
    assert from_dict(to_dict(EVENTS[0])) == EVENTS[0]
    assert to_dict(EVENTS[1])["time"] == "1970-01-02T00:00:00+00:00"
    assert EventLog(tmp_path / "missing.jsonl").read() == []


def test_walk_forward_splits_and_scores(store: CandleStore) -> None:
    assert split(0, 99, 4) == [(0, 24), (25, 49), (50, 74), (75, 99)]
    assert split(0, 99, 4, step_ms=10) == [(0, 29), (30, 49), (50, 79), (80, 99)]
    with pytest.raises(ValueError):
        split(10, 10, 1)
    wf = walk_forward(
        STRATEGIES / "pendulum",
        store,
        INSTRUMENTS,
        start_ms=350 * H,
        end_ms=599 * H,
        folds=2,
        env={"HL_WALLET": "0x1"},
    )
    assert [f.index for f in wf.folds] == [0, 1]
    assert wf.folds[0].end_ms + 1 == wf.folds[1].start_ms and wf.folds[1].end_ms == 599 * H
    assert all(f.result.initial_cash == 100.0 for f in wf.folds)
    assert 0 <= wf.profitable_folds <= 2 and wf.worst_fold_drawdown_pct >= 0
    r = 1.0
    for f in wf.folds:
        r *= 1 + f.metrics.return_pct / 100
    assert wf.compounded_return_pct == pytest.approx((r - 1) * 100)

    full = run_backtest(
        STRATEGIES / "pendulum",
        store,
        INSTRUMENTS,
        start_ms=350 * H,
        end_ms=599 * H,
        env={"HL_WALLET": "0x1"},
    )
    m = from_result(full)
    assert m.trades == len(full.closed_trades) and m.fees_paid == full.fees_paid
    assert m.final == full.final_account.account_value
