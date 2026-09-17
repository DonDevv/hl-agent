"""Shared dashboard fixture: a runs dir with one live and one stale run, an account view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hl_agent.data.models import AccountState, Direction, Position
from hl_agent.engine.loop import Event
from hl_agent.telemetry.events import EventLog
from hl_agent.web.app import AccountView
from tests.copy.test_mirror import FakeFeed, book, pos
from tests.web.test_app import NOW_MS, closed, write_run

L = Direction.LONG


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    runs = tmp_path / "runs"
    live = write_run(runs, "copy-live", [1000.0, 1010.0, 990.0, 1020.0])
    log = EventLog(live / "events.jsonl")
    log.write(Event(NOW_MS - 120_000, "opened", "BTC", "copy", {"margin_usd": 50.0, "leverage": 2}))
    log.write(closed(NOW_MS - 60_000, 12.0))
    log.write(closed(NOW_MS, -4.0))
    stale = write_run(runs, "old", [500.0, 400.0], end_ms=NOW_MS - 86_400_000)
    (stale / "metrics.json").write_text("{}", encoding="utf-8")
    eth = Position("ETH", L, 0.1, 3000.0, 3, 100.0, 10.0, 2100.0, 10.0)
    ours = AccountState(10_000.0, 9_900.0, 100.0, (eth,))
    prices = {"BTC": 100_000.0, "ETH": 3100.0}
    view = AccountView(lambda: ours, lambda: prices, lambda: "agent key: 0xabc authorised for x")
    trader = book(pos("BTC", L, 500, 100_000.0, 5))
    feed = FakeFeed(trader, prices)
    return {"runs": runs, "view": view, "feed": feed, "prices": prices, "tmp": tmp_path}
