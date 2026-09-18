"""Risk gates that sit between a signal and an order — pure functions over a small state.

Semantics follow Senpi's ``risk.guard_rails`` (``runtime-concepts.md``):

* ``daily_loss_limit_pct`` — realised loss today vs equity at day start (UTC) halts entries.
* ``max_entries_per_day`` — entries counted at submission; optional bypass while today's
  realised P&L is positive.
* ``max_consecutive_losses`` + ``cooldown_seconds`` — a losing streak pauses entries.
* ``drawdown_halt_pct`` — equity vs its peak (optionally reset each UTC day) halts entries.
* ``per_asset_cooldown_seconds`` — no re-entry on an asset just closed.
* ``max_spread_pct`` / ``min_depth_multiple`` — hl-agent extension: the L2 book must be
  tight and deep enough for the stop-market exit to fill near its trigger.

Every gate answers with a reason code, so rejections are auditable in the event log.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from hl_agent.data.models import Direction
from hl_agent.engine.config import GuardRails

_DAY_MS = 86_400_000


class GateReason(StrEnum):
    """Why a signal did not become an order. Values match Senpi's reason codes."""

    SUBMITTED = "submitted"
    NO_SLOTS = "no_slots"
    ASSET_HELD = "asset_held"
    ASSET_BANNED = "asset_banned"
    SIGNAL_NOT_READY = "signal_not_ready"
    SIGNAL_EXPIRED = "signal_expired"
    DUPLICATE = "duplicate"
    NO_MARGIN = "no_margin"
    RISK_GATE_LEVERAGE = "risk_gate_leverage"
    RISK_GATE_NOTIONAL = "risk_gate_notional"
    RISK_GATE_DAILY_LOSS = "risk_gate_daily_loss"
    RISK_GATE_MAX_ENTRIES = "risk_gate_max_entries"
    RISK_GATE_COOLDOWN = "risk_gate_cooldown"
    RISK_GATE_MAX_DRAWDOWN = "risk_gate_max_drawdown"
    RISK_GATE_ASSET_COOLDOWN = "risk_gate_asset_cooldown"
    RISK_GATE_LIQUIDITY = "risk_gate_liquidity"


Book = Mapping[str, Sequence[tuple[float, float]]]
"""``{"bids": [(px, sz), ...], "asks": [...]}`` best-first, as the venue returns it."""


@dataclass(frozen=True, slots=True)
class Liquidity:
    """What the gate measured, so a rejection event can say why."""

    spread_pct: float
    exit_depth_usd: float  # notional resting on the side a stop would hit, within the band
    band_pct: float

    def as_payload(self) -> dict[str, float]:
        return {
            "spread_pct": round(self.spread_pct, 4),
            "exit_depth_usd": round(self.exit_depth_usd, 2),
            "depth_band_pct": self.band_pct,
        }


def measure_liquidity(book: Book, direction: Direction, band_pct: float) -> Liquidity | None:
    """``None`` when one side of the book is empty (treated as illiquid by the gate)."""
    bids, asks = book.get("bids", ()), book.get("asks", ())
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    exit_side = bids if direction is Direction.LONG else asks  # a long's stop sells into bids
    depth = sum(px * sz for px, sz in exit_side if abs(px - mid) / mid * 100.0 <= band_pct)
    return Liquidity((best_ask - best_bid) / mid * 100.0, depth, band_pct)


def check_liquidity(
    cfg: GuardRails, book: Book | None, direction: Direction, notional_usd: float
) -> tuple[GateReason | None, Liquidity | None]:
    """Liquidity gate: disabled when both thresholds are 0 or the venue has no book."""
    if not cfg.liquidity_enabled or book is None:
        return None, None
    liq = measure_liquidity(book, direction, cfg.depth_band_pct)
    if liq is None:
        return GateReason.RISK_GATE_LIQUIDITY, None
    if cfg.max_spread_pct and liq.spread_pct > cfg.max_spread_pct:
        return GateReason.RISK_GATE_LIQUIDITY, liq
    if cfg.min_depth_multiple and liq.exit_depth_usd < cfg.min_depth_multiple * notional_usd:
        return GateReason.RISK_GATE_LIQUIDITY, liq
    return None, liq


def day_key(now_ms: int) -> int:
    return now_ms // _DAY_MS


@dataclass(frozen=True, slots=True)
class GuardRailState:
    day: int
    day_start_equity: float
    peak_equity: float
    realized_today: float = 0.0
    entries_today: int = 0
    consecutive_losses: int = 0
    cooldown_until_ms: int = 0
    last_close_ms: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def start(cls, now_ms: int, equity: float) -> GuardRailState:
        return cls(day=day_key(now_ms), day_start_equity=equity, peak_equity=equity)

    # ---- bookkeeping ---------------------------------------------------------

    def observe(self, cfg: GuardRails, now_ms: int, equity: float) -> GuardRailState:
        """Call once per tick before checking gates: day rollover and peak tracking."""
        state = self
        if day_key(now_ms) != state.day:
            state = replace(
                state,
                day=day_key(now_ms),
                day_start_equity=equity,
                realized_today=0.0,
                entries_today=0,
                peak_equity=equity if cfg.drawdown_reset_on_day_rollover else state.peak_equity,
            )
        return replace(state, peak_equity=max(state.peak_equity, equity))

    def record_entry(self) -> GuardRailState:
        return replace(self, entries_today=self.entries_today + 1)

    def record_close(self, cfg: GuardRails, asset: str, pnl: float, now_ms: int) -> GuardRailState:
        losses = self.consecutive_losses + 1 if pnl < 0 else 0
        cooldown = self.cooldown_until_ms
        if cfg.max_consecutive_losses and losses >= cfg.max_consecutive_losses:
            cooldown = now_ms + cfg.cooldown_seconds * 1000
            losses = 0
        closes = dict(self.last_close_ms)
        closes[asset] = now_ms
        return replace(
            self,
            realized_today=self.realized_today + pnl,
            consecutive_losses=losses,
            cooldown_until_ms=cooldown,
            last_close_ms=MappingProxyType(closes),
        )

    # ---- gates -----------------------------------------------------------------

    def check_account(self, cfg: GuardRails, now_ms: int, equity: float) -> GateReason | None:
        """Gates that apply to every entry regardless of asset."""
        if now_ms < self.cooldown_until_ms:
            return GateReason.RISK_GATE_COOLDOWN
        if cfg.daily_loss_limit_pct and self.day_start_equity > 0:
            loss_pct = -self.realized_today / self.day_start_equity * 100.0
            if loss_pct >= cfg.daily_loss_limit_pct:
                return GateReason.RISK_GATE_DAILY_LOSS
        if cfg.drawdown_halt_pct and self.peak_equity > 0:
            dd_pct = (self.peak_equity - equity) / self.peak_equity * 100.0
            if dd_pct >= cfg.drawdown_halt_pct:
                return GateReason.RISK_GATE_MAX_DRAWDOWN
        if (
            cfg.max_entries_per_day
            and self.entries_today >= cfg.max_entries_per_day
            and not (cfg.bypass_max_entries_per_day_on_profit and self.realized_today > 0)
        ):
            return GateReason.RISK_GATE_MAX_ENTRIES
        return None

    def check_asset(self, cfg: GuardRails, now_ms: int, asset: str) -> GateReason | None:
        last = self.last_close_ms.get(asset)
        if (
            cfg.per_asset_cooldown_seconds
            and last is not None
            and now_ms - last < cfg.per_asset_cooldown_seconds * 1000
        ):
            return GateReason.RISK_GATE_ASSET_COOLDOWN
        return None
