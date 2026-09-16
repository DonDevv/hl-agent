"""PENDULUM — supervised scanner.

Reads 1h candles, scores with ``scoring.evaluate``, emits at most one signal per asset per
closed bar (dedup via ``ctx.state``) and never for an asset already held.
"""

from __future__ import annotations

import sys
from typing import Any

import scoring

_DEFAULTS: dict[str, Any] = {
    "assets": ["BTC", "ETH", "SOL"],
    "interval": "1h",
    "rsiPeriod": 14,
    "oversold": 30,
    "overbought": 70,
    "marginPct": 15,
    "leverage": 2,
}


def _held(ctx: Any) -> set[str]:
    try:
        ch = ctx.senpi_mcp.call_tool(
            "strategy_get_clearinghouse_state", {"strategy_wallet": ctx.wallet}
        )
    except Exception as exc:
        print(f"[pendulum.scan] clearinghouse read failed: {exc!r}", file=sys.stderr)
        return set()
    data = ch.get("data", ch) if isinstance(ch, dict) else {}
    held: set[str] = set()
    for section in data.values() if isinstance(data, dict) else []:
        if not isinstance(section, dict):
            continue
        for ap in section.get("assetPositions", []):
            pos = ap.get("position", ap)
            if scoring._f(pos.get("szi", 0)) != 0:
                held.add(str(pos.get("coin", "")).upper())
    return held


def _candles(ctx: Any, asset: str, interval: str) -> list[dict[str, Any]]:
    try:
        md = ctx.senpi_mcp.call_tool(
            "market_get_asset_data",
            {
                "asset": asset,
                "candle_intervals": [interval],
                "include_funding": False,
                "include_order_book": False,
            },
        )
    except Exception as exc:
        print(f"[pendulum.scan] {asset}: read failed {exc!r}", file=sys.stderr)
        return []
    if not isinstance(md, dict):
        return []
    candles = ((md.get("data", md) or {}).get("candles", {}) or {}).get(interval, [])
    return candles if isinstance(candles, list) else []


def scan(inputs: dict[str, Any], ctx: Any) -> list[dict[str, Any]]:
    cfg = {**_DEFAULTS, **(inputs or {})}
    held = _held(ctx)
    last_bar = (ctx.state.last() or {}).get("last_bar", {}) if ctx.state is not None else {}
    out: list[dict[str, Any]] = []
    for asset in cfg["assets"]:
        candles = _candles(ctx, asset, cfg["interval"])
        pick = scoring.evaluate(
            candles, int(cfg["rsiPeriod"]), float(cfg["oversold"]), float(cfg["overbought"])
        )
        if pick is None or asset.upper() in held:
            continue
        if last_bar.get(asset) == pick["barTime"]:
            continue
        last_bar[asset] = pick["barTime"]
        out.append(
            {
                "asset": asset,
                "direction": pick["direction"],
                "marginPct": float(cfg["marginPct"]),
                "leverage": int(cfg["leverage"]),
                "data": {
                    "score": pick["score"],
                    "direction": pick["direction"],
                    "rsi": pick["rsi"],
                    "reasons": pick["reasons"],
                    "barTime": pick["barTime"],
                },
            }
        )
    if ctx.state is not None:
        try:
            ctx.state.append({"last_bar": last_bar})
        except Exception as exc:
            print(f"[pendulum.scan] state append failed: {exc!r}", file=sys.stderr)
    return out
