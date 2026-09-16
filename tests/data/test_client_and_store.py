from __future__ import annotations

from pathlib import Path

from hl_agent.data.history import CandleStore
from hl_agent.data.hyperliquid_client import HyperliquidClient, split_asset
from hl_agent.data.models import Candle
from tests.conftest import FakeInfoTransport


def test_split_asset() -> None:
    assert split_asset("BTC") == ("", "BTC")
    assert split_asset("xyz:NVDA") == ("xyz", "NVDA")


def test_instruments_and_contexts(client: HyperliquidClient) -> None:
    instruments = client.instruments()
    names = {i.name for i in instruments}
    assert {"BTC", "ETH"} <= names
    ctxs = client.asset_contexts()
    assert ctxs["BTC"].mark_price > 0
    assert ctxs["BTC"].instrument.max_leverage == 40


def test_all_mids_and_mid(client: HyperliquidClient) -> None:
    mids = client.all_mids()
    assert isinstance(mids["BTC"], float)
    assert client.mid("BTC") == mids["BTC"]


def test_candles_are_typed_sorted_and_filtered_by_start(
    client: HyperliquidClient, transport: FakeInfoTransport
) -> None:
    all_bars = client.candles("BTC", "1h", 0, 10**14)
    assert len(all_bars) == 31
    assert all(isinstance(c, Candle) for c in all_bars)
    assert [c.open_ms for c in all_bars] == sorted(c.open_ms for c in all_bars)

    later = client.candles("BTC", "1h", all_bars[10].open_ms, 10**14)
    assert later[0].open_ms == all_bars[10].open_ms
    assert transport.calls[-1]["req"]["coin"] == "BTC"


def test_funding_history_and_order_book(client: HyperliquidClient) -> None:
    rates = client.funding_history("BTC", 0)
    assert rates and rates[0].asset == "BTC"
    book = client.order_book("BTC", depth=3)
    assert len(book["bids"]) == 3 and len(book["asks"]) == 3
    assert book["bids"][0][0] < book["asks"][0][0]


def test_account_state(client: HyperliquidClient) -> None:
    state = client.account_state("0xabc")
    assert len(state.positions) == 2


def test_store_append_dedups_and_sorts(tmp_path: Path, client: HyperliquidClient) -> None:
    store = CandleStore(tmp_path)
    bars = client.candles("BTC", "1h", 0, 10**14)
    assert store.append("BTC", "1h", bars[:20]) == 20
    # overlapping write: only the 11 new bars count, duplicates are replaced
    assert store.append("BTC", "1h", bars[15:]) == 11
    loaded = store.load("BTC", "1h")
    assert loaded == bars
    assert store.last_open_ms("BTC", "1h") == bars[-1].open_ms


def test_store_load_range_and_missing(tmp_path: Path, client: HyperliquidClient) -> None:
    store = CandleStore(tmp_path)
    assert store.load("ETH", "1h") == []
    assert store.last_open_ms("ETH", "1h") is None
    bars = client.candles("BTC", "1h", 0, 10**14)
    store.append("BTC", "1h", bars)
    window = store.load("BTC", "1h", start_ms=bars[5].open_ms, end_ms=bars[9].open_ms)
    assert [c.open_ms for c in window] == [c.open_ms for c in bars[5:10]]


def test_store_sync_resumes_from_last_bar(
    tmp_path: Path, client: HyperliquidClient, transport: FakeInfoTransport
) -> None:
    store = CandleStore(tmp_path)
    added = store.sync(client, "BTC", "1h", since=0, until=10**14)
    assert added == 31
    first_start = transport.calls[-1]["req"]["startTime"]
    assert first_start == 0

    added_again = store.sync(client, "BTC", "1h", since=0, until=10**14)
    assert added_again == 0
    resumed_start = transport.calls[-1]["req"]["startTime"]
    assert resumed_start == store.last_open_ms("BTC", "1h")
