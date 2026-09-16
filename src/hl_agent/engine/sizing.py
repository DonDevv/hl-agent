"""Position sizing — Senpi's formula, nothing more.

``margin = marginPct / 100 x withdrawable`` (falls back to ``strategy.margin_pct``),
``leverage = signal.leverage`` (falls back to ``strategy.default_leverage``), clamped to
``min(strategy.max_leverage, instrument.max_leverage)``; ``notional = margin x leverage``.
A plan below ``min_notional_usd`` is ``risk_gate_notional``; margin above free margin is
``no_margin``. Size is truncated (never rounded up) to the instrument's ``szDecimals``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_agent.data.models import Direction, Instrument
from hl_agent.engine.config import StrategyConfig
from hl_agent.engine.guardrails import GateReason
from hl_agent.engine.signals import Signal


@dataclass(frozen=True, slots=True)
class OrderPlan:
    asset: str
    direction: Direction
    size: float  # contracts, truncated to szDecimals
    leverage: int
    margin_usd: float
    notional_usd: float
    reference_price: float


def truncate_size(size: float, decimals: int) -> float:
    factor: int = 10**decimals
    return math.floor(size * factor + 1e-9) / factor


def plan_order(
    cfg: StrategyConfig,
    signal: Signal,
    instrument: Instrument,
    *,
    price: float,
    withdrawable: float,
    free_margin: float,
) -> OrderPlan | GateReason:
    if price <= 0:
        return GateReason.SIGNAL_NOT_READY

    requested = signal.leverage if signal.leverage is not None else float(cfg.default_leverage)
    cap = min(cfg.max_leverage, instrument.max_leverage)
    leverage = max(1, min(int(requested), cap))

    margin_pct = signal.margin_pct if signal.margin_pct is not None else cfg.margin_pct
    margin = margin_pct / 100.0 * withdrawable
    notional = margin * leverage
    size = truncate_size(notional / price, instrument.size_decimals)
    actual_notional = size * price

    if size <= 0 or actual_notional < cfg.min_notional_usd:
        return GateReason.RISK_GATE_NOTIONAL
    if margin > free_margin + 1e-9:
        return GateReason.NO_MARGIN

    return OrderPlan(
        asset=signal.asset,
        direction=signal.direction,
        size=size,
        leverage=leverage,
        margin_usd=actual_notional / leverage,
        notional_usd=actual_notional,
        reference_price=price,
    )
