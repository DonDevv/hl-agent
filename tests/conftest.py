from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from hl_agent.data.hyperliquid_client import HyperliquidClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeInfoTransport(httpx.MockTransport):
    """Routes ``/info`` requests to recorded fixtures by payload ``type``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(payload)
        kind = payload["type"]
        table = {
            "candleSnapshot": "candles_btc_1h.json",
            "meta": "meta_main.json",
            "metaAndAssetCtxs": "meta_and_ctxs_main.json",
            "fundingHistory": "funding_btc.json",
            "perpDexs": "perp_dexs.json",
            "l2Book": "l2_btc.json",
            "clearinghouseState": "clearinghouse_with_positions.json",
        }
        if kind == "candleSnapshot":
            req = payload["req"]
            bars = load_fixture("candles_btc_1h.json")
            return httpx.Response(
                200, json=[b for b in bars if req["startTime"] <= b["t"] <= req["endTime"]]
            )
        if kind == "allMids":
            meta, ctxs = load_fixture("meta_and_ctxs_main.json")
            body = {u["name"]: c["markPx"] for u, c in zip(meta["universe"], ctxs, strict=True)}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=load_fixture(table[kind]))


@pytest.fixture
def transport() -> FakeInfoTransport:
    return FakeInfoTransport()


@pytest.fixture
def client(transport: FakeInfoTransport) -> HyperliquidClient:
    return HyperliquidClient(base_url="https://fake.test", transport=transport)
