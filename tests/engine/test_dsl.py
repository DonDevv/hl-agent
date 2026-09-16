"""DSL exit engine — each rule from Senpi's runtime-concepts.md, LONG and SHORT."""

from __future__ import annotations

import pytest

from hl_agent.data.models import Direction
from hl_agent.engine.config import ConfigError, DslConfig, Phase1, Tier, TimeCut
from hl_agent.engine.dsl import (
    CloseReason,
    DslState,
    DslTick,
    price_at_roe,
    roe_pct,
    stop_price,
    tick,
)

MIN = 60_000
ENTRY = 100.0
LEV = 10.0

BALANCED = DslConfig(
    phase1=Phase1(max_loss_pct=8.0),
    tiers=(
        Tier(10, 30),
        Tier(20, 30),
        Tier(35, 50),
        Tier(60, 70),
        Tier(100, 85),
    ),
    hard_timeout=TimeCut(enabled=True, interval_minutes=4320),
    weak_peak_cut=TimeCut(enabled=True, interval_minutes=360, min_value=3.0),
)


def px(roe: float, direction: Direction = Direction.LONG) -> float:
    return price_at_roe(ENTRY, roe, direction, LEV)


def opened(direction: Direction = Direction.LONG) -> DslState:
    return DslState.open(ENTRY, direction, LEV, now_ms=0)


def run(
    cfg: DslConfig, path: list[tuple[float, int]], direction: Direction = Direction.LONG
) -> list[DslTick]:
    """Feed (roe, minute) points; returns each tick. Stops after a close."""
    state = opened(direction)
    out: list[DslTick] = []
    for roe, minute in path:
        t = tick(cfg, state, px(roe, direction), minute * MIN)
        out.append(t)
        state = t.state
        if t.close_reason:
            break
    return out


# ---- math -------------------------------------------------------------------


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_roe_and_price_roundtrip(direction: Direction) -> None:
    for roe in (-8.0, 0.0, 12.5, 100.0):
        p = price_at_roe(ENTRY, roe, direction, LEV)
        assert roe_pct(ENTRY, p, direction, LEV) == pytest.approx(roe)


def test_roe_sign_convention() -> None:
    assert roe_pct(100, 101, Direction.LONG, 10) == pytest.approx(10.0)
    assert roe_pct(100, 101, Direction.SHORT, 10) == pytest.approx(-10.0)


# ---- phase 1 ----------------------------------------------------------------


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_absolute_floor_breach_closes_on_first_touch(direction: Direction) -> None:
    ticks = run(BALANCED, [(2.0, 1), (-7.9, 2), (-8.0, 3)], direction)
    assert [t.close_reason for t in ticks] == [None, None, CloseReason.DSL_BREACH]


def test_phase1_stop_price_sits_at_max_loss() -> None:
    state = opened()
    assert stop_price(BALANCED, state) == pytest.approx(px(-8.0))
    # SHORT: floor is above entry
    assert stop_price(BALANCED, opened(Direction.SHORT)) == pytest.approx(px(-8.0, Direction.SHORT))
    assert stop_price(BALANCED, opened(Direction.SHORT)) > ENTRY


def test_consecutive_breaches_required_filters_wicks() -> None:
    cfg = DslConfig(phase1=Phase1(max_loss_pct=8.0, consecutive_breaches_required=3))
    ticks = run(cfg, [(-9, 1), (-9, 2), (-7, 3), (-9, 4), (-9, 5), (-9, 6)])
    reasons = [t.close_reason for t in ticks]
    assert reasons[:5] == [None] * 5  # counter reset at minute 3
    assert reasons[5] is CloseReason.DSL_BREACH
    assert ticks[2].state.breach_count == 0


def test_trailing_retrace_floor_when_enabled() -> None:
    cfg = DslConfig(phase1=Phase1(max_loss_pct=8.0, trailing_enabled=True, retrace_threshold=3.0))
    ticks = run(cfg, [(5.0, 1), (2.1, 2), (2.0, 3)])
    # high-water 5, retrace 3 → floor at +2 ROE, stricter than -8
    assert ticks[0].stop_price == pytest.approx(px(2.0))
    assert [t.close_reason for t in ticks] == [None, None, CloseReason.DSL_BREACH]


def test_trailing_disabled_by_default_never_ratchets_into_a_loss() -> None:
    # Senpi's documented live bug: +8.5% then a bounce closed a winner at -5.9%.
    ticks = run(BALANCED, [(8.5, 1), (-5.9, 2)])
    assert ticks[1].close_reason is None
    assert ticks[1].stop_price == pytest.approx(px(-8.0))


# ---- phase 2 ----------------------------------------------------------------


def test_first_tier_enters_phase2_and_locks_share_of_high_water() -> None:
    ticks = run(BALANCED, [(10.0, 1)])
    t = ticks[0]
    assert t.tier_advanced and t.state.tier_index == 0
    assert t.state.floor_roe == pytest.approx(3.0)  # 10 x 30%
    assert t.stop_price == pytest.approx(px(3.0))


