from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hl_agent.data.hyperliquid_client import HyperliquidClient
from hl_agent.data.models import Direction
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.sizing import OrderPlan
from hl_agent.execution.live import (
    BrokerError,
    HlBroker,
    LiveMarketSource,
    Network,
    load_address,
    load_signer,
    round_price,
)

ADDR = "0x" + "ab" * 20
# eth_account test vector: private key 0x01 -> this address
AGENT_KEY = "0x" + "00" * 31 + "01"
AGENT = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
H = 3_600_000


# ---- fake /info ---------------------------------------------------------------------


def info_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    t = body["type"]
    universe = [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]
    if t == "meta":
        return httpx.Response(200, json={"universe": universe})
    if t == "allMids":
        return httpx.Response(200, json={"BTC": "100000.5"})
    if t == "metaAndAssetCtxs":
        ctx = {"markPx": "100000.5", "funding": "0.0001", "openInterest": "1", "dayNtlVlm": "2"}
        return httpx.Response(200, json=[{"universe": universe}, [ctx]])
    if t == "clearinghouseState":
        return httpx.Response(
            200,
            json={
                "marginSummary": {"accountValue": "100", "totalMarginUsed": "0"},
                "withdrawable": "100",
                "assetPositions": [],
            },
        )
    if t == "candleSnapshot":
        bars = [
            {
                "s": "BTC",
                "i": "1h",
                "t": i * H,
                "T": (i + 1) * H - 1,
                "o": "1",
                "h": "2",
                "l": "1",
                "c": "1.5",
                "v": "1",
                "n": 1,
            }
            for i in range(10)
        ]
        return httpx.Response(200, json=bars)
    if t == "extraAgents":
        return httpx.Response(
            200,
            json=[{"name": "hl-agent", "address": AGENT, "validUntil": 1_800_000_000_000}],
        )
    if t == "fundingHistory":
        return httpx.Response(200, json=[{"coin": "BTC", "time": 0, "fundingRate": "0.0001"}])
    return httpx.Response(400, json={"error": t})


@pytest.fixture
def market() -> LiveMarketSource:
    client = HyperliquidClient(transport=httpx.MockTransport(info_handler))
    src = LiveMarketSource(client, ADDR)
    src.refresh(5 * H)  # only bars closed by 5h are visible
    return src


def test_live_source_reads_and_memoises(market: LiveMarketSource) -> None:
    assert market.instrument("BTC") is not None and market.price("BTC") == 100000.5
    assert [i.name for i in market.instruments()] == ["BTC"]
    bars = market.candles("BTC", "1h", 300)
    assert len(bars) == 5 and bars[-1].close_ms <= market.now_ms
    assert market.candles("BTC", "1h", 300) is not bars  # filtered copy, one API read
    assert market.account().account_value == 100.0 and market.account() is market.account()
    ctx = market.asset_context("BTC")
    assert ctx is not None and ctx.funding_rate == 0.0001
    assert len(market.funding_history("BTC", 24)) == 1
    market.refresh(6 * H)
    assert len(market.candles("BTC", "1h", 300)) == 6


# ---- fake exchange ------------------------------------------------------------------


class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fill_px = 100_000.0
        self.fail_stop = False
        self._oid = 100

    def _rec(self, name: str, *a: Any, **kw: Any) -> None:
        self.calls.append((name, a, kw))

    def update_leverage(self, leverage: int, name: str, is_cross: bool = True) -> Any:
        self._rec("update_leverage", leverage, name, is_cross)
        return {"status": "ok"}

    def market_open(
        self, name: str, is_buy: bool, sz: float, px: float | None = None, slippage: float = 0.05
    ) -> Any:
        self._rec("market_open", name, is_buy, sz)
        self._oid += 1
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"totalSz": sz, "avgPx": self.fill_px, "oid": self._oid}}
                    ]
                },
            },
        }

    def market_close(
        self, coin: str, sz: float | None = None, px: float | None = None, slippage: float = 0.05
    ) -> Any:
        self._rec("market_close", coin)
        self._oid += 1
        filled = {"totalSz": sz or 0.001, "avgPx": self.fill_px, "oid": self._oid}
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": filled}]}}}

    def order(
        self,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: Any,
        reduce_only: bool = False,
    ) -> Any:
        self._rec("order", name, is_buy, sz, limit_px, order_type, reduce_only)
        if self.fail_stop:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "nope"}]}}}
        self._oid += 1
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": self._oid}}]}},
        }

    def cancel(self, name: str, oid: int) -> Any:
        self._rec("cancel", name, oid)
        return {"status": "ok"}


class FakeInfo:
    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []

    def user_fills(self, address: str) -> Any:
        return self.fills


