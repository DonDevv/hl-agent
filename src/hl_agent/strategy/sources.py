"""``MarketSource`` implementations that need no exchange connection.

``ReplaySource`` serves the local Parquet candle history *as of* a movable clock: a scanner
asking for the last 300 4h bars at ``now_ms`` only ever sees bars that had closed by then.
Account state comes from whoever owns the simulated wallet (the sim broker in Axe 4), so it
is injected as a callable.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping, Sequence

from hl_agent.data.history import CandleStore
from hl_agent.data.models import AccountState, AssetContext, Candle, FundingRate, Instrument


class ReplaySource:
    def __init__(
        self,
        store: CandleStore,
        instruments: Sequence[Instrument],
        account: Callable[[], AccountState],
        *,
        price_interval: str = "1h",
    ) -> None:
        self._store = store
        self._instruments = {i.name: i for i in instruments}
        self._account = account
        self._price_interval = price_interval
        self._cache: dict[tuple[str, str], list[Candle]] = {}
        self._closes: dict[tuple[str, str], list[int]] = {}  # close_ms, ascending, for bisect
        self.now_ms = 0

    @property
    def price_interval(self) -> str:
        return self._price_interval

    def _series(self, asset: str, interval: str) -> list[Candle]:
        key = (asset, interval)
        if key not in self._cache:
            self._cache[key] = self._store.load(asset, interval)
            self._closes[key] = [c.close_ms for c in self._cache[key]]
        return self._cache[key]

    def _closed_before(self, asset: str, interval: str, at_ms: int) -> int:
        """Count of bars closed at or before ``at_ms`` (the series is sorted by open_ms and
        bars do not overlap, so close_ms is sorted too)."""
        series = self._series(asset, interval)
        return bisect.bisect_right(self._closes[(asset, interval)], at_ms) if series else 0

    # ---- MarketSource ---------------------------------------------------------------

    def candles(self, asset: str, interval: str, limit: int) -> Sequence[Candle]:
        series = self._series(asset, interval)
        n = self._closed_before(asset, interval, self.now_ms)
        return series[max(0, n - limit) : n] if limit > 0 else series[:n]

    def price(self, asset: str) -> float | None:
        """Last close at or before the clock — the mark price a replay can honestly claim."""
        bars = self.candles(asset, self._price_interval, 1)
        return bars[-1].close if bars else None

    def asset_context(self, asset: str) -> AssetContext | None:
        inst = self._instruments.get(asset)
        px = self.price(asset)
        if inst is None or px is None:
            return None
        series = self._series(asset, self._price_interval)
        n_day_ago = self._closed_before(asset, self._price_interval, self.now_ms - 86_400_000)
        prev_day = series[n_day_ago - 1].close if n_day_ago else px
        return AssetContext(
            instrument=inst,
            mark_price=px,
            oracle_price=px,
            funding_rate=0.0,
            open_interest=0.0,
            day_notional_volume=sum(
                c.volume * c.close for c in self.candles(asset, self._price_interval, 24)
            ),
            prev_day_price=prev_day,
        )

    def instruments(self, dex: str = "") -> Sequence[Instrument]:
        prefix = f"{dex}:" if dex else ""
        return [
            i
            for i in self._instruments.values()
            if (i.name.startswith(prefix) if dex else ":" not in i.name)
        ]

    def mids(self, dex: str = "") -> Mapping[str, float]:
        out: dict[str, float] = {}
        for inst in self.instruments(dex):
            px = self.price(inst.name)
            if px is not None:
                out[inst.name] = px
        return out

    def account(self) -> AccountState:
        return self._account()

    def funding_history(self, asset: str, limit: int) -> Sequence[FundingRate]:
        return []
