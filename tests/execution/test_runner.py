from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest

from hl_agent.data.hyperliquid_client import HyperliquidClient
from hl_agent.data.models import Direction
from hl_agent.engine.config import DslConfig, Phase1, StrategyConfig, Tier
from hl_agent.engine.loop import Engine, EngineConfig, Event
from hl_agent.engine.signals import Signal
from hl_agent.execution.live import HlBroker, LiveMarketSource, Network
from hl_agent.execution.runner import LiveRunner, LoopConfig, RefusedError, check_network
from tests.execution.test_live import ADDR, FakeExchange, FakeInfo, info_handler

H = 3_600_000
CFG = EngineConfig(
    StrategyConfig(slots=2, margin_pct=20, default_leverage=2),
    DslConfig(Phase1(max_loss_pct=10), (Tier(10, 50),)),
)


class Once:
    def __init__(self) -> None:
        self.sent = False
        self.boom = False
        self.before: Callable[[], None] = lambda: None

    def signals(self, now_ms: int) -> Sequence[Signal]:
        self.before()
        if self.boom:
            raise RuntimeError("scanner exploded")
        if self.sent:
            return []
        self.sent = True
        return [Signal("BTC", Direction.LONG, "t", now_ms, now_ms + 60_000, "s1")]


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def build(tmp_path: Path) -> tuple[LiveRunner, Once, list[Event], FakeExchange, Clock]:
    ex = FakeExchange()

    def handler(request: httpx.Request) -> httpx.Response:
        """The venue shows a BTC position while the fake exchange holds one open."""
        opened = sum(c[0] == "market_open" for c in ex.calls)
        closed = sum(c[0] == "market_close" for c in ex.calls)
        if json.loads(request.content)["type"] == "clearinghouseState" and opened > closed:
            pos = {
                "coin": "BTC",
                "szi": "0.001",
                "entryPx": "100000",
                "marginUsed": "50",
                "unrealizedPnl": "0",
                "leverage": {"value": 2},
            }
            return httpx.Response(
                200,
                json={
                    "marginSummary": {"accountValue": "100", "totalMarginUsed": "50"},
                    "withdrawable": "50",
                    "assetPositions": [{"position": pos}],
                },
            )
        return info_handler(request)

    client = HyperliquidClient(transport=httpx.MockTransport(handler))
    market = LiveMarketSource(client, ADDR)
    market.refresh(1_700_000_000 * 1000)
    broker = HlBroker(ex, FakeInfo(), ADDR, market=market)
    source = Once()
    engine = Engine(CFG, market, broker, source, now_ms=market.now_ms)
    sink: list[Event] = []
    clock = Clock()
    runner = LiveRunner(
        engine,
        market,
        LoopConfig(interval_s=10, run_dir=tmp_path, max_consecutive_errors=2),
        sink=sink.append,
        log=lambda _m: None,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return runner, source, sink, ex, clock


def test_runner_ticks_and_paces(tmp_path: Path) -> None:
    runner, _, sink, ex, clock = build(tmp_path)
    assert runner.run(max_ticks=2) == "max_ticks"
    assert runner.ticks == 2 and [e.kind for e in sink] == ["opened"]
    # the fill (100000) differs from the mid used to plan (100000.5): the stop is re-armed
    assert [c[0] for c in ex.calls] == [
        "update_leverage",
        "market_open",
        "order",
        "cancel",
        "order",
    ]
    assert clock.slept == [10.0, 10.0]


def test_stop_file_flattens_and_exits(tmp_path: Path) -> None:
    runner, _, sink, ex, _ = build(tmp_path)
    runner.run(max_ticks=1)
    (tmp_path / "STOP").touch()
    assert runner.run(max_ticks=5) == "stop_file"
    assert runner.ticks == 1 and sink[-1].kind == "closed" and sink[-1].reason == "manual_close"
    assert ex.calls[-1][0] == "market_close"


def test_error_streak_stops_the_loop(tmp_path: Path) -> None:
    runner, source, _, _, _ = build(tmp_path)
    source.boom = True
    assert runner.run() == "too_many_errors" and runner.errors == 2


def test_network_outage_backs_off_instead_of_stopping(tmp_path: Path) -> None:
    runner, source, sink, _, clock = build(tmp_path)
    left = 5

    def flaky() -> None:
        nonlocal left
        left -= 1
        if left >= 0:
            raise RuntimeError("wrapped") from httpx.ConnectError("name resolution")

    source.before = flaky
    assert runner.run(max_ticks=1) == "max_ticks"
    assert runner.errors == 5 and runner.ticks == 1 and [e.kind for e in sink] == ["opened"]
    assert clock.slept[:5] == [20.0, 40.0, 80.0, 160.0, 300.0]


def test_mainnet_needs_explicit_consent() -> None:
    check_network(Network.TESTNET, accept_real_money=False)
    with pytest.raises(RefusedError):
        check_network(Network.MAINNET, accept_real_money=False)
    check_network(Network.MAINNET, accept_real_money=True)
