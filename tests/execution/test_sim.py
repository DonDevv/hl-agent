from __future__ import annotations

from pathlib import Path

import pytest

from hl_agent.data.history import CandleStore
from hl_agent.data.models import Candle, Direction, Instrument
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.sizing import OrderPlan
from hl_agent.execution.sim import SimBroker, SimConfig
from hl_agent.strategy.sources import ReplaySource

H = 3_600_000
BTC = Instrument("BTC", size_decimals=5, max_leverage=40)


def bar(i: int, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle("BTC", "1h", i * H, (i + 1) * H - 1, o, h, lo, c, 1.0, 1)


def make(tmp_path: Path, bars: list[Candle], cfg: SimConfig | None = None) -> SimBroker:
    store = CandleStore(tmp_path / "c")
    store.append("BTC", "1h", bars)
    holder: list[SimBroker] = []
    src = ReplaySource(store, [BTC], lambda: holder[0].account())
    src.now_ms = (bars[0].open_ms // H + 1) * H  # right after the first bar closed
    broker = SimBroker(src, cfg or SimConfig(), 100.0)
    holder.append(broker)
    return broker


def plan(direction: Direction = Direction.LONG, size: float = 0.001, lev: int = 2) -> OrderPlan:
    return OrderPlan("BTC", direction, size, lev, 50.0, 100.0, 100_000.0)


FLAT = [bar(i, 100_000, 100_100, 99_900, 100_000) for i in range(6)]


def test_open_pays_fee_and_slippage(tmp_path: Path) -> None:
    b = make(tmp_path, FLAT)
    fill = b.open(plan(), 98_000.0, b._src.now_ms)
    assert fill.price == pytest.approx(100_000 * 1.0005)
    assert fill.fee_usd == pytest.approx(0.001 * fill.price * 0.00035)
    assert b.cash == pytest.approx(100.0 - fill.fee_usd)
    acct = b.account()
    assert acct.total_margin_used == pytest.approx(0.001 * fill.price / 2)
    assert acct.position("BTC") is not None and acct.position("BTC").leverage == 2  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        b.open(plan(), 98_000.0, b._src.now_ms)


def test_withdrawable_ignores_unrealised_profit(tmp_path: Path) -> None:
    bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 120, 100, 120), bar(2, 120, 125, 60, 60)]
    b = make(tmp_path, bars)
    b.open(OrderPlan("BTC", Direction.LONG, 0.5, 1, 50.0, 50.0, 100.0), 1.0, b._src.now_ms)
    b._src.now_ms = 2 * H
    b.mark(2 * H)
    a = b.account()
    assert a.account_value > 100 and a.withdrawable == pytest.approx(b.cash - a.total_margin_used)
    b._src.now_ms = 3 * H
    b.mark(3 * H)
    a = b.account()
    assert a.account_value < 100 and a.withdrawable == pytest.approx(
        a.account_value - a.total_margin_used
    )


def test_stop_fills_intra_bar_at_stop_price(tmp_path: Path) -> None:
    bars = [*FLAT[:2], bar(2, 100000, 100200, 97500, 99800), *FLAT[3:]]
    b = make(tmp_path, bars)
    b.open(plan(), 98_000.0, H)
    b._src.now_ms = 2 * H
    b.mark(2 * H)  # bar 1 (flat) — untouched
    assert b.external_close("BTC", 2 * H) is None and "BTC" in b._positions
    b._src.now_ms = 3 * H
    b.mark(3 * H)  # bar 2 dips through the stop
    hit = b.external_close("BTC", 3 * H)
    assert hit is not None
    reason, fill = hit
    assert reason is CloseReason.EXCHANGE_SL_HIT
    assert fill.price == pytest.approx(98_000 * (1 - 0.0005)) and fill.time_ms == 3 * H - 1
    assert b.external_close("BTC", 3 * H) is None  # reported once
    assert b.account().positions == ()


def test_gap_through_stop_fills_at_open_and_short_uses_high(tmp_path: Path) -> None:
    bars = [*FLAT[:2], bar(2, 96000, 97000, 95000, 96500)]
    b = make(tmp_path, bars)
    b.open(plan(), 98_000.0, H)
    b._src.now_ms = 3 * H
    b.mark(3 * H)
    hit = b.external_close("BTC", 3 * H)
    assert hit is not None and hit[1].price == pytest.approx(96_000 * (1 - 0.0005))

    bars = [*FLAT[:2], bar(2, 100000, 103000, 99000, 100500)]
    b = make(tmp_path / "s", bars)
    b.open(plan(Direction.SHORT), 102_000.0, H)
    b._src.now_ms = 3 * H
    b.mark(3 * H)
    hit = b.external_close("BTC", 3 * H)
    assert hit is not None and hit[1].price == pytest.approx(102_000 * 1.0005)


def test_set_stop_moves_floor_and_close_realises(tmp_path: Path) -> None:
    bars = [*FLAT[:3], bar(3, 100000, 101000, 99500, 100800)]
    b = make(tmp_path, bars)
    b.open(plan(), 98_000.0, H)
    b.set_stop("BTC", 99_600.0, 2 * H)
    b._src.now_ms = 4 * H
    b.mark(4 * H)
    assert b.external_close("BTC", 4 * H) is not None  # 99_500 low crossed the new floor
    b = make(tmp_path / "c2", bars)
    b.open(plan(), 98_000.0, H)
    b._src.now_ms = 4 * H
    b.mark(4 * H)
    fill = b.close("BTC", CloseReason.HARD_TIMEOUT, 4 * H)
    assert fill.price == pytest.approx(100_800 * (1 - 0.0005))
    assert b.cash == pytest.approx(100 + (fill.price - 100_050) * 0.001 - b.fees_paid)


def test_funding_charged_hourly(tmp_path: Path) -> None:
    b = make(tmp_path, FLAT, SimConfig(taker_fee_bps=0, slippage_bps=0, funding_hourly=0.001))
    b.open(plan(), 90_000.0, H)
    b._src.now_ms = 4 * H
    b.mark(4 * H)  # bars 1, 2, 3 closed since
    assert b.funding_paid == pytest.approx(0.001 * 100_000 * 0.001 * 3)
    assert b.cash == pytest.approx(100 - b.funding_paid)
    b = make(tmp_path / "s", FLAT, SimConfig(taker_fee_bps=0, slippage_bps=0, funding_hourly=0.001))
    b.open(plan(Direction.SHORT), 110_000.0, H)
    b._src.now_ms = 2 * H
    b.mark(2 * H)
    assert b.funding_paid < 0  # shorts receive when funding is positive


def test_liquidation_when_stop_is_beyond_margin(tmp_path: Path) -> None:
    bars = [*FLAT[:2], bar(2, 100000, 100000, 50000, 55000)]
    b = make(tmp_path, bars, SimConfig(slippage_bps=0))
    b.open(plan(lev=2), 40_000.0, H)  # stop far below the -100% ROE line
    b._src.now_ms = 3 * H
    b.mark(3 * H)
    hit = b.external_close("BTC", 3 * H)
    assert hit is not None and hit[0] is CloseReason.LIQUIDATED
    assert hit[1].price == pytest.approx(100_000 * (1 - 1 / 2))
