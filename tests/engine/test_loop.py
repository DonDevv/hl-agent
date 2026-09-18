"""End-to-end engine step with in-memory market/broker fakes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from hl_agent.data.models import AccountState, Direction, Instrument, Position
from hl_agent.engine.config import DslConfig, GuardRails, Phase1, StrategyConfig, Tier
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.guardrails import Book
from hl_agent.engine.loop import Engine, EngineConfig
from hl_agent.engine.ports import Fill
from hl_agent.engine.signals import Signal
from hl_agent.engine.sizing import OrderPlan

MIN = 60_000
FEE = 0.00035
INSTRUMENTS = {
    "BTC": Instrument("BTC", size_decimals=5, max_leverage=40),
    "ETH": Instrument("ETH", size_decimals=4, max_leverage=25),
}


class FakeVenue:
    """Market + broker in one: fills at the current price, tracks positions and cash."""

    def __init__(self, cash: float, prices: dict[str, float]) -> None:
        self.cash = cash
        self.prices = dict(prices)
        self.books: dict[str, Book] = {}
        self.positions: dict[str, tuple[Direction, float, float, int]] = {}
        self.stops: dict[str, float] = {}
        self.closes: list[tuple[str, CloseReason]] = []

    # MarketView
    def price(self, asset: str) -> float | None:
        return self.prices.get(asset)

    def instrument(self, asset: str) -> Instrument | None:
        return INSTRUMENTS.get(asset)

    def order_book(self, asset: str) -> Book | None:
        return self.books.get(asset)

    def account(self) -> AccountState:
        pos = []
        margin = 0.0
        upnl = 0.0
        for asset, (d, size, entry, lev) in self.positions.items():
            m = size * entry / lev
            u = (self.prices[asset] - entry) * d.sign * size
            margin += m
            upnl += u
            pos.append(Position(asset, d, size, entry, lev, m, u, None, u / m * 100))
        return AccountState(self.cash + upnl, self.cash - margin, margin, tuple(pos))

    # Broker
    def open(self, plan: OrderPlan, stop_price: float, now_ms: int) -> Fill:
        px = self.prices[plan.asset]
        fee = plan.size * px * FEE
        self.cash -= fee
        self.positions[plan.asset] = (plan.direction, plan.size, px, plan.leverage)
        self.stops[plan.asset] = stop_price
        return Fill(plan.asset, plan.direction, plan.size, px, fee, now_ms)

    def close(self, asset: str, reason: CloseReason, now_ms: int) -> Fill:
        d, size, entry, _ = self.positions.pop(asset)
        px = self.prices[asset]
        fee = size * px * FEE
        self.cash += (px - entry) * d.sign * size - fee
        self.stops.pop(asset, None)
        self.closes.append((asset, reason))
        return Fill(asset, d, size, px, fee, now_ms)

    def set_stop(self, asset: str, stop_price: float, now_ms: int) -> None:
        self.stops[asset] = stop_price

    def external_close(self, asset: str, now_ms: int) -> tuple[CloseReason, Fill] | None:
        return None


class Queue:
    def __init__(self) -> None:
        self.pending: list[Signal] = []

    def push(self, asset: str, direction: Direction, now_ms: int, **kw: float) -> None:
        self.pending.append(
            Signal(asset, direction, "test", now_ms, now_ms + 300_000, f"{asset}-{now_ms}", **kw)
        )

    def signals(self, now_ms: int) -> Sequence[Signal]:
        out, self.pending = self.pending, []
        return out


CFG = EngineConfig(
    strategy=StrategyConfig(slots=1, margin_pct=30.0, default_leverage=3, max_leverage=3),
    dsl=DslConfig(phase1=Phase1(max_loss_pct=8.0), tiers=(Tier(10, 40),)),
    rails=GuardRails(per_asset_cooldown_seconds=300),
    dedup_window_ms=MIN,
)


def make() -> tuple[Engine, FakeVenue, Queue]:
    venue = FakeVenue(100.0, {"BTC": 50_000.0, "ETH": 3_000.0})
    q = Queue()
    return Engine(CFG, venue, venue, q, now_ms=0), venue, q


def test_full_lifecycle_open_ratchet_stop_close_on_exchange_stop() -> None:
    engine, venue, q = make()
    q.push("BTC", Direction.LONG, 0)
    ev = engine.step(0)
    assert [e.kind for e in ev] == ["opened"]
    assert ev[0].payload["margin_usd"] == 30.0 and ev[0].payload["leverage"] == 3
    assert ev[0].payload["size"] == 0.0018  # 90 / 50000 truncated to 5 dp
    assert venue.stops["BTC"] < 50_000.0

    # +12% ROE (= +4% price at 3x) → tier armed, stop ratchets above entry
    venue.prices["BTC"] = 52_000.0
    ev = engine.step(MIN)
    assert ev[0].kind == "stop_moved" and ev[0].reason == "tier_advanced"
    assert venue.stops["BTC"] > 50_000.0

    # drop back through the floor (4.8% ROE lock → price 50 800): closes as exchange_sl_hit
    venue.prices["BTC"] = 50_700.0
    ev = engine.step(2 * MIN)
    assert ev[0].kind == "closed" and ev[0].reason == "exchange_sl_hit"
    assert ev[0].payload["pnl_usd"] > 0
    assert venue.closes == [("BTC", CloseReason.EXCHANGE_SL_HIT)]
    assert engine.positions == {}


def test_rejections_carry_reason_codes() -> None:
    engine, venue, q = make()
    q.push("DOGE", Direction.LONG, 0)  # unknown instrument → asset_banned
    q.push("BTC", Direction.LONG, 0)
    q.push("BTC", Direction.LONG, 0)  # same scanner re-emits → duplicate
    q.pending.append(Signal("BTC", Direction.SHORT, "other", 0, 300_000, "x"))  # → asset_held
    q.push("ETH", Direction.SHORT, 0)  # slots=1 → no_slots
    reasons = [(e.kind, e.reason) for e in engine.step(0)]
    assert reasons == [
        ("rejected", "asset_banned"),
        ("opened", "submitted"),
        ("rejected", "duplicate"),
        ("rejected", "asset_held"),
        ("rejected", "no_slots"),
    ]
    # expired signal
    q.push("ETH", Direction.LONG, 0)
    venue.prices["BTC"] = 40_000.0  # blows through -8% ROE → dsl_breach frees the slot
    ev = engine.step(10 * MIN)
    assert [(e.kind, e.reason) for e in ev] == [
        ("closed", "dsl_breach"),
        ("rejected", "signal_expired"),
    ]


def test_asset_cooldown_after_close_then_reentry() -> None:
    engine, venue, q = make()
    q.push("BTC", Direction.LONG, 0)
    engine.step(0)
    venue.prices["BTC"] = 40_000.0
    engine.step(MIN)  # dsl_breach
    q.push("BTC", Direction.LONG, 2 * MIN)
    assert engine.step(2 * MIN)[0].reason == "risk_gate_asset_cooldown"
    q.push("BTC", Direction.LONG, 7 * MIN)
    assert engine.step(7 * MIN)[0].reason == "submitted"


def test_external_close_is_reconciled() -> None:
    engine, venue, q = make()
    q.push("BTC", Direction.LONG, 0)
    engine.step(0)
    venue.positions.clear()  # closed by hand on the exchange
    ev = engine.step(MIN)
    assert [(e.kind, e.reason) for e in ev] == [("closed", "closed_externally")]
    assert engine.rails.last_close_ms["BTC"] == MIN


def test_second_entry_in_same_tick_is_sized_on_refreshed_withdrawable() -> None:
    cfg = EngineConfig(
        strategy=StrategyConfig(slots=3, margin_pct=60.0, default_leverage=2, max_leverage=3),
        dsl=CFG.dsl,
    )
    venue = FakeVenue(100.0, {"BTC": 50_000.0, "ETH": 3_000.0})
    q = Queue()
    engine = Engine(cfg, venue, venue, q, now_ms=0)
    q.push("BTC", Direction.LONG, 0)
    q.push("ETH", Direction.LONG, 0)
    ev = engine.step(0)
    assert [e.reason for e in ev] == ["submitted", "submitted"]
    # BTC took 60% of $100; ETH gets 60% of what is left (~$40), not of the stale $100
    assert ev[0].payload["margin_usd"] == pytest.approx(60.0, abs=0.5)
    assert ev[1].payload["margin_usd"] == pytest.approx(24.0, abs=0.5)


def test_source_close_requests_close_tracked_positions_only() -> None:
    engine, venue, q = make()
    requests: list[list[str]] = []

    class Exits:
        def close_requests(self, now_ms: int) -> Sequence[str]:
            return requests.pop(0) if requests else []

    engine = Engine(CFG, venue, venue, q, now_ms=0, exits=Exits())
    q.push("BTC", Direction.LONG, 0)
    engine.step(0)
    requests.append(["ETH", "BTC"])  # ETH is not ours: ignored, no broker call
    ev = engine.step(MIN)
    assert [(e.kind, e.reason) for e in ev] == [("closed", "source_closed")]
    assert venue.closes == [("BTC", CloseReason.SOURCE_CLOSED)] and engine.positions == {}
    assert engine.rails.last_close_ms["BTC"] == MIN


def test_liquidity_gate_blocks_entry_and_reports_the_book() -> None:
    cfg = replace(CFG, rails=GuardRails(max_spread_pct=0.3, min_depth_multiple=20))
    venue = FakeVenue(100.0, {"BTC": 50_000.0, "ETH": 3_000.0})
    venue.books["BTC"] = {"bids": [(49_000.0, 1.0)], "asks": [(51_000.0, 1.0)]}  # 4% spread
    q = Queue()
    engine = Engine(cfg, venue, venue, q, now_ms=0)
    q.push("BTC", Direction.LONG, 0)
    (ev,) = engine.step(0)
    assert (ev.kind, ev.reason) == ("rejected", "risk_gate_liquidity")
    assert ev.payload["spread_pct"] == 4.0 and ev.payload["notional_usd"] > 0
    assert not venue.positions
    # deep book → same signal goes through
    venue.books["BTC"] = {"bids": [(49_990.0, 10.0)], "asks": [(50_010.0, 10.0)]}
    q.push("BTC", Direction.LONG, MIN)
    assert [(e.kind, e.reason) for e in engine.step(MIN)] == [("opened", "submitted")]
