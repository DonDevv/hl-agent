"""Trader discovery from public data only.

Senpi's ``discovery_*`` / ``leaderboard_*`` tools sit on a private indexer. What is public:

* Hyperliquid's own leaderboard (one ~40 MB JSON, refreshed by them a few times a day):
  account value plus PnL / ROI / volume over day, week, month and all-time for ~45k
  addresses. Mainnet only — that is where the traders are, whatever network *we* trade.
* ``clearinghouseState`` for any address: the live book we would mirror.

Blueprint section 2 is reproduced on top of that: blend of views (7d ROI, 30d ROI,
30d PnL), then per-candidate *mirrorability* (fresh entry surface, concentration, margin
usage, minimum budget). Win rate / drawdown from closed trades is v2.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from hl_agent.copy.mirror import MirrorPlan, simulate_mirror
from hl_agent.data.models import AccountState

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_MAX_AGE_S = 6 * 3600
# Plausibility bounds for a copyable account. Above MAX_ROI the window's return is a
# lottery ticket or an equity artefact (a $50 account that hit +80 000 %); above
# MAX_ACCOUNT_VALUE it is a vault or market maker whose book is not a directional view.
MAX_ROI = 5.0  # +500 % on the window
MAX_ACCOUNT_VALUE = 5_000_000.0

Window = Literal["day", "week", "month", "allTime"]
WINDOWS: tuple[Window, ...] = ("day", "week", "month", "allTime")
Metric = Literal["roi", "pnl"]

# Blueprint 2.1: a trader present in several views is a better candidate than one in one.
VIEWS: tuple[tuple[str, Window, Metric], ...] = (
    ("7d_roi", "week", "roi"),
    ("30d_roi", "month", "roi"),
    ("30d_pnl", "month", "pnl"),
)


@dataclass(frozen=True, slots=True)
class LeaderRow:
    address: str
    account_value: float
    pnl: Mapping[Window, float]
    roi: Mapping[Window, float]  # fraction, 0.05 == +5 %
    volume: Mapping[Window, float]
    display_name: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> LeaderRow:
        perf = {str(w): dict(p) for w, p in raw.get("windowPerformances", [])}
        return cls(
            address=str(raw["ethAddress"]).lower(),
            account_value=float(raw.get("accountValue", 0.0)),
            pnl={w: float(perf.get(w, {}).get("pnl", 0.0)) for w in WINDOWS},
            roi={w: float(perf.get(w, {}).get("roi", 0.0)) for w in WINDOWS},
            volume={w: float(perf.get(w, {}).get("vlm", 0.0)) for w in WINDOWS},
            display_name=raw.get("displayName") or None,
        )


def parse_leaderboard(raw: Mapping[str, Any]) -> list[LeaderRow]:
    return [LeaderRow.from_api(r) for r in raw.get("leaderboardRows", [])]


def fetch_leaderboard(
    cache_path: Path,
    *,
    max_age_s: float = LEADERBOARD_MAX_AGE_S,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
    now: float | None = None,
) -> list[LeaderRow]:
    """The leaderboard, from ``cache_path`` when younger than ``max_age_s``."""
    now = time.time() if now is None else now
    if not refresh and cache_path.exists() and now - cache_path.stat().st_mtime < max_age_s:
        return parse_leaderboard(json.loads(cache_path.read_text(encoding="utf-8")))
    with httpx.Client(timeout=120.0, transport=transport) as http:
        resp = http.get(LEADERBOARD_URL)
        resp.raise_for_status()
        raw = resp.json()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(raw), encoding="utf-8")
    return parse_leaderboard(raw)


def rank(
    rows: Iterable[LeaderRow],
    window: Window,
    metric: Metric = "roi",
    *,
    min_account_value: float = 10_000.0,
    max_account_value: float = MAX_ACCOUNT_VALUE,
    max_roi: float = MAX_ROI,
    min_volume: float = 0.0,
    top: int = 20,
) -> list[LeaderRow]:
    """Top ``top`` rows by ``metric`` over ``window`` among *plausible* accounts: equity
    within [min, max], window ROI positive and below ``max_roi``, volume floor."""
    pool = [
        r
        for r in rows
        if min_account_value <= r.account_value <= max_account_value
        and 0 < r.roi[window] <= max_roi
        and r.volume[window] >= min_volume
    ]
    pool.sort(key=lambda r: getattr(r, metric)[window], reverse=True)
    return pool[:top]


def blend(
    rows: Sequence[LeaderRow],
    *,
    top: int = 20,
    min_account_value: float = 10_000.0,
    max_account_value: float = MAX_ACCOUNT_VALUE,
    max_roi: float = MAX_ROI,
) -> dict[str, list[str]]:
    """Union of the blueprint's three views → ``{address: [view names it appears in]}``,
    traders seen in the most views first."""
    seen: dict[str, list[str]] = {}
    for view, window, metric in VIEWS:
        ranked = rank(
            rows,
            window,
            metric,
            min_account_value=min_account_value,
            max_account_value=max_account_value,
            max_roi=max_roi,
            top=top,
        )
        for r in ranked:
            seen.setdefault(r.address, []).append(view)
    return dict(sorted(seen.items(), key=lambda kv: len(kv[1]), reverse=True))


# ---- enrichment ----------------------------------------------------------------------

Fit = Literal["good", "partial", "poor", "unknown"]


@dataclass(frozen=True, slots=True)
class TraderProfile:
    row: LeaderRow
    state: AccountState
    plan: MirrorPlan  # dry-run at the caller's budget
    seen_in: tuple[str, ...] = ()

    @property
    def open_positions(self) -> int:
        return len(self.state.positions)

    @property
    def longs(self) -> int:
        return sum(1 for p in self.state.positions if p.direction.value == "LONG")

    @property
    def margin_ratio(self) -> float:
        v = self.state.account_value
        return self.state.total_margin_used / v if v > 0 else 0.0

    @property
    def top_asset_share(self) -> float:
        """Share of the book's notional in its largest position (blueprint: > 70 % → flag)."""
        notionals = [p.notional for p in self.state.positions]
        return max(notionals) / sum(notionals) if notionals and sum(notionals) > 0 else 0.0

    @property
    def fit(self) -> Fit:
        pct = self.plan.fresh_notional_pct
        if pct is None:
            return "unknown"
        return "good" if pct >= 60 else "partial" if pct >= 20 else "poor"

    @property
    def flags(self) -> tuple[str, ...]:
        out: list[str] = []
        n = self.open_positions
        if n == 0:
            out.append("no_positions")
        elif n == 1:
            out.append("single_position")
        if 0 < n < 3 or self.top_asset_share > 0.70:
            out.append("concentrated_book")
        if self.margin_ratio > 0.90:
            out.append("critical_margin_usage")
        if self.row.roi["allTime"] < 0:
            out.append("negative_all_time")
        if self.plan.lines and not self.plan.to_open:
            out.append("nothing_copyable_at_budget")
        return tuple(out)


def profile(
    row: LeaderRow,
    state: AccountState,
    prices: Mapping[str, float],
    *,
    budget_usd: float,
    seen_in: Sequence[str] = (),
    slippage_pct: float = 3.0,
    max_leverage: int = 3,
) -> TraderProfile:
    plan = simulate_mirror(
        state, budget_usd, prices, slippage_pct=slippage_pct, max_leverage=max_leverage
    )
    return TraderProfile(row, state, plan, tuple(seen_in))


def sort_profiles(profiles: Iterable[TraderProfile]) -> list[TraderProfile]:
    """Blueprint 2.3 ranking: copyable first, then mirror fit, fresh surface, 30d ROI."""
    fit_rank = {"good": 3, "partial": 2, "poor": 1, "unknown": 0}
    return sorted(
        profiles,
        key=lambda p: (
            "nothing_copyable_at_budget" not in p.flags and "no_positions" not in p.flags,
            "critical_margin_usage" not in p.flags,
            fit_rank[p.fit],
            len(p.seen_in),
            p.plan.fresh_notional_pct or 0.0,
            p.row.roi["month"],
        ),
        reverse=True,
    )
