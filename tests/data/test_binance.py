from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hl_agent.data.binance_client import FUTURES_URL, BinanceClient, symbol_for
from hl_agent.data.history import CandleStore
from hl_agent.data.models import Candle

H = 3_600_000


def kline_handler(request: httpx.Request) -> httpx.Response:
    """A tape of one bar per hour from 0; honours startTime/endTime/limit like Binance."""
    q = request.url.params
    assert q["symbol"] == "BTCUSDT" and request.url.path == "/fapi/v1/klines"
    start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
    rows = []
    t = (start // H) * H
    while t <= end and len(rows) < limit:
        px = 100 + t // H
        rows.append(
            [t, str(px), str(px + 1), str(px - 1), str(px), "5", t + H - 1, "0", 7, 0, 0, 0]
        )
        t += H
    return httpx.Response(200, json=rows)


def client() -> BinanceClient:
    return BinanceClient(
        FUTURES_URL, transport=httpx.MockTransport(kline_handler), sleep=lambda s: None
    )


def test_symbols_and_pages_forward() -> None:
    assert symbol_for("btc") == "BTCUSDT"
    assert symbol_for("kPEPE") == "1000PEPEUSDT"  # HL k-prefix = Binance 1000-prefix
    with pytest.raises(ValueError):
        symbol_for("xyz:NVDA")
    with client() as c:
        bars = c.candles("BTC", "1h", 0, 2500 * H - 1)  # three pages of 1000
        assert len(bars) == 2500 and bars[0].open_ms == 0 and bars[-1].open_ms == 2499 * H
        assert bars[3] == Candle("BTC", "1h", 3 * H, 4 * H - 1, 103, 104, 102, 103, 5.0, 7)
        assert c.candles("BTC", "1h", 10 * H, 10 * H + 5) == []  # in-progress bar dropped
        with pytest.raises(ValueError):
            c.candles("BTC", "7h", 0, H)


def test_backfill_never_overwrites_primary_bars(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    hl = [Candle("BTC", "1h", t * H, (t + 1) * H - 1, 1, 1, 1, 1, 1.0) for t in range(50, 60)]
    store.append("BTC", "1h", hl)
    with client() as c:
        assert store.backfill(c, "BTC", "1h", 20 * H) == 30
        assert store.backfill(c, "BTC", "1h", 20 * H) == 0  # already filled
        assert store.backfill(c, "BTC", "1h", 55 * H) == 0  # inside the primary window
    frame = store.frame("BTC", "1h")
    assert frame.height == 40 and store.first_open_ms("BTC", "1h") == 20 * H
    assert frame.filter(frame["open_ms"] == 50 * H)["close"][0] == 1.0  # HL bar kept
    assert frame.filter(frame["open_ms"] == 49 * H)["close"][0] == 149.0  # Binance bar

    empty = CandleStore(tmp_path / "empty")
    with client() as c:
        assert empty.backfill(c, "BTC", "1h", 0) > 0  # no primary data: fills to now
    assert json.loads(json.dumps(empty.first_open_ms("BTC", "1h"))) == 0
