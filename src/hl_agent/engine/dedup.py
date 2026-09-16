"""Signal de-duplication.

Senpi's runtime keeps a high-water mark per ``(scanner, asset)`` so a scanner that keeps
re-emitting the same setup only enters once, and a ``signal_id`` set so an exact replay
(e.g. after a restart) never double-fires. Both are tiny, in-memory and immutable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from hl_agent.data.models import Direction
from hl_agent.engine.signals import Signal


@dataclass(frozen=True, slots=True)
class Seen:
    direction: Direction
    produced_at_ms: int


@dataclass(frozen=True, slots=True)
class DedupState:
    seen_ids: frozenset[str] = frozenset()
    high_water: Mapping[tuple[str, str], Seen] = field(default_factory=lambda: MappingProxyType({}))

    def is_duplicate(self, signal: Signal, *, window_ms: int) -> bool:
        if signal.signal_id in self.seen_ids:
            return True
        last = self.high_water.get((signal.scanner, signal.asset))
        if last is None:
            return False
        same_setup = last.direction is signal.direction
        return same_setup and signal.produced_at_ms - last.produced_at_ms < window_ms

    def remember(self, signal: Signal) -> DedupState:
        hw = dict(self.high_water)
        hw[(signal.scanner, signal.asset)] = Seen(signal.direction, signal.produced_at_ms)
        return replace(
            self,
            seen_ids=self.seen_ids | {signal.signal_id},
            high_water=MappingProxyType(hw),
        )