def broker(market: LiveMarketSource) -> tuple[HlBroker, FakeExchange, FakeInfo]:
    ex, info = FakeExchange(), FakeInfo()
    return HlBroker(ex, info, ADDR, market=market), ex, info


PLAN = OrderPlan("BTC", Direction.LONG, 0.00123, 2, 61.5, 123.0, 100_000.0)


def test_open_sets_leverage_fills_and_places_reduce_only_stop(market: LiveMarketSource) -> None:
    b, ex, _ = broker(market)
    fill = b.open(PLAN, 97_654.321, 1)
    assert fill.size == 0.00123 and fill.price == 100_000.0
    assert fill.fee_usd == pytest.approx(0.00123 * 100_000 * 0.00035)
    names = [c[0] for c in ex.calls]
    assert names == ["update_leverage", "market_open", "order"]
    assert ex.calls[0][1] == (2, "BTC", True) and ex.calls[1][1] == ("BTC", True, 0.00123)
    _, (_, is_buy, sz, px, otype, reduce_only), _ = ex.calls[2]
    assert not is_buy and sz == 0.00123 and reduce_only
    assert px == 97654.0 and otype["trigger"] == {"triggerPx": px, "isMarket": True, "tpsl": "sl"}
    assert b.orders["BTC"].stop_oid == 102


def test_stop_failure_flattens_and_raises(market: LiveMarketSource) -> None:
    b, ex, _ = broker(market)
    ex.fail_stop = True
    with pytest.raises(BrokerError):
        b.open(PLAN, 97_000.0, 1)
    assert [c[0] for c in ex.calls][-2:] == ["order", "market_close"] and b.orders == {}


def test_set_stop_and_close_manage_the_exchange_order(market: LiveMarketSource) -> None:
    b, ex, _ = broker(market)
    b.open(PLAN, 97_000.0, 1)
    b.set_stop("BTC", 98_500.0, 2)
    assert [c[0] for c in ex.calls][-2:] == ["cancel", "order"]
    assert ex.calls[-2][1] == ("BTC", 102) and b.orders["BTC"].stop_oid == 103
    fill = b.close("BTC", CloseReason.HARD_TIMEOUT, 3)
    assert [c[0] for c in ex.calls][-2:] == ["cancel", "market_close"]
    assert fill.direction is Direction.LONG and b.orders == {}


def test_external_close_reads_fills(market: LiveMarketSource) -> None:
    b, _, info = broker(market)
    b.open(PLAN, 97_000.0, 1)
    assert b.external_close("ETH", 5) is None
    info.fills = [
        {"coin": "BTC", "oid": 102, "px": "97000", "sz": "0.001", "fee": "0.03", "time": 7},
        {"coin": "BTC", "oid": 102, "px": "96000", "sz": "0.00023", "fee": "0.01", "time": 8},
        {"coin": "ETH", "oid": 102, "px": "1", "sz": "1", "fee": "0", "time": 9},
    ]
    hit = b.external_close("BTC", 10)
    assert hit is not None and hit[0] is CloseReason.EXCHANGE_SL_HIT
    fill = hit[1]
    assert fill.size == pytest.approx(0.00123) and fill.fee_usd == pytest.approx(0.04)
    assert fill.price == pytest.approx((97000 * 0.001 + 96000 * 0.00023) / 0.00123)
    assert fill.time_ms == 8 and b.orders == {}

    b.open(PLAN, 97_000.0, 11)
    info.fills = [
        {"coin": "BTC", "oid": 1, "px": "50000", "sz": "0.00123", "dir": "Liquidated Long"}
    ]
    hit = b.external_close("BTC", 12)
    assert hit is not None and hit[0] is CloseReason.LIQUIDATED
    b.open(PLAN, 97_000.0, 13)
    info.fills = []
    assert b.external_close("BTC", 14) is None


def test_price_rounding_and_env_loading() -> None:
    assert round_price(1234.5678, 5) == 1234.6
    assert round_price(0.0123456, 0) == 0.012346
    assert round_price(65432.1, 5) == 65432.0
    assert round_price(3.14159265, 4) == 3.14  # 6 - szDecimals decimals
    assert Network.TESTNET.url.endswith("testnet.xyz") and "testnet" not in Network.MAINNET.url
    with pytest.raises(BrokerError):
        load_signer({})
    with pytest.raises(BrokerError):
        load_address({"HL_AGENT_ADDRESS": "abc"})
    assert load_address({"HL_AGENT_ADDRESS": ADDR}) == ADDR
    signer = load_signer({"HL_AGENT_PRIVATE_KEY": "0x" + "11" * 32})
    assert signer.address.startswith("0x") and len(signer.address) == 42
