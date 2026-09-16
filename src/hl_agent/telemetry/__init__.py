"""Telemetry: JSONL event log, performance metrics and reports."""

from hl_agent.telemetry.events import EventLog, from_dict, to_dict
from hl_agent.telemetry.metrics import Drawdown, Metrics, Trade, compute, drawdown, from_result
from hl_agent.telemetry.report import render_comparison, render_text, to_json

__all__ = [
    "Drawdown",
    "EventLog",
    "Metrics",
    "Trade",
    "compute",
    "drawdown",
    "from_dict",
    "from_result",
    "render_comparison",
    "render_text",
    "to_dict",
    "to_json",
]
