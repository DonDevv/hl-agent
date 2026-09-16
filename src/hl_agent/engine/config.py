"""Engine configuration — typed, validated, immutable.

Field names and semantics mirror Senpi's ``runtime.yaml`` (``strategy``, ``exit.dsl_preset``,
``risk.guard_rails``) so a Senpi package maps 1:1. Percent fields are **ROE percent of
margin**, not price percent — the DSL converts using leverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Tier:
    trigger_pct: float  # ROE% that arms this tier
    lock_hw_pct: float  # % of high-water ROE protected once armed

    def __post_init__(self) -> None:
        if self.trigger_pct <= 0 or self.trigger_pct > 100:
            raise ConfigError(f"tier trigger_pct must be in (0, 100], got {self.trigger_pct}")
        if self.lock_hw_pct <= 0 or self.lock_hw_pct > 100:
            # Senpi: a breakeven lock (0) exits flat and still pays fees — forbidden.
            raise ConfigError(f"tier lock_hw_pct must be in (0, 100], got {self.lock_hw_pct}")


@dataclass(frozen=True, slots=True)
class TimeCut:
    enabled: bool = False
    interval_minutes: float = 0.0
    min_value: float = 0.0  # weak_peak_cut only: ROE% that counts as "real profit"

    def __post_init__(self) -> None:
        if self.enabled and self.interval_minutes <= 0:
            raise ConfigError("an enabled time cut needs interval_minutes > 0")


@dataclass(frozen=True, slots=True)
class Phase1:
    max_loss_pct: float  # absolute floor: never lose more than this ROE%
    trailing_enabled: bool = False  # Senpi ships this OFF fleet-wide (ratchet-into-a-loss bug)
    retrace_threshold: float = 0.0  # ROE% below high-water, only when trailing_enabled
    consecutive_breaches_required: int = 1

    def __post_init__(self) -> None:
        if self.max_loss_pct <= 0:
            raise ConfigError("phase1.max_loss_pct must be > 0")
        if self.trailing_enabled and self.retrace_threshold <= 0:
            raise ConfigError("phase1.retrace_threshold must be > 0 when trailing is enabled")
        if self.consecutive_breaches_required < 1:
            raise ConfigError("phase1.consecutive_breaches_required must be >= 1")


@dataclass(frozen=True, slots=True)
class DslConfig:
    phase1: Phase1
    tiers: tuple[Tier, ...] = ()
    hard_timeout: TimeCut = field(default_factory=TimeCut)
    weak_peak_cut: TimeCut = field(default_factory=TimeCut)
    dead_weight_cut: TimeCut = field(default_factory=TimeCut)

    def __post_init__(self) -> None:
        triggers = [t.trigger_pct for t in self.tiers]
        if triggers != sorted(triggers) or len(set(triggers)) != len(triggers):
            raise ConfigError("phase2.tiers must be strictly ascending by trigger_pct")
        if self.weak_peak_cut.enabled and self.weak_peak_cut.min_value <= 0:
            raise ConfigError("weak_peak_cut.min_value must be > 0 when enabled")


@dataclass(frozen=True, slots=True)
class GuardRails:
    daily_loss_limit_pct: float = 0.0  # 0 = disabled
    max_entries_per_day: int = 0  # 0 = unlimited
    bypass_max_entries_per_day_on_profit: bool = False
    max_consecutive_losses: int = 0  # 0 = disabled
    cooldown_seconds: int = 0
    drawdown_halt_pct: float = 0.0  # 0 = disabled
    drawdown_reset_on_day_rollover: bool = False
    per_asset_cooldown_seconds: int = 0

    def __post_init__(self) -> None:
        for name in ("daily_loss_limit_pct", "drawdown_halt_pct"):
            v = getattr(self, name)
            if v < 0 or v > 100:
                raise ConfigError(f"{name} must be within 0-100, got {v}")
        if self.max_consecutive_losses and self.cooldown_seconds < 60:
            raise ConfigError("cooldown_seconds must be >= 60 when max_consecutive_losses is set")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    slots: int
    margin_pct: float  # fallback per-signal margin, percent of withdrawable
    default_leverage: int
    max_leverage: int = 3  # hard global cap; the account's, not the strategy's
    min_notional_usd: float = 10.0
    allow_pyramiding: bool = False

    def __post_init__(self) -> None:
        if self.slots < 1:
            raise ConfigError("slots must be >= 1")
        if not 0 < self.margin_pct <= 100:
            raise ConfigError("margin_pct must be in (0, 100]")
        if self.default_leverage < 1 or self.max_leverage < 1:
            raise ConfigError("leverage values must be >= 1")
