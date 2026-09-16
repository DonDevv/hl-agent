"""``MarketSource`` implementations that need no exchange connection.

``ReplaySource`` serves the local Parquet candle history *as of* a movable clock: a scanner
asking for the last 300 4h bars at ``now_ms`` only ever sees bars that had closed by then.
Account state comes from whoever owns the simulated wallet (the sim broker in Axe 4), so it
is injected as a callable.
"""

from __future__ import annotations

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
        self.now_ms = 0

    @property
    def price_interval(self) -> str:
        return self._price_interval

    def _series(self, asset: str, interval: str) -> list[Candle]:
        key = (asset, interval)
        if key not in self._cache:
            self._cache[key] = self._store.load(asset, interval)
        return self._cache[key]

    # ---- MarketSource ---------------------------------------------------------------

    def candles(self, asset: str, interval: str, limit: int) -> Sequence[Candle]:
        series = self._series(asset, interval)
        closed = [c for c in series if c.close_ms <= self.now_ms]
        return closed[-limit:] if limit > 0 else closed

    def price(self, asset: str) -> float | None:
        """Last close at or before the clock — the mark price a replay can honestly claim."""
        bars = self.candles(asset, self._price_interval, 1)
        return bars[-1].close if bars else None

    def asset_context(self, asset: str) -> AssetContext | None:
        inst = self._instruments.get(asset)
        px = self.price(asset)
        if inst is None or px is None:
            return None
        day_ago = [
            c
            for c in self._series(asset, self._price_interval)
            if c.close_ms <= self.now_ms - 86_400_000
        ]
        return AssetContext(
            instrument=inst,
            mark_price=px,
            oracle_price=px,
            funding_rate=0.0,
            open_interest=0.0,
            day_notional_volume=sum(
                c.volume * c.close for c in self.candles(asset, self._price_interval, 24)
            ),
            prev_day_price=day_ago[-1].close if day_ago else px,
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
