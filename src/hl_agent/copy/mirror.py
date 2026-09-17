"""Mirror engine: turn a trader's perp book into positions sized for *our* budget.

Ported from Senpi's copy-trading blueprint (sections 3, 5 and 6) minus what is Senpi
infrastructure (sub-wallets, builder fee, their 4h leaderboard). Deliberate differences:

* the trader's leverage is copied only up to our cap (``max_leverage``): a 20x trader
  mirrored at 3x opens 6.7x less notional for the same margin — margin-proportional, not
  notional-proportional;
* their stop-losses are never copied; our DSL and guard rails apply on top;
* margin resizes are ignored in v1 (opens, closes and flips only);
* everything is poll-based (``clearinghouseState`` every ``poll_ms``): a position the trader
  opens and closes inside one poll window is invisible — filter high-turnover traders.

``simulate_mirror`` is pure and reused by the ``mirror-sim`` dry-run, the initial snapshot
of a live mirror, and every new position detected afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from hl_agent.data.models import AccountState, Direction, Position
from hl_agent.engine.signals import Signal

MIN_MIRROR_NOTIONAL_USD = 12.0  # Senpi's cushion above Hyperliquid's $10 venue minimum
SCALE_FACTOR_CAP = 0.90  # never mirror a book that uses more than 90 % of its equity
DEFAULT_POLL_MS = 300_000

Verdict = Literal["open", "skip_slippage", "skip_budget", "skip_price"]


class TraderFeed(Protocol):
    """Where the copied trader's book and reference prices come from (mainnet, always:
    leaderboard traders live there even when *we* trade on testnet)."""

    def account_state(self, address: str) -> AccountState: ...
    def all_mids(self) -> Mapping[str, float]: ...


@dataclass(frozen=True, slots=True)
class MirrorLine:
    asset: str
    direction: Direction
    og_entry_price: float
    og_leverage: int
    og_margin_used: float
    allocation: float  # share of the trader's equity behind this position
    price: float | None
    leverage: int  # what we would use: min(theirs, our cap)
    margin_usd: float
    notional_usd: float
    verdict: Verdict

    @property
    def moved_from_entry_pct(self) -> float | None:
        if self.price is None:
            return None
        return abs(self.price / self.og_entry_price - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class MirrorPlan:
    lines: tuple[MirrorLine, ...]
    budget_usd: float
    scale_factor: float
    og_account_value: float

    @property
    def to_open(self) -> tuple[MirrorLine, ...]:
        return tuple(ln for ln in self.lines if ln.verdict == "open")

    @property
    def skipped(self) -> tuple[MirrorLine, ...]:
        return tuple(ln for ln in self.lines if ln.verdict != "open")

    @property
    def margin_committed_usd(self) -> float:
        return sum(ln.margin_usd for ln in self.to_open)

    @property
    def min_budget_usd(self) -> float:
        """Budget that would have opened everything the slippage filter let through."""
        return sum(ln.margin_usd for ln in self.lines if ln.verdict in ("open", "skip_budget"))

    @property
    def fresh_notional_pct(self) -> float | None:
        """Share of the trader's book (by notional) still within the slippage band —
        Senpi's *mirror fit*: >= 60 good, 20-60 partial, < 20 poor."""
        priced = [ln for ln in self.lines if ln.price is not None]
        total = sum(ln.og_margin_used * ln.og_leverage for ln in priced)
        if total <= 0:
            return None
        fresh = sum(
            ln.og_margin_used * ln.og_leverage for ln in priced if ln.verdict != "skip_slippage"
        )
        return fresh / total * 100.0


def scale_factor(og: AccountState) -> float:
    if og.account_value <= 0:
        return 1.0
    ratio = og.total_margin_used / og.account_value
    return SCALE_FACTOR_CAP / ratio if ratio > SCALE_FACTOR_CAP else 1.0


def within_slippage(direction: Direction, entry: float, price: float, slippage_pct: float) -> bool:
    """Per direction: a long is still worth copying while price <= entry * (1 + s);
    a short while price >= entry * (1 - s). Being *better* than entry is always fine."""
    band = slippage_pct / 100.0
    if direction is Direction.LONG:
        return price <= entry * (1.0 + band)
    return price >= entry * (1.0 - band)


