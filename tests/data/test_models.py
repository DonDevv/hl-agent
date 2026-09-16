from __future__ import annotations

from hl_agent.data.models import AccountState, Candle, Direction, Instrument
from tests.conftest import load_fixture


def test_candle_from_api_converts_strings_to_floats() -> None:
    raw = load_fixture("candles_btc_1h.json")[0]
    c = Candle.from_api(raw)
    assert c.asset == "BTC"
    assert c.interval == "1h"
    assert isinstance(c.open, float)
    assert c.high >= max(c.open, c.close) >= min(c.open, c.close) >= c.low
    assert c.close_ms - c.open_ms == 3_599_999


def test_candle_to_senpi_roundtrip_keeps_numeric_types() -> None:
    raw = load_fixture("candles_btc_1h.json")[0]
    senpi = Candle.from_api(raw).to_senpi()
    assert senpi["c"] == float(raw["c"])
    assert isinstance(senpi["c"], float)  # Senpi scanners call float() on it: no-op
    assert set(senpi) >= {"t", "T", "o", "h", "l", "c", "v", "n"}


def test_instrument_dex_prefix() -> None:
    raw = {"szDecimals": 2, "name": "NVDA", "maxLeverage": 10}
    assert Instrument.from_api(raw).name == "NVDA"
    assert Instrument.from_api(raw, dex="xyz").name == "xyz:NVDA"


def test_account_state_parses_positions_and_skips_flat_ones() -> None:
    state = AccountState.from_api(load_fixture("clearinghouse_with_positions.json"))
    assert state.account_value == 1000.5
    assert state.withdrawable == 700.5
    assert state.free_margin == 700.5
    assert [p.asset for p in state.positions] == ["BTC", "ETH"]  # SOL (szi=0) dropped

    btc = state.position("BTC")
    assert btc is not None
    assert btc.direction is Direction.LONG
    assert btc.size == 0.01
    assert btc.leverage == 5
    assert btc.liquidation_price == 61000.0
    assert round(btc.roe_pct, 2) == 6.66

    eth = state.position("ETH")
    assert eth is not None
    assert eth.direction is Direction.SHORT
    assert eth.size == 0.2
    assert eth.liquidation_price is None
    assert state.position("SOL") is None


def test_direction_helpers() -> None:
    assert Direction.parse("long") is Direction.LONG
    assert Direction.LONG.sign == 1
    assert Direction.SHORT.sign == -1
    assert Direction.LONG.opposite() is Direction.SHORT
