"""Ports: the only three things the engine needs from the outside world.

The backtester (Axe 4) and the live runner implement these against a candle replay or
the Hyperliquid API respectively; the engine never knows which. Everything here is a
``Protocol`` so tests can pass plain fakes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from hl_agent.data.models import AccountState, Direction, Instrument
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.guardrails import Book
from hl_agent.engine.signals import Signal
from hl_agent.engine.sizing import OrderPlan


@dataclass(frozen=True, slots=True)
class Fill:
    asset: str
    direction: Direction  # direction of the resulting/previous position, not of the trade
    size: float
    price: float
    fee_usd: float
    time_ms: int


class MarketView(Protocol):
    def price(self, asset: str) -> float | None: ...
    def instrument(self, asset: str) -> Instrument | None: ...
    def account(self) -> AccountState: ...

    def order_book(self, asset: str) -> Book | None:
        """L2 snapshot for the liquidity gate; ``None`` when the venue has none (backtest)."""
        ...


class SignalSource(Protocol):
    """Whatever produces validated ``Signal`` objects for this tick (Axe 2 strategies)."""

    def signals(self, now_ms: int) -> Sequence[Signal]: ...


class ExitSource(Protocol):
    """A source that can also ask for exits (copy-trading: the mirrored trader closed).
    Assets the engine does not track are ignored; each request is consumed once."""

    def close_requests(self, now_ms: int) -> Sequence[str]: ...


class Broker(Protocol):
    def open(self, plan: OrderPlan, stop_price: float, now_ms: int) -> Fill: ...
    def close(self, asset: str, reason: CloseReason, now_ms: int) -> Fill: ...
    def set_stop(self, asset: str, stop_price: float, now_ms: int) -> None: ...

    def external_close(self, asset: str, now_ms: int) -> tuple[CloseReason, Fill] | None:
        """How the venue closed ``asset`` on its own (stop order filled, liquidation), if it
        knows; ``None`` means the engine reports a generic ``closed_externally``."""
        ...