def test_floor_only_ratchets_up_and_follows_high_water() -> None:
    ticks = run(BALANCED, [(10, 1), (18, 2), (12, 3), (22, 4), (36, 5)])
    floors = [t.state.floor_roe for t in ticks]
    # 10→3.0 ; 18→5.4 ; 12 keeps 5.4 ; 22 (tier 1, lock 30) → 6.6 ; 36 (tier 2, lock 50) → 18
    assert floors == pytest.approx([3.0, 5.4, 5.4, 6.6, 18.0])
    assert [t.state.tier_index for t in ticks] == [0, 0, 0, 1, 2]
    assert all(t.close_reason is None for t in ticks)


def test_senpi_worked_example() -> None:
    # Entry $100, 10x LONG, tier (10, 40): +10% → SL $100.40 ; hw 18% → $100.72
    cfg = DslConfig(phase1=Phase1(max_loss_pct=8.0), tiers=(Tier(10, 40), Tier(20, 70)))
    ticks = run(cfg, [(10, 1), (18, 2), (22, 3)])
    assert ticks[0].stop_price == pytest.approx(100.40)
    assert ticks[1].stop_price == pytest.approx(100.72)
    assert ticks[2].stop_price == pytest.approx(101.54)


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_phase2_floor_breach_reports_exchange_sl_hit(direction: Direction) -> None:
    ticks = run(BALANCED, [(12, 1), (3.5, 2), (3.6, 3)], direction)
    assert ticks[1].close_reason is CloseReason.EXCHANGE_SL_HIT
    assert len(ticks) == 2


def test_multiple_tiers_can_be_skipped_in_one_tick() -> None:
    ticks = run(BALANCED, [(65, 1)])
    assert ticks[0].state.tier_index == 3  # trigger 60
    assert ticks[0].state.floor_roe == pytest.approx(65 * 0.70)


# ---- time cuts --------------------------------------------------------------


def test_hard_timeout_fires_in_either_phase_but_not_on_entering_tick() -> None:
    cfg = DslConfig(
        phase1=Phase1(max_loss_pct=8.0),
        tiers=(Tier(10, 30),),
        hard_timeout=TimeCut(enabled=True, interval_minutes=60),
    )
    # phase 1 timeout
    assert run(cfg, [(1, 59), (1, 60)])[-1].close_reason is CloseReason.HARD_TIMEOUT
    # entering phase 2 exactly at the deadline: tier wins, next tick times out
    ticks = run(cfg, [(1, 30), (10, 60), (11, 61)])
    assert ticks[1].close_reason is None and ticks[1].tier_advanced
    assert ticks[2].close_reason is CloseReason.HARD_TIMEOUT


def test_weak_peak_cut_only_when_peak_below_min_and_fading() -> None:
    cfg = DslConfig(
        phase1=Phase1(max_loss_pct=8.0),
        weak_peak_cut=TimeCut(enabled=True, interval_minutes=360, min_value=3.0),
    )
    # too early
    assert run(cfg, [(2, 100), (1, 200)])[-1].close_reason is None
    # after 6h, peak 2 < 3, fading → cut
    assert run(cfg, [(2, 100), (1, 360)])[-1].close_reason is CloseReason.WEAK_PEAK_CUT
    # after 6h but still at peak → no cut
    assert run(cfg, [(2, 100), (2, 360)])[-1].close_reason is None
    # peak reached 3 → never cuts
    assert run(cfg, [(3, 100), (1, 400)])[-1].close_reason is None


def test_dead_weight_cut_resets_on_positive_tick() -> None:
    cfg = DslConfig(
        phase1=Phase1(max_loss_pct=8.0),
        dead_weight_cut=TimeCut(enabled=True, interval_minutes=45),
    )
    assert run(cfg, [(-1, 10), (-2, 44)])[-1].close_reason is None
    assert run(cfg, [(-1, 10), (-2, 45)])[-1].close_reason is CloseReason.DEAD_WEIGHT_CUT
    # a positive tick at 30 resets the timer: 30 → 75 needed
    ticks = run(cfg, [(-1, 10), (0.5, 30), (-1, 60), (-1, 75)])
    assert [t.close_reason for t in ticks] == [None, None, None, CloseReason.DEAD_WEIGHT_CUT]


def test_time_cuts_take_precedence_over_breach_in_documented_order() -> None:
    cfg = DslConfig(
        phase1=Phase1(max_loss_pct=8.0),
        hard_timeout=TimeCut(enabled=True, interval_minutes=10),
        dead_weight_cut=TimeCut(enabled=True, interval_minutes=5),
    )
    # minute 10, roe -9: hard_timeout (first in order) wins over dead_weight and breach
    assert run(cfg, [(-9, 10)])[-1].close_reason is CloseReason.HARD_TIMEOUT
    # minute 6, roe -9: dead_weight beats breach
    assert run(cfg, [(-9, 6)])[-1].close_reason is CloseReason.DEAD_WEIGHT_CUT


# ---- config validation --------------------------------------------------------


def test_config_rejects_unsorted_tiers_and_zero_lock() -> None:
    with pytest.raises(ConfigError):
        DslConfig(phase1=Phase1(8), tiers=(Tier(20, 30), Tier(10, 30)))
    with pytest.raises(ConfigError):
        Tier(10, 0)
    Tier(300, 92)  # catalog strategies ladder well past +100% ROE
    with pytest.raises(ConfigError):
        DslConfig(phase1=Phase1(8), weak_peak_cut=TimeCut(enabled=True, interval_minutes=10))
