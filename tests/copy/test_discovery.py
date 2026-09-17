"""Leaderboard parsing / caching, view blending and candidate profiling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from hl_agent.copy.discovery import (
    LEADERBOARD_URL,
    WINDOWS,
    LeaderRow,
    blend,
    fetch_leaderboard,
    parse_leaderboard,
    profile,
    rank,
    sort_profiles,
)
from hl_agent.data.models import AccountState, Direction
from tests.copy.test_mirror import book, pos

L = Direction.LONG


def raw_row(addr: str, equity: float, **perf: tuple[float, float, float]) -> dict[str, Any]:
    """``perf``: window -> (pnl, roi, vlm), as the API nests them."""
    return {
        "ethAddress": addr,
        "accountValue": str(equity),
        "windowPerformances": [
            [w, {"pnl": str(p), "roi": str(r), "vlm": str(v)}] for w, (p, r, v) in perf.items()
        ],
        "prize": 0,
        "displayName": None,
    }


ROWS = [
    raw_row("0xA", 50_000, week=(500, 0.10, 1e6), month=(2000, 0.40, 5e6), allTime=(1, 0.5, 1)),
    raw_row("0xB", 20_000, week=(9000, 0.90, 1e6), month=(100, 0.01, 5e6), allTime=(1, 0.5, 1)),
    raw_row("0xC", 50, week=(400, 80.0, 1e3), month=(400, 80.0, 1e3)),  # lottery ticket
    raw_row("0xD", 9e9, week=(1e7, 0.02, 1e9), month=(5e7, 0.05, 1e9)),  # vault
    raw_row("0xE", 30_000, week=(-100, -0.05, 1e5), month=(60_000, 2.0, 1e6)),  # loser this week
]


def test_parse_normalises_types_and_case() -> None:
    rows = parse_leaderboard({"leaderboardRows": ROWS})
    a = rows[0]
    assert a.address == "0xa" and a.account_value == 50_000.0
    assert a.roi["month"] == 0.40 and a.pnl["week"] == 500.0 and a.volume["day"] == 0.0
    assert a.display_name is None


def test_fetch_uses_cache_until_stale(tmp_path: Path) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(req.url) == LEADERBOARD_URL
        return httpx.Response(200, json={"leaderboardRows": ROWS[:2]})

    transport = httpx.MockTransport(handler)
    cache = tmp_path / "lb.json"
    rows = fetch_leaderboard(cache, transport=transport, now=1000.0)
    assert len(rows) == 2 and calls == 1 and json.loads(cache.read_text())["leaderboardRows"]
    mtime = cache.stat().st_mtime
    fetch_leaderboard(cache, transport=transport, now=mtime + 10, max_age_s=3600)
    assert calls == 1  # fresh: served from disk
    fetch_leaderboard(cache, transport=transport, now=mtime + 7200, max_age_s=3600)
    assert calls == 2  # stale
    fetch_leaderboard(cache, transport=transport, now=mtime + 10, refresh=True)
    assert calls == 3


def test_rank_filters_implausible_accounts() -> None:
    rows = parse_leaderboard({"leaderboardRows": ROWS})
    assert [r.address for r in rank(rows, "week")] == ["0xb", "0xa"]  # C, D, E out
    assert [r.address for r in rank(rows, "month", "pnl")] == ["0xe", "0xa", "0xb"]
    assert [r.address for r in rank(rows, "month", top=1)] == ["0xe"]
    assert rank(rows, "week", min_volume=5e6) == []
    assert [r.address for r in rank(rows, "week", max_roi=0.5)] == ["0xa"]


def test_blend_orders_by_number_of_views() -> None:
    rows = parse_leaderboard({"leaderboardRows": ROWS})
    seen = blend(rows, top=2)
    assert seen["0xa"] == ["7d_roi", "30d_roi", "30d_pnl"]
    assert next(iter(seen)) == "0xa" and set(seen) == {"0xa", "0xb", "0xe"}


# ---- profiles --------------------------------------------------------------------------


def row(addr: str, **roi: float) -> LeaderRow:
    zero = dict.fromkeys(WINDOWS, 0.0)
    return LeaderRow(addr, 50_000.0, pnl=zero, roi={**zero, **roi}, volume=zero)  # type: ignore[dict-item]


def test_profile_metrics_and_flags() -> None:
    state = book(pos("BTC", L, 8000, 100.0, 2), pos("ETH", L, 2000, 100.0, 2))
    p = profile(
        row("0x1", month=0.5, allTime=-0.1),
        state,
        {"BTC": 100.0, "ETH": 100.0},
        budget_usd=100.0,
        seen_in=("7d_roi",),
    )
    assert p.open_positions == 2 and p.longs == 2
    assert p.top_asset_share == 0.8 and p.margin_ratio == 0.5
    assert p.fit == "good" and p.flags == ("concentrated_book", "negative_all_time")

    flat = profile(row("0x2"), book(), {}, budget_usd=100.0)
    assert flat.fit == "unknown" and flat.flags == ("no_positions",)

    maxed = AccountState(10_000.0, 100.0, 9_500.0, (pos("BTC", L, 9500, 100.0, 1),))
    p = profile(row("0x3"), maxed, {"BTC": 200.0}, budget_usd=100.0)  # ran away: nothing opens
    assert p.fit == "poor" and set(p.flags) == {
        "single_position",
        "concentrated_book",
        "critical_margin_usage",
        "nothing_copyable_at_budget",
    }


def test_sort_profiles_copyable_then_fit_then_views_then_roi() -> None:
    prices = {"BTC": 100.0, "ETH": 100.0}
    good_hi = profile(
        row("good_hi", month=0.9), book(pos("BTC", L, 1000, 100.0, 2)), prices, budget_usd=100.0
    )
    good_lo = profile(
        row("good_lo", month=0.1),
        book(pos("BTC", L, 1000, 100.0, 2)),
        prices,
        budget_usd=100.0,
        seen_in=("7d_roi", "30d_roi"),
    )
    partial = profile(
        row("partial", month=2.0),
        book(pos("BTC", L, 5000, 100.0, 2), pos("ETH", L, 4000, 100.0, 2)),
        {"BTC": 100.0, "ETH": 150.0},
        budget_usd=100.0,
    )
    flat = profile(row("flat", month=9.0), book(), prices, budget_usd=100.0)
    order = [p.row.address for p in sort_profiles([flat, partial, good_hi, good_lo])]
    assert order == ["good_lo", "good_hi", "partial", "flat"]
