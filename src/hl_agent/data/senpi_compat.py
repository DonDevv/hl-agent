"""Builders for the payload shapes Senpi ``scan.py`` files expect from the MCP.

These are pure functions from our typed models to plain dicts. The shapes
mirror what the 112 catalog scanners actually read (``data.candles[interval]``,
``data.asset_context.markPx``, ``data.main.marginSummary`` …). Keeping them
here means a Senpi package runs on our runtime unmodified, and the only place
that knows about Senpi's dict conventions is this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hl_agent.data.models import AccountState, AssetContext, Candle, Instrument, Interval


def asset_context_payload(ctx: AssetContext) -> dict[str, Any]:
    return {
        "coin": ctx.name,
        "markPx": ctx.mark_price,
        "oraclePx": ctx.oracle_price,
        "funding": ctx.funding_rate,
        "openInterest": ctx.open_interest,
        "dayNtlVlm": ctx.day_notional_volume,
        "prevDayPx": ctx.prev_day_price,
        "max_leverage": ctx.instrument.max_leverage,
        "maxLeverage": ctx.instrument.max_leverage,
    }


def market_get_asset_data(
    asset: str,
    candles: Mapping[Interval, Sequence[Candle]],
    ctx: AssetContext | None = None,
    order_book: Mapping[str, list[tuple[float, float]]] | None = None,
) -> dict[str, Any]:
    """Payload for ``market_get_asset_data``. Candles are keyed by interval."""
    data: dict[str, Any] = {
        "asset": asset,
        "candles": {iv: [c.to_senpi() for c in bars] for iv, bars in candles.items()},
    }
    if ctx is not None:
        ac = asset_context_payload(ctx)
        data["asset_context"] = ac
        data["funding"] = ctx.funding_rate
        data["price"] = ctx.mark_price
    if order_book is not None:
        data["order_book"] = {
            "bids": [{"px": px, "sz": sz} for px, sz in order_book.get("bids", [])],
            "asks": [{"px": px, "sz": sz} for px, sz in order_book.get("asks", [])],
        }
    return {"success": True, "data": data}


def market_get_prices(mids: Mapping[str, float]) -> dict[str, Any]:
    return {"success": True, "data": {"prices": dict(mids), "count": len(mids)}}


def market_list_instruments(
    instruments: Sequence[Instrument], contexts: Mapping[str, AssetContext] | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for inst in instruments:
        row: dict[str, Any] = {
            "name": inst.name,
            "coin": inst.name,
            "max_leverage": inst.max_leverage,
            "maxLeverage": inst.max_leverage,
            "is_delisted": inst.delisted,
            "only_isolated": inst.only_isolated,
        }
        if contexts and inst.name in contexts:
            row["context"] = asset_context_payload(contexts[inst.name])
        rows.append(row)
    return {"success": True, "data": {"instruments": rows, "count": len(rows)}}


def _clearinghouse_section(state: AccountState) -> dict[str, Any]:
    return {
        "marginSummary": {
            "accountValue": state.account_value,
            "totalMarginUsed": state.total_margin_used,
            "totalNtlPos": sum(p.notional for p in state.positions),
        },
        "withdrawable": state.withdrawable,
        "assetPositions": [
            {
                "type": "oneWay",
                "position": {
                    "coin": p.asset,
                    "szi": p.size * p.direction.sign,
                    "entryPx": p.entry_price,
                    "leverage": {"type": "cross", "value": p.leverage},
                    "marginUsed": p.margin_used,
                    "unrealizedPnl": p.unrealized_pnl,
                    "liquidationPx": p.liquidation_price,
                    "returnOnEquity": p.roe_pct / 100.0,
                },
            }
            for p in state.positions
        ],
    }


def strategy_get_clearinghouse_state(
    wallet: str, main: AccountState, by_dex: Mapping[str, AccountState] | None = None
) -> dict[str, Any]:
    """Senpi returns one section per dex (``main``, ``xyz`` …) for one cross-margined
    wallet. Scanners take ``max()`` of account values across sections, never the sum."""
    data: dict[str, Any] = {"wallet": wallet, "main": _clearinghouse_section(main)}
    for dex, state in (by_dex or {}).items():
        data[dex] = _clearinghouse_section(state)
    return {"success": True, "data": data}


def empty_leaderboard() -> dict[str, Any]:
    """Stand-in for Senpi's proprietary ``leaderboard_*`` tools: a valid, empty
    payload so scanners that use the smart-money bonus degrade to zero bonus."""
    return {"success": True, "data": {"markets": [], "traders": [], "count": 0}}
