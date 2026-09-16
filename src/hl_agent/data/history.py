"""Local candle history: incremental download to Parquet, offline reads.

One file per ``(asset, interval)`` under ``cache_dir``. Downloads resume from the
last stored bar, so re-running ``sync`` is cheap.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from hl_agent.data.hyperliquid_client import INTERVAL_MS, HyperliquidClient
from hl_agent.data.models import Candle, Interval

_SCHEMA = {
    "open_ms": pl.Int64,
    "close_ms": pl.Int64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "trades": pl.Int64,
}


def _to_frame(asset: str, interval: Interval, candles: Sequence[Candle]) -> pl.DataFrame:
    if not candles:
        return pl.DataFrame(schema=_SCHEMA)
    return pl.DataFrame(
        {
            "open_ms": [c.open_ms for c in candles],
            "close_ms": [c.close_ms for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
            "trades": [c.trades for c in candles],
        },
        schema=_SCHEMA,
    )


def _from_frame(asset: str, interval: Interval, frame: pl.DataFrame) -> list[Candle]:
    return [Candle(asset=asset, interval=interval, **row) for row in frame.iter_rows(named=True)]


class CandleStore:
    def __init__(self, cache_dir: Path | str) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def path(self, asset: str, interval: Interval) -> Path:
        safe = asset.replace(":", "_")
        return self._dir / f"{safe}_{interval}.parquet"

    # ---- reads -------------------------------------------------------------

    def frame(self, asset: str, interval: Interval) -> pl.DataFrame:
        p = self.path(asset, interval)
        if not p.exists():
            return pl.DataFrame(schema=_SCHEMA)
        return pl.read_parquet(p)

    def load(
        self,
        asset: str,
        interval: Interval,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Candle]:
        frame = self.frame(asset, interval)
        if start_ms is not None:
            frame = frame.filter(pl.col("open_ms") >= start_ms)
        if end_ms is not None:
            frame = frame.filter(pl.col("open_ms") <= end_ms)
        return _from_frame(asset, interval, frame.sort("open_ms"))

    def last_open_ms(self, asset: str, interval: Interval) -> int | None:
        frame = self.frame(asset, interval)
        if frame.is_empty():
            return None
        return int(frame["open_ms"].max())  # type: ignore[arg-type]

    # ---- writes ------------------------------------------------------------

    def append(self, asset: str, interval: Interval, candles: Sequence[Candle]) -> int:
        """Merge new candles in; returns the number of bars added."""
        incoming = _to_frame(asset, interval, candles)
        if incoming.is_empty():
            return 0
        existing = self.frame(asset, interval)
        before = existing.height
        merged = (
            pl.concat([existing, incoming]).unique(subset=["open_ms"], keep="last").sort("open_ms")
        )
        merged.write_parquet(self.path(asset, interval))
        return merged.height - before

    def sync(
        self,
        client: HyperliquidClient,
        asset: str,
        interval: Interval,
        since: datetime | int,
        until: datetime | int | None = None,
    ) -> int:
        """Download everything missing between ``since`` and ``until`` (default: now)."""
        start = _ms(since)
        last = self.last_open_ms(asset, interval)
        if last is not None:
            # Re-fetch the last stored bar: it may have been in-progress when stored.
            start = max(start, last)
        end = _ms(until) if until is not None else None
        fresh = client.candles(asset, interval, start, end)
        return self.append(asset, interval, fresh)


def _ms(value: datetime | int) -> int:
    if isinstance(value, int):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def bars_between(interval: Interval, start_ms: int, end_ms: int) -> int:
    return max(0, (end_ms - start_ms) // INTERVAL_MS[interval])
