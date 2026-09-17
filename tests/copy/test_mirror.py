"""Pure mirror maths and the poll-based ``CopySource``."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from hl_agent.copy.mirror import (
    CopyConfig,
    CopySource,
    diff_books,
    scale_factor,
    simulate_mirror,
    within_slippage,
)
from hl_agent.data.models import AccountState, Direction, Position

L, S = Direction.LONG, Direction.SHORT


def pos(asset: str, d: Direction, notional: float, entry: float, lev: int) -> Position:
    margin = notional / lev
    return Position(asset, d, notional / entry, entry, lev, margin, 0.0, None, 0.0)


def book(*positions: Position, equity: float = 10_000.0) -> AccountState:
    used = sum(p.margin_used for p in positions)
    return AccountState(equity, equity - used, used, tuple(positions))


# ---- simulate_mirror -------------------------------------------------------------------


def test_proportional_margin_leverage_capped_largest_first() -> None:
    # BTC: 4000 notional at 20x = 200 margin = 2 % of equity; ETH: 3000 at 3x = 1000 = 10 %
    og = book(pos("ETH", S, 3000, 3000.0, 3), pos("BTC", L, 4000, 50_000.0, 20))
    plan = simulate_mirror(og, 1000.0, {"BTC": 50_000.0, "ETH": 3000.0}, max_leverage=3)
    assert [ln.asset for ln in plan.lines] == ["BTC", "ETH"]  # by notional, largest first
    btc, eth = plan.lines
    assert btc.leverage == 3 and btc.og_leverage == 20  # their 20x clamped to our cap
    assert btc.margin_usd == pytest.approx(20.0) and btc.notional_usd == pytest.approx(60.0)
    assert eth.leverage == 3 and eth.margin_usd == pytest.approx(100.0)
    assert plan.scale_factor == 1.0 and plan.to_open == plan.lines
    assert plan.margin_committed_usd == pytest.approx(120.0)
    assert plan.fresh_notional_pct == 100.0


def test_min_notional_bump_and_multiplier() -> None:
    og = book(pos("BTC", L, 200, 100.0, 2))  # 1 % of equity
    plan = simulate_mirror(og, 100.0, {"BTC": 100.0})
    (ln,) = plan.lines
    assert ln.notional_usd == 12.0 and ln.margin_usd == 6.0  # bumped from 2 -> 12 notional
    plan = simulate_mirror(og, 100.0, {"BTC": 100.0}, multiplier=10.0)
    assert plan.lines[0].margin_usd == pytest.approx(10.0)


def test_scale_factor_caps_over_committed_books() -> None:
    og = book(pos("BTC", L, 9500, 100.0, 1))  # 95 % of equity in margin
    assert scale_factor(og) == pytest.approx(0.90 / 0.95)
    plan = simulate_mirror(og, 100.0, {"BTC": 100.0})
    assert plan.lines[0].margin_usd == pytest.approx(90.0)
    assert scale_factor(book(pos("BTC", L, 100, 100.0, 1))) == 1.0
    assert scale_factor(AccountState(0.0, 0.0, 0.0, ())) == 1.0


def test_slippage_is_per_direction() -> None:
    assert within_slippage(L, 100.0, 103.0, 3.0) and not within_slippage(L, 100.0, 103.1, 3.0)
    assert within_slippage(L, 100.0, 50.0, 3.0)  # better than their entry is always fine
    assert within_slippage(S, 100.0, 97.0, 3.0) and not within_slippage(S, 100.0, 96.9, 3.0)
    assert within_slippage(S, 100.0, 150.0, 3.0)


def test_verdicts_slippage_budget_price_and_fresh_share() -> None:
    og = book(
        pos("BTC", L, 6000, 100.0, 1),  # ran away: +10 %
        pos("ETH", S, 3000, 100.0, 1),  # fine
        pos("SOL", L, 1000, 100.0, 1),  # no price
    )
    plan = simulate_mirror(og, 100.0, {"BTC": 110.0, "ETH": 100.0})
    by = {ln.asset: ln for ln in plan.lines}
    assert by["BTC"].verdict == "skip_slippage"
    assert by["BTC"].moved_from_entry_pct == pytest.approx(10.0)
    assert by["ETH"].verdict == "open" and by["SOL"].verdict == "skip_price"
    assert plan.fresh_notional_pct == pytest.approx(3000 / 9000 * 100)  # SOL unpriced: excluded
    assert plan.skipped == (by["BTC"], by["SOL"])

    # Proportional margins always fit the budget; only the min-notional bump can overflow it:
    # at 15 $ both lines bump to 12 $ and the second one no longer fits.
    tight = simulate_mirror(og, 15.0, {"BTC": 100.0, "ETH": 100.0})
    assert [ln.verdict for ln in tight.lines] == ["open", "skip_budget", "skip_price"]
    assert tight.min_budget_usd == pytest.approx(24.0)  # what would have opened both
    assert simulate_mirror(book(), 100.0, {}).fresh_notional_pct is None


# ---- diff_books ------------------------------------------------------------------------


def test_diff_books_new_closed_flipped() -> None:
    before = {"BTC": pos("BTC", L, 100, 1.0, 1), "ETH": pos("ETH", L, 100, 1.0, 1)}
    after = {"BTC": pos("BTC", S, 100, 1.0, 1), "SOL": pos("SOL", L, 100, 1.0, 1)}
    kinds = {(c.kind, c.position.asset) for c in diff_books(before, after)}
    assert kinds == {("flipped", "BTC"), ("new", "SOL"), ("closed", "ETH")}
    assert diff_books(before, before) == ()


# ---- CopySource ------------------------------------------------------------------------


class FakeFeed:
    def __init__(self, state: AccountState, prices: Mapping[str, float]) -> None:
        self.state, self.prices, self.calls = state, dict(prices), 0

    def account_state(self, address: str) -> AccountState:
        self.calls += 1
        return self.state

    def all_mids(self) -> Mapping[str, float]:
        return self.prices


OURS = AccountState(1000.0, 800.0, 200.0, ())
PRICES = {"BTC": 100.0, "ETH": 100.0, "SOL": 100.0}


def make(state: AccountState, **cfg: object) -> tuple[CopySource, FakeFeed]:
    feed = FakeFeed(state, PRICES)
    config = CopyConfig("0xABCDEF0123456789", poll_ms=1000, **cfg)  # type: ignore[arg-type]
    src = CopySource(feed, config, lambda: OURS)
    return src, feed


def test_config_validation_and_scanner_name() -> None:
    src, _ = make(book())
    assert src.scanner == "copy:0xABCDEF01"
    for bad in (dict(multiplier=0), dict(slippage_pct=25), dict(poll_ms=10), dict(budget_usd=0)):
        with pytest.raises(ValueError):
            CopyConfig("0x1", **bad)  # type: ignore[arg-type]


def test_initial_book_is_mirrored_then_only_polled_every_interval() -> None:
    src, feed = make(book(pos("BTC", L, 500, 100.0, 5)), budget_usd=100.0)
    sigs = src.signals(0)
    assert [s.asset for s in sigs] == ["BTC"]
    sig = sigs[0]
    # 1 % allocation of a 100 $ budget = 1 $ margin, bumped to 12 $ notional at 3x -> 4 $
    assert sig.margin_pct == pytest.approx(4.0 / OURS.withdrawable * 100)
    assert sig.leverage == 5.0 and sig.scanner == src.scanner  # engine clamps to our cap
    assert sig.valid_until_ms == 1000 and sig.data["allocation_pct"] == pytest.approx(1.0)
    assert src.signals(500) == [] and feed.calls == 1  # inside the poll window
    assert src.signals(1000) == [] and feed.calls == 2  # unchanged book: nothing new


def test_no_initial_skips_the_startup_book() -> None:
    src, _ = make(book(pos("BTC", L, 500, 100.0, 5)), mirror_existing=False)
    assert src.signals(0) == [] and list(src.book) == ["BTC"]


def test_new_closed_and_flipped_positions() -> None:
    src, feed = make(book(pos("BTC", L, 500, 100.0, 5), pos("ETH", L, 500, 100.0, 5)))
    src.signals(0)
    feed.state = book(pos("BTC", S, 500, 100.0, 5), pos("SOL", L, 500, 100.0, 5))
    sigs = src.signals(1000)
    assert [s.asset for s in sigs] == ["SOL"]  # new only; the flip waits one tick
    assert sorted(src.close_requests(1000)) == ["BTC", "ETH"]
    assert src.close_requests(1000) == []  # consumed
    flip = src.signals(1500)  # pending flip re-enters even between polls
    assert [(s.asset, s.direction) for s in flip] == [("BTC", S)]
    assert feed.calls == 2


def test_price_filters_drop_signals_but_budget_does_not() -> None:
    feed_state = book(pos("BTC", L, 500, 100.0, 5), pos("ETH", S, 5000, 100.0, 1))
    src, feed = make(feed_state, budget_usd=10.0)
    feed.prices["BTC"] = 110.0  # ran away
    sigs = src.signals(0)
    assert [s.asset for s in sigs] == ["ETH"]  # 50 $ margin > 10 $ budget: engine decides
    assert src.last_plan is not None and src.last_plan.lines[1].verdict == "skip_slippage"


def test_budget_defaults_to_our_equity_and_no_withdrawable_means_no_entries() -> None:
    src, _ = make(book(pos("BTC", L, 500, 100.0, 5)))
    (sig,) = src.signals(0)
    assert sig.margin_pct == pytest.approx(10.0 / OURS.withdrawable * 100)  # 1 % of 1000 $
    broke = CopySource(
        FakeFeed(book(pos("BTC", L, 500, 100.0, 5)), PRICES),
        CopyConfig("0x1", poll_ms=1000),
        lambda: AccountState(1000.0, 0.0, 1000.0, ()),
    )
    assert broke.signals(0) == []
