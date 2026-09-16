"""Thin, read-only client for the Hyperliquid ``/info`` endpoint.

No trading logic lives here. Every method returns typed models from
:mod:`hl_agent.data.models`; raw API dicts never leak upward.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from hl_agent.data.models import (
    AccountState,
    AssetContext,
    Candle,
    FundingRate,
    Instrument,
    Interval,
)

MAINNET_URL = "https://api.hyperliquid.xyz"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

INTERVAL_MS: dict[Interval, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}

_CANDLE_PAGE = 5000  # server-side cap per candleSnapshot request


def split_asset(asset: str) -> tuple[str, str]:
    """``"xyz:NVDA"`` -> ``("xyz", "NVDA")``; ``"BTC"`` -> ``("", "BTC")``."""
    dex, sep, coin = asset.partition(":")
    return (dex, coin) if sep else ("", asset)


class HyperliquidClient:
    def __init__(
        self,
        base_url: str = MAINNET_URL,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HyperliquidClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- low level ---------------------------------------------------------

    def _info(self, payload: dict[str, Any]) -> Any:
        resp = self._http.post("/info", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ---- instruments -------------------------------------------------------

    def instruments(self, dex: str = "") -> list[Instrument]:
        payload: dict[str, Any] = {"type": "meta"}
        if dex:
            payload["dex"] = dex
        meta = self._info(payload)
        return [Instrument.from_api(u, dex) for u in meta["universe"]]

    def asset_contexts(self, dex: str = "") -> dict[str, AssetContext]:
        """Live per-asset context (mark, funding, OI, 24h volume), keyed by asset name."""
        payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        meta, ctxs = self._info(payload)
        out: dict[str, AssetContext] = {}
        for uni, ctx in zip(meta["universe"], ctxs, strict=True):
            inst = Instrument.from_api(uni, dex)
            out[inst.name] = AssetContext.from_api(inst, ctx)
        return out

    def perp_dexs(self) -> list[str]:
        """Names of HIP-3 builder dexs (e.g. ``["xyz"]``). Main dex is ``""``."""
        raw = self._info({"type": "perpDexs"})
        return [str(d["name"]) for d in raw if d is not None]

    # ---- prices ------------------------------------------------------------

    def all_mids(self, dex: str = "") -> dict[str, float]:
        payload: dict[str, Any] = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        raw = self._info(payload)
        prefix = f"{dex}:" if dex else ""
        return {f"{prefix}{k}": float(v) for k, v in raw.items()}

    def mid(self, asset: str) -> float:
        dex, _ = split_asset(asset)
        return self.all_mids(dex)[asset]

    # ---- candles -----------------------------------------------------------

    def candles(
        self, asset: str, interval: Interval, start_ms: int, end_ms: int | None = None
    ) -> list[Candle]:
        """All candles in ``[start_ms, end_ms]``, ascending.

        The server answers with the *most recent* ``_CANDLE_PAGE`` bars of the window,
        so paging walks backwards from ``end_ms`` until the window is exhausted. Note
        Hyperliquid retains a bounded history per interval (roughly 5000 bars for 1h,
        a couple of years for 4h); older bars are simply unavailable.
        """
        end = end_ms if end_ms is not None else int(time.time() * 1000)
        pages: list[list[Candle]] = []
        while end >= start_ms:
            page = self._candle_page(asset, interval, start_ms, end)
            if not page:
                break
            pages.append(page)
            if len(page) < _CANDLE_PAGE:
                break
            end = page[0].open_ms - 1
        out = [c for page in reversed(pages) for c in page]
        return sorted({c.open_ms: c for c in out}.values(), key=lambda c: c.open_ms)

    def _candle_page(
        self, asset: str, interval: Interval, start_ms: int, end_ms: int
    ) -> list[Candle]:
        raw = self._info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": asset,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        return [c for c in map(Candle.from_api, raw) if start_ms <= c.open_ms <= end_ms]

    def recent_candles(self, asset: str, interval: Interval, count: int) -> list[Candle]:
        now = int(time.time() * 1000)
        return self.candles(asset, interval, now - INTERVAL_MS[interval] * count, now)

    # ---- funding -----------------------------------------------------------

    def funding_history(
        self, asset: str, start_ms: int, end_ms: int | None = None
    ) -> list[FundingRate]:
        payload: dict[str, Any] = {"type": "fundingHistory", "coin": asset, "startTime": start_ms}
        if end_ms is not None:
            payload["endTime"] = end_ms
        return [FundingRate.from_api(r) for r in self._info(payload)]

    # ---- account -----------------------------------------------------------

    def account_state(self, address: str, dex: str = "") -> AccountState:
        payload: dict[str, Any] = {"type": "clearinghouseState", "user": address}
        if dex:
            payload["dex"] = dex
        return AccountState.from_api(self._info(payload))

    def order_book(self, asset: str, depth: int = 20) -> dict[str, list[tuple[float, float]]]:
        """``{"bids": [(px, sz), ...], "asks": [...]}`` best-first."""
        raw = self._info({"type": "l2Book", "coin": asset})
        bids, asks = raw["levels"]
        return {
            "bids": [(float(lvl["px"]), float(lvl["sz"])) for lvl in bids[:depth]],
            "asks": [(float(lvl["px"]), float(lvl["sz"])) for lvl in asks[:depth]],
        }
