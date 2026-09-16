from __future__ import annotations

import pytest

from hl_agent.data.models import Direction, Instrument
from hl_agent.engine.config import StrategyConfig
from hl_agent.engine.dedup import DedupState
from hl_agent.engine.guardrails import GateReason
from hl_agent.engine.signals import Signal
from hl_agent.engine.sizing import OrderPlan, plan_order, truncate_size

BTC = Instrument(name="BTC", size_decimals=5, max_leverage=40)
CFG = StrategyConfig(slots=2, margin_pct=20.0, default_leverage=2, max_leverage=3)


def sig(**kw: object) -> Signal:
    base: dict[str, object] = {
        "asset": "BTC",
        "direction": Direction.LONG,
        "scanner": "s",
        "produced_at_ms": 0,
        "valid_until_ms": 10_000,
        "signal_id": "id1",
    }
    base.update(kw)
    return Signal(**base)  # type: ignore[arg-type]


def test_senpi_formula_margin_pct_of_withdrawable_times_leverage() -> None:
    plan = plan_order(
        CFG,
        sig(margin_pct=25.0, leverage=2.0),
        BTC,
        price=50_000.0,
        withdrawable=100.0,
        free_margin=100.0,
    )
    assert isinstance(plan, OrderPlan)
    assert plan.margin_usd == pytest.approx(25.0, rel=1e-3)
    assert plan.notional_usd == pytest.approx(50.0, rel=1e-3)
    assert plan.size == 0.001 and plan.leverage == 2


def test_fallbacks_and_leverage_clamp() -> None:
    plan = plan_order(
        CFG, sig(leverage=10.0), BTC, price=50_000.0, withdrawable=100.0, free_margin=100.0
    )
    assert isinstance(plan, OrderPlan)
    assert plan.leverage == 3  # global cap wins over the signal and the instrument's 40x
    plan = plan_order(CFG, sig(), BTC, price=50_000.0, withdrawable=100.0, free_margin=100.0)
    assert isinstance(plan, OrderPlan)
    assert plan.leverage == 2 and plan.margin_usd == pytest.approx(20.0, rel=1e-2)


def test_notional_and_margin_gates() -> None:
    small = plan_order(
        CFG, sig(margin_pct=4.0), BTC, price=50_000.0, withdrawable=100.0, free_margin=100.0
    )
    assert small is GateReason.RISK_GATE_NOTIONAL  # 4 x 2 = $8 < $10
    broke = plan_order(
        CFG, sig(margin_pct=50.0), BTC, price=50_000.0, withdrawable=100.0, free_margin=30.0
    )
    assert broke is GateReason.NO_MARGIN
    assert (
        plan_order(CFG, sig(), BTC, price=0.0, withdrawable=100.0, free_margin=100.0)
        is GateReason.SIGNAL_NOT_READY
    )


def test_truncate_never_rounds_up() -> None:
    assert truncate_size(0.0019999, 3) == 0.001
    assert truncate_size(1.23456789, 4) == 1.2345
    assert truncate_size(0.3, 1) == 0.3  # float noise guard


def test_dedup_by_id_then_by_high_water() -> None:
    d = DedupState()
    first = sig(signal_id="a", produced_at_ms=0)
    assert not d.is_duplicate(first, window_ms=1000)
    d = d.remember(first)
    assert d.is_duplicate(sig(signal_id="a", produced_at_ms=5000), window_ms=1000)  # replay
    assert d.is_duplicate(sig(signal_id="b", produced_at_ms=999), window_ms=1000)  # same setup
    assert not d.is_duplicate(
        sig(signal_id="c", produced_at_ms=1000), window_ms=1000
    )  # window over
    assert not d.is_duplicate(
        sig(signal_id="d", produced_at_ms=10, direction=Direction.SHORT), window_ms=1000
    )
    assert not d.is_duplicate(
        sig(signal_id="e", produced_at_ms=10, scanner="other"), window_ms=1000
    )
