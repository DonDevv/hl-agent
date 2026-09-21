"""Read-only Binance klines client, used only to *extend* the candle cache further back
than Hyperliquid retains (1h ≈ 7 months there, years here).

Bars are stored under the Hyperliquid asset name (``BTC``, not ``BTCUSDT``) so the
backtester does not know or care where a bar came from. Binance USDT-perp prices track
Hyperliquid's within a few bps; more than good enough for hourly-and-slower strategies.
No API key is needed for public market data.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from hl_agent.data.hyperliquid_client import INTERVAL_MS
from hl_agent.data.models import Candle, Interval

FUTURES_URL = "https://fapi.binance.com"  # USDT-margined perpetuals (closest to HL)
SPOT_URL = "https://api.binance.com"

_PAGE = 1000  # server-side cap per klines request
_PATH = {FUTURES_URL: "/fapi/v1/klines", SPOT_URL: "/api/v3/klines"}


def symbol_for(asset: str, quote: str = "USDT") -> str:
    """``"BTC"`` -> ``"BTCUSDT"``. HIP-3 assets (``xyz:NVDA``) have no Binance market."""
    if ":" in asset:
        raise ValueError(f"{asset} is a Hyperliquid-only market, no Binance symbol")
    if asset.startswith("k") and asset[1:].isupper():  # HL "kPEPE" = 1000 PEPE = Binance 1000PEPE
        return f"1000{asset[1:]}{quote}"
    return f"{asset.upper()}{quote}"


def candle_from_kline(asset: str, interval: Interval, raw: list[Any]) -> Candle:
    # [open_ms, o, h, l, c, base_vol, close_ms, quote_vol, trades, ...]
    return Candle(
        asset=asset,
        interval=interval,
        open_ms=int(raw[0]),
        close_ms=int(raw[6]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        trades=int(raw[8]),
    )


class BinanceClient:
    def __init__(
        self,
        base_url: str = FUTURES_URL,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        pause_s: float = 0.1,  # public limit is generous; be polite anyway
        sleep: Any = time.sleep,
    ) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)
        self._path = _PATH.get(base_url, "/fapi/v1/klines")
        self._pause_s = pause_s
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BinanceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _page(self, symbol: str, interval: Interval, start_ms: int, end_ms: int) -> list[Any]:
        r = self._http.get(
            self._path,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": _PAGE,
            },
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected Binance reply: {data!r}"[:200])
        return data

    def candles(
        self, asset: str, interval: Interval, start_ms: int, end_ms: int | None = None
    ) -> list[Candle]:
        """Closed candles in ``[start_ms, end_ms]``, ascending. Pages *forward* (Binance
        returns the oldest ``_PAGE`` bars of the window, the opposite of Hyperliquid)."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"unknown interval {interval!r}")
        symbol = symbol_for(asset)
        end = end_ms if end_ms is not None else int(time.time() * 1000)
        out: list[Candle] = []
        cursor = start_ms
        while cursor <= end:
            page = self._page(symbol, interval, cursor, end)
            if not page:
                break
            bars = [candle_from_kline(asset, interval, k) for k in page]
            out.extend(b for b in bars if b.close_ms <= end)  # drop the in-progress bar
            if len(page) < _PAGE:
                break
            cursor = bars[-1].open_ms + INTERVAL_MS[interval]
            self._sleep(self._pause_s)
        return sorted({c.open_ms: c for c in out}.values(), key=lambda c: c.open_ms)
