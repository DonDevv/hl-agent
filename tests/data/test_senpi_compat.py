"""These tests exercise the payloads the way real Senpi scanners read them
(copied access patterns from ``strategies/coyote`` and ``strategies/spider``)."""

from __future__ import annotations

from hl_agent.data import senpi_compat as sc
from hl_agent.data.hyperliquid_client import HyperliquidClient
from hl_agent.data.models import AccountState
from tests.conftest import load_fixture


def test_market_get_asset_data_shape_as_read_by_coyote(client: HyperliquidClient) -> None:
    bars = client.candles("BTC", "1h", 0, 10**14)
    ctx = client.asset_contexts()["BTC"]
    md = sc.market_get_asset_data("BTC", {"1h": bars}, ctx)

    # verbatim coyote/scan.py access path
    assert md.get("success") is not False
    d = md.get("data", md)
    candles = (d.get("candles", {}) or {}).get("1h", []) or []
    closes = [float(c.get("close", c.get("c"))) for c in candles]
    assert len(closes) == 31 and closes[-1] == bars[-1].close

    # spider-style asset_context access
    venue = (d.get("asset_context", {}) or {}).get("max_leverage")
    assert venue == 40
    assert d["asset_context"]["markPx"] == ctx.mark_price
    assert d["funding"] == ctx.funding_rate


def test_clearinghouse_shape_as_read_by_coyote() -> None:
    state = AccountState.from_api(load_fixture("clearinghouse_with_positions.json"))
    ch = sc.strategy_get_clearinghouse_state("0xabc", state, {"xyz": state})
    data = ch.get("data", ch)

    held, account_value = [], 0.0
    for section in ("main", "xyz"):
        s = data.get(section, {})
        ms = s.get("marginSummary", {}) or {}
        account_value = max(account_value, float(ms.get("accountValue", 0)))
        for ap in s.get("assetPositions", []) or []:
            pos = ap.get("position", ap)
            if float(pos.get("szi", 0)) == 0:
                continue
            held.append(str(pos.get("coin", "")).upper())
    assert account_value == 1000.5  # max, not sum: no double counting across dexs
    assert held == ["BTC", "ETH", "BTC", "ETH"]
    assert data["main"]["assetPositions"][1]["position"]["szi"] == -0.2


def test_instruments_and_prices_shapes(client: HyperliquidClient) -> None:
    insts = client.instruments()
    payload = sc.market_list_instruments(insts, client.asset_contexts())
    rows = payload["data"]["instruments"]
    live = [r for r in rows if not r.get("is_delisted")]
    assert live and all("context" in r and "markPx" in r["context"] for r in live)

    prices = sc.market_get_prices(client.all_mids())
    pm = prices.get("data", prices).get("prices", {})
    assert isinstance(pm, dict) and "BTC" in pm


def test_empty_leaderboard_is_a_valid_no_bonus_payload() -> None:
    lb = sc.empty_leaderboard()
    markets = lb.get("data", lb).get("markets", [])
    assert markets == []
