"""Append-only JSONL event log: one ``Event`` per line, written as it happens.

The same file format serves live runs (tail it) and backtests (persist for later reports).
Reading is tolerant: a truncated last line (crash mid-write) is skipped, not fatal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hl_agent.engine.loop import Event


def to_dict(event: Event) -> dict[str, Any]:
    return {
        "time_ms": event.time_ms,
        "time": datetime.fromtimestamp(event.time_ms / 1000, UTC).isoformat(timespec="seconds"),
        "kind": event.kind,
        "asset": event.asset,
        "reason": event.reason,
        "payload": dict(event.payload),
    }


def from_dict(raw: dict[str, Any]) -> Event:
    return Event(
        int(raw["time_ms"]),
        str(raw["kind"]),
        str(raw["asset"]),
        str(raw["reason"]),
        dict(raw.get("payload", {})),
    )


class EventLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_dict(event), separators=(",", ":"), default=str) + "\n")

    def write_all(self, events: Iterable[Event]) -> int:
        n = 0
        with self.path.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(to_dict(e), separators=(",", ":"), default=str) + "\n")
                n += 1
        return n

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []
        out: list[Event] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(from_dict(json.loads(line)))
                except (ValueError, KeyError):
                    continue  # torn write at the tail; nothing after it is trustworthy anyway
        return out
