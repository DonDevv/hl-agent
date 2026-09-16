"""Engine layer: pure decision logic shared by backtest and live (see ``loop.Engine``)."""

from hl_agent.engine.config import (
    ConfigError,
    DslConfig,
    GuardRails,
    Phase1,
    StrategyConfig,
    Tier,
    TimeCut,
)
from hl_agent.engine.dsl import CloseReason, DslState, DslTick, roe_pct, stop_price, tick
from hl_agent.engine.guardrails import GateReason, GuardRailState
from hl_agent.engine.loop import Engine, EngineConfig, Event, Tracked
from hl_agent.engine.ports import Broker, Fill, MarketView, SignalSource
from hl_agent.engine.signals import InvalidSignalError, Signal, parse_signal
from hl_agent.engine.sizing import OrderPlan, plan_order

__all__ = [
    "Broker",
    "CloseReason",
    "ConfigError",
    "DslConfig",
    "DslState",
    "DslTick",
    "Engine",
    "EngineConfig",
    "Event",
    "Fill",
    "GateReason",
    "GuardRailState",
    "GuardRails",
    "InvalidSignalError",
    "MarketView",
    "OrderPlan",
    "Phase1",
    "Signal",
    "SignalSource",
    "StrategyConfig",
    "Tier",
    "TimeCut",
    "Tracked",
    "parse_signal",
    "plan_order",
    "roe_pct",
    "stop_price",
    "tick",
]
