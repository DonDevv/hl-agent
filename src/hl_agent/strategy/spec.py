"""``runtime.yaml`` — parsed, env-substituted, validated, mapped to engine config.

The schema is Senpi's (``runtime-yaml.md``). Only the sections our runtime acts on are typed
strictly: ``strategy``, ``scanners``, ``exit.dsl_preset`` and ``risk.guard_rails``. ``actions``
and ``notifications`` are carried as-is so a package round-trips, but the runtime is
rule-mode only (no LLM decision step) and the DSL is the only exit engine.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hl_agent.engine.config import (
    ConfigError,
    DslConfig,
    GuardRails,
    Phase1,
    StrategyConfig,
    Tier,
    TimeCut,
)
from hl_agent.engine.loop import EngineConfig

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SpecError(ConfigError):
    pass


def substitute_env(text: str, env: dict[str, str] | None = None) -> str:
    """``${VAR}`` / ``${VAR:-default}``; an unset variable without default becomes ``""``."""
    source = os.environ if env is None else env

    def repl(m: re.Match[str]) -> str:
        return source.get(m.group(1), m.group(2) or "")

    return _ENV_RE.sub(repl, text)


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class StrategyBlock(_Model):
    wallet: str = ""
    slots: int = Field(ge=1)
    margin_pct: float | None = Field(default=None, gt=0, le=100)
    default_leverage: int = Field(ge=1)
    trading_risk: Literal["conservative", "moderate", "aggressive"] = "conservative"
    enabled: bool = True


class ScannerSpec(_Model):
    name: str
    type: Literal["position_tracker", "external_scanner"]
    interval_seconds: int = Field(ge=1)
    path: str = "./scanners"
    entrypoint: str = "scan.py"
    timeout_seconds: int | None = Field(default=None, ge=1)
    default_signal_validity_seconds: int | None = Field(default=None, ge=1)
    state_history_max_count: int = Field(default=0, ge=0)
    inputs: dict[str, Any] = Field(default_factory=dict)
    signal_data_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def is_external(self) -> bool:
        return self.type == "external_scanner"

    @property
    def effective_timeout(self) -> int:
        return self.timeout_seconds or self.interval_seconds


class TimeCutSpec(_Model):
    enabled: bool = False
    interval_in_minutes: float = 0.0
    min_value: float = 0.0


class Phase1Spec(_Model):
    enabled: bool = True
    max_loss_pct: float | None = None
    retrace_threshold: float = 0.0
    consecutive_breaches_required: int = 1


class TierSpec(_Model):
    trigger_pct: float
    lock_hw_pct: float


class Phase2Spec(_Model):
    enabled: bool = True
    tiers: list[TierSpec] = Field(default_factory=list)


class DslPresetSpec(_Model):
    max_loss_pct: float | None = None
    hard_timeout: TimeCutSpec = Field(default_factory=TimeCutSpec)
    weak_peak_cut: TimeCutSpec = Field(default_factory=TimeCutSpec)
    dead_weight_cut: TimeCutSpec = Field(default_factory=TimeCutSpec)
    phase1: Phase1Spec = Field(default_factory=Phase1Spec)
    phase2: Phase2Spec = Field(default_factory=Phase2Spec)


class ExitBlock(_Model):
    engine: Literal["dsl"] = "dsl"
    interval_seconds: int = 60
    dsl_preset: DslPresetSpec = Field(default_factory=DslPresetSpec)


class GuardRailsSpec(_Model):
    daily_loss_limit_pct: float = Field(default=0.0, ge=0)
    max_entries_per_day: int = Field(default=0, ge=0)
    bypass_max_entries_per_day_on_profit: bool = False
    max_consecutive_losses: int = Field(default=0, ge=0)
    cooldown_seconds: int = Field(default=0, ge=0)
    drawdown_halt_pct: float = Field(default=0.0, ge=0, le=100)
    drawdown_reset_on_day_rollover: bool = False
    per_asset_cooldown_seconds: int = Field(default=0, ge=0)

    @field_validator("cooldown_seconds")
    @classmethod
    def _cooldown_min(cls, v: int) -> int:
        if v and v < 60:
            raise ValueError("cooldown_seconds min 60")
        return v

    @field_validator("per_asset_cooldown_seconds")
    @classmethod
    def _asset_cooldown_min(cls, v: int) -> int:
        if v and v < 300:
            raise ValueError("per_asset_cooldown_seconds min 300")
        return v


class RiskBlock(_Model):
    data_retention_seconds: int = Field(default=86_400, ge=3600, le=604_800)
    guard_rails: GuardRailsSpec = Field(default_factory=GuardRailsSpec)


class RuntimeSpec(_Model):
    name: str
    version: str = ""
    group: str = ""
    description: str = ""
    strategy: StrategyBlock
    scanners: list[ScannerSpec] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    exit: ExitBlock = Field(default_factory=ExitBlock)
    risk: RiskBlock | None = None
    notifications: dict[str, Any] = Field(default_factory=dict)
    base_dir: Path = Path(".")

    # ---- invariants (runtime-yaml.md "Load-time invariants") ----------------------

    @field_validator("scanners")
    @classmethod
    def _unique_scanner_names(cls, v: list[ScannerSpec]) -> list[ScannerSpec]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("scanner names must be unique")
        for s in v:
            if s.is_external and s.default_signal_validity_seconds is None:
                raise ValueError(f"scanner {s.name}: default_signal_validity_seconds is required")
        return v

    @property
    def external_scanners(self) -> list[ScannerSpec]:
        return [s for s in self.scanners if s.is_external]

    def scanner_dir(self, scanner: ScannerSpec) -> Path:
        return (self.base_dir / scanner.path).resolve()

    # ---- mapping to the engine -------------------------------------------------------

    def dsl_config(self) -> DslConfig:
        p = self.exit.dsl_preset
        max_loss = p.max_loss_pct if p.max_loss_pct is not None else p.phase1.max_loss_pct
        if max_loss is None:
            if p.phase1.enabled:
                raise SpecError("exit.dsl_preset: max_loss_pct is required when phase1 is enabled")
            max_loss = 1.0  # documented fallback when phase 1 is disabled
        phase1 = Phase1(
            max_loss_pct=max_loss,
            # "phase1.enabled: false" in the catalog means the trailing retrace is off; the
            # absolute floor still protects the account.
            trailing_enabled=p.phase1.enabled and p.phase1.retrace_threshold > 0,
            retrace_threshold=p.phase1.retrace_threshold,
            consecutive_breaches_required=max(1, p.phase1.consecutive_breaches_required),
        )
        tiers = (
            tuple(Tier(t.trigger_pct, t.lock_hw_pct) for t in p.phase2.tiers)
            if p.phase2.enabled
            else ()
        )
        return DslConfig(
            phase1=phase1,
            tiers=tiers,
            hard_timeout=_time_cut(p.hard_timeout),
            weak_peak_cut=_time_cut(p.weak_peak_cut),
            dead_weight_cut=_time_cut(p.dead_weight_cut),
        )

    def guard_rails(self) -> GuardRails:
        if self.risk is None:
            return GuardRails()
        g = self.risk.guard_rails
        return GuardRails(
            daily_loss_limit_pct=g.daily_loss_limit_pct,
            max_entries_per_day=g.max_entries_per_day,
            bypass_max_entries_per_day_on_profit=g.bypass_max_entries_per_day_on_profit,
            max_consecutive_losses=g.max_consecutive_losses,
            cooldown_seconds=g.cooldown_seconds,
            drawdown_halt_pct=g.drawdown_halt_pct,
            drawdown_reset_on_day_rollover=g.drawdown_reset_on_day_rollover,
            per_asset_cooldown_seconds=g.per_asset_cooldown_seconds,
        )

    def engine_config(
        self, *, max_leverage: int = 3, min_notional_usd: float = 10.0
    ) -> EngineConfig:
        s = self.strategy
        strategy = StrategyConfig(
            slots=s.slots,
            # No margin_pct in the recipe → scanners must emit marginPct; a signal without it
            # would size at this floor value and get rejected on notional, which is loud.
            margin_pct=s.margin_pct if s.margin_pct is not None else 1.0,
            default_leverage=s.default_leverage,
            max_leverage=max_leverage,
            min_notional_usd=min_notional_usd,
        )
        return EngineConfig(strategy=strategy, dsl=self.dsl_config(), rails=self.guard_rails())


def _time_cut(spec: TimeCutSpec) -> TimeCut:
    return TimeCut(
        enabled=spec.enabled, interval_minutes=spec.interval_in_minutes, min_value=spec.min_value
    )


def load_runtime_spec(path: Path | str, env: dict[str, str] | None = None) -> RuntimeSpec:
    p = Path(path)
    if p.is_dir():
        p = p / "runtime.yaml"
    raw = yaml.safe_load(substitute_env(p.read_text(encoding="utf-8"), env)) or {}
    if not isinstance(raw, dict):
        raise SpecError(f"{p}: top level must be a mapping")
    raw["base_dir"] = p.parent
    try:
        return RuntimeSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecError(f"{p}: {exc}") from exc
