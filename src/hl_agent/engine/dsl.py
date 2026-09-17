"""Dynamic Stop-Loss exit engine — a pure state machine.

Behaviour reproduces Senpi's runtime DSL (``runtime-concepts.md``):

* **Phase 1 (survive)** — an absolute floor at ``-max_loss_pct`` ROE, optionally a trailing
  retrace floor; the stricter one applies. A tick at/through the floor counts a breach; at
  ``consecutive_breaches_required`` the position closes (``dsl_breach``).
* **Phase 2 (lock)** — once high-water ROE crosses a tier's ``trigger_pct`` the floor becomes
  ``high_water_roe x lock_hw_pct / 100`` and only ever ratchets up. The floor is meant to sit
  on the exchange as a stop order; the broker reports ``exchange_sl_hit`` when it fills.
* **Time cuts** (``hard_timeout``, ``weak_peak_cut``, ``dead_weight_cut``) run in either phase.

Evaluation order per tick: hard_timeout → dead_weight_cut → weak_peak_cut → phase-1 breach →
phase-2 tier advance. The one exception: on the tick that first enters Phase 2 the tier advance
wins over ``hard_timeout``.

ROE convention (Hyperliquid/Senpi): ``roe% = (price / entry - 1) x sign x leverage x 100``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from hl_agent.data.models import Direction
from hl_agent.engine.config import DslConfig


class CloseReason(StrEnum):
    DSL_BREACH = "dsl_breach"
    EXCHANGE_SL_HIT = "exchange_sl_hit"
    HARD_TIMEOUT = "hard_timeout"
    WEAK_PEAK_CUT = "weak_peak_cut"
    DEAD_WEIGHT_CUT = "dead_weight_cut"
    FLIPPED = "flipped"
    MANUAL_CLOSE = "manual_close"
    SOURCE_CLOSED = "source_closed"  # the signal source asked (copy-trading: the trader exited)
    CLOSED_EXTERNALLY = "closed_externally"
    LIQUIDATED = "liquidated"


_ROE_DECIMALS = 9  # absorb float noise so threshold comparisons are exact at boundaries


def roe_pct(entry: float, price: float, direction: Direction, leverage: float) -> float:
    return round((price / entry - 1.0) * direction.sign * leverage * 100.0, _ROE_DECIMALS)


def price_at_roe(entry: float, roe: float, direction: Direction, leverage: float) -> float:
    return entry * (1.0 + direction.sign * roe / 100.0 / leverage)


@dataclass(frozen=True, slots=True)
class DslState:
    """Everything the engine remembers about one tracked position."""

    entry_price: float
    direction: Direction
    leverage: float
    opened_at_ms: int
    high_water_roe: float = 0.0
    tier_index: int = -1  # -1 = Phase 1
    breach_count: int = 0
    floor_roe: float | None = None  # Phase 2 locked floor (ROE%), ratchets up only
    dead_weight_since_ms: int | None = None

    @property
    def in_phase2(self) -> bool:
        return self.tier_index >= 0

    @classmethod
    def open(
        cls, entry_price: float, direction: Direction, leverage: float, now_ms: int
    ) -> DslState:
        return cls(
            entry_price=entry_price,
            direction=direction,
            leverage=leverage,
            opened_at_ms=now_ms,
            dead_weight_since_ms=now_ms,
        )


@dataclass(frozen=True, slots=True)
class DslTick:
    state: DslState
    close_reason: CloseReason | None
    stop_price: float  # where the exchange stop should sit after this tick
    tier_advanced: bool
    roe: float


def _phase1_floor_roe(cfg: DslConfig, state: DslState) -> float:
    floor = -cfg.phase1.max_loss_pct
    if cfg.phase1.trailing_enabled:
        floor = max(floor, state.high_water_roe - cfg.phase1.retrace_threshold)
    return floor


def stop_price(cfg: DslConfig, state: DslState) -> float:
    """Current effective floor as a price (absolute floor in P1, locked floor in P2)."""
    floor = state.floor_roe if state.in_phase2 and state.floor_roe is not None else None
    if floor is None:
        floor = _phase1_floor_roe(cfg, state)
    return price_at_roe(state.entry_price, floor, state.direction, state.leverage)


def _highest_armed_tier(cfg: DslConfig, high_water_roe: float) -> int:
    idx = -1
    for i, tier in enumerate(cfg.tiers):
        if high_water_roe >= tier.trigger_pct:
            idx = i
    return idx


def tick(cfg: DslConfig, state: DslState, price: float, now_ms: int) -> DslTick:
    roe = roe_pct(state.entry_price, price, state.direction, state.leverage)
    hw = max(state.high_water_roe, roe)
    minutes_open = (now_ms - state.opened_at_ms) / 60_000.0

    # dead-weight timer resets on any positive tick
    dw_since = now_ms if roe > 0 else state.dead_weight_since_ms
    state = replace(state, high_water_roe=hw, dead_weight_since_ms=dw_since)

    # ---- phase-2 tier (computed first: the entering tick wins over hard_timeout) ----
    new_tier = max(state.tier_index, _highest_armed_tier(cfg, hw))
    entering_phase2 = new_tier >= 0 and not state.in_phase2
    advanced = new_tier > state.tier_index
    if new_tier >= 0:
        locked = hw * cfg.tiers[new_tier].lock_hw_pct / 100.0
        floor = max(locked, state.floor_roe) if state.floor_roe is not None else locked
        state = replace(state, tier_index=new_tier, floor_roe=floor, breach_count=0)

    def done(reason: CloseReason | None) -> DslTick:
        return DslTick(state, reason, stop_price(cfg, state), advanced, roe)

    # ---- time cuts (either phase) ----
    ht = cfg.hard_timeout
    if ht.enabled and minutes_open >= ht.interval_minutes and not entering_phase2:
        return done(CloseReason.HARD_TIMEOUT)

    dw = cfg.dead_weight_cut
    if dw.enabled and roe <= 0 and state.dead_weight_since_ms is not None:
        underwater_minutes = (now_ms - state.dead_weight_since_ms) / 60_000.0
        if underwater_minutes >= dw.interval_minutes:
            return done(CloseReason.DEAD_WEIGHT_CUT)

    wp = cfg.weak_peak_cut
    if wp.enabled and minutes_open >= wp.interval_minutes and hw < wp.min_value and roe < hw:
        return done(CloseReason.WEAK_PEAK_CUT)

    # ---- phase 2: exchange stop owns the exit; we only report a breach if polled through it ----
    if state.in_phase2:
        assert state.floor_roe is not None
        if roe <= state.floor_roe:
            return done(CloseReason.EXCHANGE_SL_HIT)
        return done(None)

    # ---- phase 1: breach counting against the stricter floor ----
    floor = _phase1_floor_roe(cfg, state)
    if roe <= floor:
        breaches = state.breach_count + 1
        state = replace(state, breach_count=breaches)
        if breaches >= cfg.phase1.consecutive_breaches_required:
            return done(CloseReason.DSL_BREACH)
        return done(None)
    state = replace(state, breach_count=0)
    return done(None)
