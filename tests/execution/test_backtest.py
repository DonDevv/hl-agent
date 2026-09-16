from __future__ import annotations

import pytest

from hl_agent.data.history import CandleStore
from hl_agent.execution.backtest import prepare, run, run_backtest
from hl_agent.execution.sim import SimConfig
from hl_agent.strategy.sources import ReplaySource
from tests.strategy.conftest import INSTRUMENTS, STRATEGIES, H

ENV = {"HL_WALLET": "0x1"}


def test_backtest_is_cash_consistent_and_flattens(store: CandleStore) -> None:
    res = run_backtest(
        STRATEGIES / "pendulum",
        store,
        INSTRUMENTS,
        start_ms=350 * H,
        end_ms=599 * H,
        env=ENV,
    )
    assert (
        res.strategy.startswith("pendulum") and len(res.equity) == 251
    )  # 250 ticks + the flattened close
    kinds = {e.kind for e in res.events}
    assert "opened" in kinds and "closed" in kinds
    assert res.final_account.positions == ()  # flattened
    net_from_trades = sum(e.payload["pnl_usd"] for e in res.closed_trades)
    assert res.net_pnl == pytest.approx(net_from_trades - res.funding_paid)
    assert res.fees_paid > 0
    for e in res.closed_trades:
        assert e.payload["pnl_usd"] == pytest.approx(
            (e.payload["exit_price"] - e.payload["entry_price"])
            * (1 if e.payload["direction"] == "LONG" else -1)
            * e.payload["size"],
            abs=0.05,
        )


def test_stop_hits_surface_with_exchange_reason(store: CandleStore) -> None:
    res = run_backtest(
        STRATEGIES / "compass",
        store,
        INSTRUMENTS,
        start_ms=300 * H,
        end_ms=599 * H,
        sim=SimConfig(slippage_bps=0),
        env=ENV,
        flatten_at_end=False,
    )
    reasons = {e.reason for e in res.closed_trades}
    assert reasons <= {"exchange_sl_hit", "hard_timeout", "weak_peak_cut", "dsl_breach"}
    assert any(e.kind == "stop_moved" for e in res.events) or "exchange_sl_hit" in reasons
    if res.final_account.positions:
        assert res.net_pnl == pytest.approx(res.final_account.account_value - res.initial_cash)


def test_prepare_then_run_is_resumable(store: CandleStore) -> None:
    setup = prepare(STRATEGIES / "pendulum", store, INSTRUMENTS, start_ms=350 * H, env=ENV)
    assert isinstance(setup.source, ReplaySource) and setup.source.now_ms == 350 * H
    first = run(setup, end_ms=450 * H, flatten_at_end=False)
    second = run(setup, end_ms=599 * H)
    assert len(first.equity) == 102 and second.start_ms == 451 * H
    assert len(second.equity) == 150 and second.final_account.positions == ()


def test_off_grid_start_snaps_to_the_bar_grid(store: CandleStore) -> None:
    """A tick at hh:51 would price off a bar 51 minutes stale while scanners already see
    fresher sub-hourly bars: that is look-ahead, so ticks always sit on step boundaries."""
    res = run_backtest(
        STRATEGIES / "pendulum",
        store,
        INSTRUMENTS,
        start_ms=350 * H + 1234,
        end_ms=360 * H,
        env=ENV,
    )
    assert res.start_ms == 351 * H and res.equity[0].time_ms == 351 * H
    assert all(p.time_ms % H == 0 for p in res.equity)
