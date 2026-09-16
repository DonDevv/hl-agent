"""``ctx.senpi_mcp`` — a local, read-only stand-in for Senpi's MCP client.

``call_tool(name, args)`` routes the handful of tools the catalog scanners actually use to a
``MarketSource`` (live API or backtest replay — the shim does not care). Payload shapes come
from ``data.senpi_compat`` so a Senpi ``scan.py`` reads them unmodified.

Boundary rules mirror the scaffold: every mutating tool raises ``PermissionError`` before
anything happens; Senpi-proprietary reads (``leaderboard_*``, ``discovery_*``, …) return a
valid empty payload so smart-money bonuses degrade to zero instead of crashing the tick.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from hl_agent.data import senpi_compat
from hl_agent.data.models import AccountState, AssetContext, Candle, FundingRate, Instrument

DEFAULT_CANDLE_LIMIT = 300

MUTATING_TOOLS = frozenset(
    {
        "create_position",
        "close_position",
        "edit_position",
        "cancel_order",
        "send_usdc",
        "transfer_spot_to_perps",
        "strategy_create",
        "strategy_create_custom_strategy",
        "strategy_close",
        "strategy_close_positions",
        "strategy_update",
        "strategy_pause",
        "strategy_top_up",
        "strategy_withdraw_funds",
        "ratchet_stop_add",
        "ratchet_stop_edit",
        "ratchet_stop_delete",
        "user_claim_referral_rewards",
    }
)

_PROPRIETARY_PREFIXES = ("leaderboard_", "discovery_", "arena_", "audit_", "execution_", "user_")


class MarketSource(Protocol):
    """What the shim needs; implemented over the live API (Axe 4) or a candle replay."""

    def candles(self, asset: str, interval: str, limit: int) -> Sequence[Candle]: ...
    def asset_context(self, asset: str) -> AssetContext | None: ...
    def instruments(self, dex: str = "") -> Sequence[Instrument]: ...
    def mids(self, dex: str = "") -> Mapping[str, float]: ...
    def account(self) -> AccountState: ...
    def funding_history(self, asset: str, limit: int) -> Sequence[FundingRate]: ...


def _qualify(asset: str, dex: str) -> str:
    return f"{dex}:{asset}" if dex and ":" not in asset else asset


class SenpiMcp:
    def __init__(self, source: MarketSource, wallet: str) -> None:
        self._source = source
        self._wallet = wallet
        self.calls = 0  # read count — a tick that reads nothing validates as UNPROVEN

    def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> Any:
        if name in MUTATING_TOOLS:
            raise PermissionError(f"{name} is a mutation; scanners are read-only")
        a = dict(args or {})
        self.calls += 1
        handler = getattr(self, f"_tool_{name}", None)
        if handler is not None:
            return handler(a)
        if name.startswith(_PROPRIETARY_PREFIXES):
            return senpi_compat.empty_leaderboard()
        raise KeyError(f"unknown tool {name!r}")

    # ---- market ------------------------------------------------------------------------

    def _tool_market_get_asset_data(self, a: dict[str, Any]) -> dict[str, Any]:
        asset = _qualify(str(a.get("asset") or a.get("coin") or ""), str(a.get("dex", "")))
        intervals = a.get("candle_intervals") or ["1h"]
        limit = int(a.get("candle_limit") or a.get("limit") or DEFAULT_CANDLE_LIMIT)
        candles = {iv: self._source.candles(asset, iv, limit) for iv in intervals}
        return senpi_compat.market_get_asset_data(asset, candles, self._source.asset_context(asset))

    def _tool_market_get_prices(self, a: dict[str, Any]) -> dict[str, Any]:
        return senpi_compat.market_get_prices(self._source.mids(str(a.get("dex", ""))))

    def _tool_market_list_instruments(self, a: dict[str, Any]) -> dict[str, Any]:
        dex = str(a.get("dex", ""))
        instruments = self._source.instruments(dex)
        contexts = {
            i.name: c for i in instruments if (c := self._source.asset_context(i.name)) is not None
        }
        return senpi_compat.market_list_instruments(instruments, contexts)

    def _tool_market_get_funding_history(self, a: dict[str, Any]) -> dict[str, Any]:
        asset = _qualify(str(a.get("asset") or a.get("coin") or ""), str(a.get("dex", "")))
        rows = self._source.funding_history(asset, int(a.get("limit", 100)))
        return {
            "success": True,
            "data": {
                "asset": asset,
                "funding_history": [
                    {
                        "coin": r.asset,
                        "time": r.time_ms,
                        "fundingRate": r.rate,
                        "premium": r.premium,
                    }
                    for r in rows
                ],
            },
        }

    # ---- account -----------------------------------------------------------------------

    def _tool_strategy_get_clearinghouse_state(self, a: dict[str, Any]) -> dict[str, Any]:
        return senpi_compat.strategy_get_clearinghouse_state(
            str(a.get("strategy_wallet") or self._wallet), self._source.account()
        )

    _tool_account_get_clearinghouse_state = _tool_strategy_get_clearinghouse_state
