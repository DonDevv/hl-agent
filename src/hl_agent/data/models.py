"""Typed, immutable market and account models.

All numeric conversion from the Hyperliquid API (which serves numbers as strings)
happens in the ``from_api`` constructors here and nowhere else. Downstream code
only ever sees ``float`` / ``int``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Interval = str  # "1m" | "5m" | "15m" | "1h" | "4h" | "1d" — Hyperliquid interval names


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @classmethod
    def parse(cls, value: str) -> Direction:
        return cls(value.upper())

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar. ``open_ms`` / ``close_ms`` are epoch milliseconds."""

    asset: str
    interval: Interval
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Candle:
        return cls(
            asset=str(raw["s"]),
            interval=str(raw["i"]),
            open_ms=int(raw["t"]),
            close_ms=int(raw["T"]),
            open=float(raw["o"]),
            high=float(raw["h"]),
            low=float(raw["l"]),
            close=float(raw["c"]),
            volume=float(raw["v"]),
            trades=int(raw.get("n", 0)),
        )

    def to_senpi(self) -> dict[str, Any]:
        """Shape expected by Senpi ``scan.py`` files (``t/T/o/h/l/c/v/n``)."""
        return {
            "t": self.open_ms,
            "T": self.close_ms,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "n": self.trades,
            "s": self.asset,
            "i": self.interval,
        }


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable perpetual. ``name`` is the bare coin (``BTC``) or dex-prefixed
    (``xyz:NVDA``) exactly as Hyperliquid and Senpi spell it."""

    name: str
    size_decimals: int
    max_leverage: int
    only_isolated: bool = False
    delisted: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any], dex: str = "") -> Instrument:
        name = str(raw["name"])
        if dex:
            name = f"{dex}:{name}"
        return cls(
            name=name,
            size_decimals=int(raw["szDecimals"]),
            max_leverage=int(raw["maxLeverage"]),
            only_isolated=bool(raw.get("onlyIsolated", False)),
            delisted=bool(raw.get("isDelisted", False)),
        )


@dataclass(frozen=True, slots=True)
class AssetContext:
    """Live venue context for one instrument (from ``metaAndAssetCtxs``)."""

    instrument: Instrument
    mark_price: float
    oracle_price: float
    funding_rate: float  # current hourly rate, as a fraction
    open_interest: float
    day_notional_volume: float
    prev_day_price: float

    @property
    def name(self) -> str:
        return self.instrument.name

    @classmethod
    def from_api(cls, instrument: Instrument, raw: dict[str, Any]) -> AssetContext:
        return cls(
            instrument=instrument,
            mark_price=float(raw["markPx"]),
            oracle_price=float(raw.get("oraclePx", raw["markPx"])),
            funding_rate=float(raw.get("funding", 0.0)),
            open_interest=float(raw.get("openInterest", 0.0)),
            day_notional_volume=float(raw.get("dayNtlVlm", 0.0)),
            prev_day_price=float(raw.get("prevDayPx", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class FundingRate:
    asset: str
    time_ms: int
    rate: float  # per funding interval (hourly on Hyperliquid), as a fraction
    premium: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> FundingRate:
        return cls(
            asset=str(raw["coin"]),
            time_ms=int(raw["time"]),
            rate=float(raw["fundingRate"]),
            premium=float(raw.get("premium", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class Position:
    asset: str
    direction: Direction
    size: float  # absolute contracts
    entry_price: float
    leverage: int
    margin_used: float
    unrealized_pnl: float
    liquidation_price: float | None
    roe_pct: float  # return on equity, in percent (Senpi/Hyperliquid convention)

    @property
    def notional(self) -> float:
        return self.size * self.entry_price

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Position:
        pos = raw["position"]
        signed = float(pos["szi"])
        lev = pos.get("leverage", {})
        return cls(
            asset=str(pos["coin"]),
            direction=Direction.LONG if signed > 0 else Direction.SHORT,
            size=abs(signed),
            entry_price=float(pos["entryPx"]),
            leverage=int(lev.get("value", 1)),
            margin_used=float(pos["marginUsed"]),
            unrealized_pnl=float(pos["unrealizedPnl"]),
            liquidation_price=(
                float(pos["liquidationPx"]) if pos.get("liquidationPx") is not None else None
            ),
            roe_pct=float(pos.get("returnOnEquity", 0.0)) * 100.0,
        )


@dataclass(frozen=True, slots=True)
class AccountState:
    account_value: float
    withdrawable: float
    total_margin_used: float
    positions: tuple[Position, ...]

    @property
    def free_margin(self) -> float:
        return self.account_value - self.total_margin_used

    def position(self, asset: str) -> Position | None:
        return next((p for p in self.positions if p.asset == asset), None)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> AccountState:
        summary = raw["marginSummary"]
        return cls(
            account_value=float(summary["accountValue"]),
            withdrawable=float(raw["withdrawable"]),
            total_margin_used=float(summary["totalMarginUsed"]),
            positions=tuple(
                Position.from_api(p)
                for p in raw.get("assetPositions", [])
                if float(p["position"]["szi"]) != 0.0
            ),
        )
