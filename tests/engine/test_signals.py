from __future__ import annotations

import pytest

from hl_agent.data.models import Direction
from hl_agent.engine.signals import InvalidSignalError, parse_signal, validate_data

SCHEMA = {
    "score": {"type": "number"},
    "note": {"type": "string", "required": False},
}


def parse(raw: dict, **kw):  # type: ignore[no-untyped-def]
    return parse_signal(raw, scanner="t", now_ms=1_000, default_validity_s=300, **kw)


def test_minimal_signal_gets_id_and_validity() -> None:
    s = parse({"asset": "BTC", "direction": "long"})
    assert s.direction is Direction.LONG
    assert s.signal_id and s.valid_until_ms == 1_000 + 300_000
    assert s.margin_pct is None and s.leverage is None
    assert s.is_valid(301_000) and not s.is_valid(301_001)


def test_sizing_keys_are_parsed_and_must_be_positive() -> None:
    s = parse({"asset": "ETH", "direction": "SHORT", "marginPct": "25", "leverage": 3})
    assert s.margin_pct == 25.0 and s.leverage == 3.0
    with pytest.raises(InvalidSignalError):
        parse({"asset": "ETH", "direction": "SHORT", "marginPct": 0})
    with pytest.raises(InvalidSignalError):
        parse({"asset": "ETH", "direction": "SHORT", "leverage": "abc"})


@pytest.mark.parametrize(
    "raw", [{}, {"asset": "", "direction": "LONG"}, {"asset": "X", "direction": "UP"}]
)
def test_required_fields(raw: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(InvalidSignalError):
        parse(raw)


def test_data_schema_rules() -> None:
    validate_data({"score": 1.5}, SCHEMA)
    validate_data({"score": 1, "note": "x"}, SCHEMA)
    with pytest.raises(InvalidSignalError):
        validate_data({}, SCHEMA)  # required missing
    with pytest.raises(InvalidSignalError):
        validate_data({"score": 1, "extra": 1}, SCHEMA)  # undeclared key
    with pytest.raises(InvalidSignalError):
        validate_data({"score": "1"}, SCHEMA)  # wrong type
    with pytest.raises(InvalidSignalError):
        validate_data({"score": True}, SCHEMA)  # bool is not a number


def test_signal_id_is_preserved_when_given() -> None:
    s = parse(
        {"asset": "BTC", "direction": "LONG", "signal_id": "abc", "data": {"score": 2}},
        data_schema=SCHEMA,
    )
    assert s.signal_id == "abc" and s.score == 2.0