def mirror_line(
    pos: Position,
    *,
    og_account_value: float,
    budget_usd: float,
    remaining_usd: float,
    price: float | None,
    scale: float,
    slippage_pct: float,
    multiplier: float,
    max_leverage: int,
    min_notional_usd: float,
) -> MirrorLine:
    allocation = pos.margin_used / og_account_value if og_account_value > 0 else 0.0
    leverage = max(1, min(pos.leverage, max_leverage))
    margin = budget_usd * allocation * scale * multiplier
    notional = margin * leverage
    if notional < min_notional_usd:  # bump, as Senpi does: consumes more budget than proportional
        notional = min_notional_usd
        margin = notional / leverage

    verdict: Verdict = "open"
    if price is None or price <= 0:
        verdict = "skip_price"
    elif not within_slippage(pos.direction, pos.entry_price, price, slippage_pct):
        verdict = "skip_slippage"
    elif margin > remaining_usd + 1e-9:
        verdict = "skip_budget"
    return MirrorLine(
        asset=pos.asset,
        direction=pos.direction,
        og_entry_price=pos.entry_price,
        og_leverage=pos.leverage,
        og_margin_used=pos.margin_used,
        allocation=allocation,
        price=price,
        leverage=leverage,
        margin_usd=margin,
        notional_usd=notional,
        verdict=verdict,
    )


def simulate_mirror(
    og: AccountState,
    budget_usd: float,
    prices: Mapping[str, float],
    *,
    slippage_pct: float = 3.0,
    multiplier: float = 1.0,
    max_leverage: int = 3,
    min_notional_usd: float = MIN_MIRROR_NOTIONAL_USD,
) -> MirrorPlan:
    """Blueprint section 3: largest position first, proportional margin, scale factor,
    min-notional bump, slippage filter, then budget check. Pure."""
    scale = scale_factor(og)
    ordered = sorted(
        og.positions,
        key=lambda p: p.size * prices.get(p.asset, p.entry_price),
        reverse=True,
    )
    remaining = budget_usd
    lines: list[MirrorLine] = []
    for pos in ordered:
        line = mirror_line(
            pos,
            og_account_value=og.account_value,
            budget_usd=budget_usd,
            remaining_usd=remaining,
            price=prices.get(pos.asset),
            scale=scale,
            slippage_pct=slippage_pct,
            multiplier=multiplier,
            max_leverage=max_leverage,
            min_notional_usd=min_notional_usd,
        )
        if line.verdict == "open":
            remaining -= line.margin_usd
        lines.append(line)
    return MirrorPlan(tuple(lines), budget_usd, scale, og.account_value)


# ---- live source -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookChange:
    kind: Literal["new", "closed", "flipped"]
    position: Position  # the trader's position after the change (before it, for "closed")


def diff_books(
    before: Mapping[str, Position], after: Mapping[str, Position]
) -> tuple[BookChange, ...]:
    """Blueprint 5.1 without the resize cases: new / closed / direction flipped."""
    changes: list[BookChange] = []
    for asset, pos in after.items():
        prev = before.get(asset)
        if prev is None:
            changes.append(BookChange("new", pos))
        elif prev.direction is not pos.direction:
            changes.append(BookChange("flipped", pos))
    for asset, pos in before.items():
        if asset not in after:
            changes.append(BookChange("closed", pos))
    return tuple(changes)


@dataclass(frozen=True, slots=True)
class CopyConfig:
    target: str  # the mirrored address
    budget_usd: float | None = None  # None → our whole account value
    multiplier: float = 1.0
    slippage_pct: float = 3.0
    max_leverage: int = 3
    min_notional_usd: float = MIN_MIRROR_NOTIONAL_USD
    poll_ms: int = DEFAULT_POLL_MS
    mirror_existing: bool = True  # copy the book as found at start-up (Senpi INITIALIZE)

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier must be > 0")
        if not 0 <= self.slippage_pct <= 20:
            raise ValueError("slippage_pct must be within 0-20")
        if self.poll_ms < 1000:
            raise ValueError("poll_ms must be >= 1000")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd must be > 0")


