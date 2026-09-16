"""Signal intake: the dict a scanner returns → a validated, immutable ``Signal``.

Mirrors Senpi's scaffold rules (``scan-contract.md``): ``asset`` and ``direction`` are
required; ``marginPct``/``leverage`` are the only top-level sizing keys and must be
positive when present; ``data`` is validated against the scanner's ``signal_data_schema``;
the scaffold mints ``signal_id`` and stamps ``produced_at`` / ``valid_until``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hl_agent.data.models import Direction

_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class InvalidSignalError(ValueError):
    """Loud reject, as in Senpi: a malformed signal never sizes a position differently."""


@dataclass(frozen=True, slots=True)
class Signal:
    asset: str
    direction: Direction
    scanner: str
    produced_at_ms: int
    valid_until_ms: int
    signal_id: str
    margin_pct: float | None = None  # percent of withdrawable; None → config fallback
    leverage: float | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    signal_type: str | None = None

    @property
    def score(self) -> float | None:
        v = self.data.get("score")
        return float(v) if isinstance(v, (int, float)) else None

    def is_valid(self, now_ms: int) -> bool:
        return now_ms <= self.valid_until_ms


def _positive(raw: Mapping[str, Any], key: str) -> float | None:
    if key not in raw or raw[key] is None:
        return None
    try:
        value = float(raw[key])
    except (TypeError, ValueError) as exc:
        raise InvalidSignalError(f"{key} is not a number: {raw[key]!r}") from exc
    if value <= 0:
        raise InvalidSignalError(f"{key} must be positive, got {value}")
    return value


def validate_data(data: Mapping[str, Any], schema: Mapping[str, Mapping[str, Any]]) -> None:
    """Senpi rules: unknown key → reject; missing required → reject; wrong type → reject."""
    for key in data:
        if key not in schema:
            raise InvalidSignalError(f"data.{key} not declared in signal_data_schema")
    for key, spec in schema.items():
        required = bool(spec.get("required", True))
        if key not in data:
            if required:
                raise InvalidSignalError(f"data.{key} is required")
            continue
        expected = _SCHEMA_TYPES.get(str(spec.get("type", "")))
        if expected is None:
            raise InvalidSignalError(f"signal_data_schema.{key}: unknown type {spec.get('type')!r}")
        value = data[key]
        # bool is an int in Python; keep "number" strict like a JSON schema would
        if expected == (int, float) and isinstance(value, bool):
            raise InvalidSignalError(f"data.{key} must be a number")
        if not isinstance(value, expected):
            raise InvalidSignalError(f"data.{key} must be {spec.get('type')}")


def parse_signal(
    raw: Mapping[str, Any],
    *,
    scanner: str,
    now_ms: int,
    default_validity_s: int,
    data_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> Signal:
    asset = str(raw.get("asset") or "").strip()
    if not asset:
        raise InvalidSignalError("asset is required")
    try:
        direction = Direction.parse(str(raw.get("direction", "")))
    except ValueError as exc:
        raise InvalidSignalError(
            f"direction must be LONG or SHORT, got {raw.get('direction')!r}"
        ) from exc

    data = raw.get("data") or {}
    if not isinstance(data, Mapping):
        raise InvalidSignalError("data must be an object")
    if data_schema is not None:
        validate_data(data, data_schema)

    validity = raw.get("valid_for_seconds")
    ttl = validity if isinstance(validity, int) and validity > 0 else default_validity_s

    return Signal(
        asset=asset,
        direction=direction,
        scanner=scanner,
        produced_at_ms=now_ms,
        valid_until_ms=now_ms + ttl * 1000,
        signal_id=str(raw.get("signal_id") or uuid.uuid4()),
        margin_pct=_positive(raw, "marginPct"),
        leverage=_positive(raw, "leverage"),
        data=dict(data),
        signal_type=raw.get("signal_type"),
    )
