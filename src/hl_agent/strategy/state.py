"""``ctx.state`` — the bounded, transactional history store scanners see.

API is exactly Senpi's: ``last()``, ``recent(n)``, ``len()``, ``append(dict)``. The runner
wraps each tick in ``begin()`` … ``commit()`` / ``rollback()`` so state never advances on a
failed tick. Persistence is one JSON file per scanner (or in-memory for backtests).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, max_count: int, path: Path | None = None) -> None:
        self._max = max_count
        self._path = path
        self._records: list[dict[str, Any]] = []
        self._snapshot: list[dict[str, Any]] | None = None
        if path is not None and path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                self._records = [r for r in loaded if isinstance(r, dict)][-max_count:]

    # ---- scanner-facing API -------------------------------------------------------

    def last(self) -> dict[str, Any] | None:
        return self._records[-1] if self._records else None

    def recent(self, n: int) -> list[dict[str, Any]]:
        return self._records[-n:] if n > 0 else []

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._records)

    def append(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise TypeError("ctx.state.append expects a dict")
        self._records.append(record)
        del self._records[: -self._max]

    # ---- runner-facing transaction ------------------------------------------------

    def begin(self) -> None:
        self._snapshot = list(self._records)

    def rollback(self) -> None:
        if self._snapshot is not None:
            self._records = self._snapshot
        self._snapshot = None

    def commit(self) -> None:
        self._snapshot = None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._records, default=str), encoding="utf-8")
            tmp.replace(self._path)


def make_state(max_count: int, path: Path | None = None) -> StateStore | None:
    """Senpi: ``state_history_max_count`` of 0/unset disables history → ``ctx.state is None``."""
    return StateStore(max_count, path) if max_count > 0 else None
