"""Assemble a strategy package (a directory with ``runtime.yaml`` + ``scanners/``) into the
pieces the engine consumes: an ``EngineConfig`` and one ``SignalSource`` fanning out to every
external scanner in the recipe.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from hl_agent.engine.loop import EngineConfig
from hl_agent.engine.signals import Signal
from hl_agent.strategy.loader import load_scan
from hl_agent.strategy.mcp_shim import MarketSource, SenpiMcp
from hl_agent.strategy.runner import ScannerRunner, TickReport
from hl_agent.strategy.spec import RuntimeSpec, load_runtime_spec
from hl_agent.strategy.state import make_state


class FanOutSource:
    """Merges several runners into the single ``SignalSource`` the engine expects."""

    def __init__(self, runners: Sequence[ScannerRunner]) -> None:
        self.runners = list(runners)

    def signals(self, now_ms: int) -> Sequence[Signal]:
        out: list[Signal] = []
        for r in self.runners:
            out.extend(r.signals(now_ms))
        return out


@dataclass(frozen=True, slots=True)
class LoadedPackage:
    spec: RuntimeSpec
    engine_config: EngineConfig
    source: FanOutSource

    def validate(self, now_ms: int) -> dict[str, TickReport]:
        """One dry-run tick per scanner (``senpi validate`` semantics)."""
        return {r.spec.name: r.tick(now_ms, dry_run=True) for r in self.source.runners}


def load_package(
    path: Path | str,
    market: MarketSource,
    *,
    state_dir: Path | None = None,
    freeze_time: bool = False,
    enforce_timeout: bool = False,
    max_leverage: int = 3,
    env: dict[str, str] | None = None,
    force_leverage: int | None = None,
) -> LoadedPackage:
    spec = load_runtime_spec(path, env)
    wallet = spec.strategy.wallet
    runners: list[ScannerRunner] = []
    for sc in spec.external_scanners:
        state_path = state_dir / f"{spec.name}.{sc.name}.json" if state_dir else None
        runners.append(
            ScannerRunner(
                spec=sc,
                scan=load_scan(spec.scanner_dir(sc), sc.entrypoint, alias=spec.name),
                mcp=SenpiMcp(market, wallet),
                state=make_state(sc.state_history_max_count, state_path),
                wallet=wallet,
                freeze_time=freeze_time,
                enforce_timeout=enforce_timeout,
            )
        )
    return LoadedPackage(
        spec=spec,
        engine_config=spec.engine_config(max_leverage=max_leverage, force_leverage=force_leverage),
        source=FanOutSource(runners),
    )