@dataclass
class CopySource:
    """``SignalSource`` + ``ExitSource``: polls the trader's book, emits an entry signal
    for every mirrorable new position and a close request when they exit. A flip closes
    now and re-enters at the next tick, once the venue shows us flat."""

    feed: TraderFeed
    cfg: CopyConfig
    our_account: Callable[[], AccountState]
    scanner: str = ""
    _book: dict[str, Position] | None = field(default=None, init=False, repr=False)
    _polled_at_ms: int = field(default=-1, init=False, repr=False)
    _closes: list[str] = field(default_factory=list, init=False, repr=False)
    _pending: list[Signal] = field(default_factory=list, init=False, repr=False)
    last_plan: MirrorPlan | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.scanner:
            self.scanner = f"copy:{self.cfg.target[:10]}"

    @property
    def book(self) -> Mapping[str, Position]:
        return dict(self._book or {})

    # ExitSource
    def close_requests(self, now_ms: int) -> Sequence[str]:
        out, self._closes = self._closes, []
        return out

    # SignalSource
    def signals(self, now_ms: int) -> Sequence[Signal]:
        out: list[Signal] = []
        if self._pending:  # flips queued last tick: the venue has had a tick to show us flat
            out, self._pending = self._pending, []
        if self._polled_at_ms >= 0 and now_ms - self._polled_at_ms < self.cfg.poll_ms:
            return out
        self._polled_at_ms = now_ms

        og = self.feed.account_state(self.cfg.target)
        after = {p.asset: p for p in og.positions}
        if self._book is None:
            self._book = after
            if self.cfg.mirror_existing:
                out.extend(self._entries(og, list(after.values()), now_ms))
            return out

        changes = diff_books(self._book, after)
        self._book = after
        new = [c.position for c in changes if c.kind == "new"]
        flipped = [c.position for c in changes if c.kind == "flipped"]
        self._closes.extend(c.position.asset for c in changes if c.kind in ("closed", "flipped"))
        out.extend(self._entries(og, new, now_ms))
        self._pending.extend(self._entries(og, flipped, now_ms))
        return out

    def _entries(
        self, og: AccountState, positions: Sequence[Position], now_ms: int
    ) -> list[Signal]:
        if not positions:
            return []
        ours = self.our_account()
        if ours.withdrawable <= 0:
            return []
        budget = self.cfg.budget_usd if self.cfg.budget_usd is not None else ours.account_value
        budget = min(budget, ours.account_value)
        # Same equity and margin usage as the full book, so the scale factor is the trader's.
        subset = AccountState(
            og.account_value, og.withdrawable, og.total_margin_used, tuple(positions)
        )
        plan = simulate_mirror(
            subset,
            budget,
            self.feed.all_mids(),
            slippage_pct=self.cfg.slippage_pct,
            multiplier=self.cfg.multiplier,
            max_leverage=self.cfg.max_leverage,
            min_notional_usd=self.cfg.min_notional_usd,
        )
        self.last_plan = plan
        signals: list[Signal] = []
        for line in plan.lines:
            # Budget is re-checked by the engine against real free margin (``no_margin``),
            # so only the price-based verdicts are final here.
            if line.verdict in ("skip_price", "skip_slippage"):
                continue
            signals.append(
                Signal(
                    asset=line.asset,
                    direction=line.direction,
                    scanner=self.scanner,
                    produced_at_ms=now_ms,
                    valid_until_ms=now_ms + self.cfg.poll_ms,
                    signal_id=str(uuid.uuid4()),
                    margin_pct=line.margin_usd / ours.withdrawable * 100.0,
                    leverage=float(line.og_leverage),
                    data={
                        "og_entry_price": line.og_entry_price,
                        "og_leverage": line.og_leverage,
                        "allocation_pct": line.allocation * 100.0,
                        "moved_from_entry_pct": line.moved_from_entry_pct,
                    },
                )
            )
        return signals
