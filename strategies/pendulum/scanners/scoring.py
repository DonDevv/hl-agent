"""PENDULUM scoring — pure functions over candle dicts. No I/O, no MCP."""

from __future__ import annotations

from typing import Any


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def rsi(closes: list[float], period: int) -> list[float | None]:
    """Wilder RSI; ``None`` until ``period`` deltas exist."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def evaluate(
    candles: list[dict[str, Any]], period: int, oversold: float, overbought: float
) -> dict[str, Any] | None:
    """Fade an extreme once the previous bar was stretched and the last bar turned back."""
    if len(candles) < period * 3:
        return None
    closes = [_f(c.get("c")) for c in candles]
    values = rsi(closes, period)
    last = len(candles) - 1
    prev_rsi, cur_rsi = values[last - 1], values[last]
    if prev_rsi is None or cur_rsi is None:
        return None
    bar_time = _f(candles[last].get("t"))
    turned_up = closes[last] > closes[last - 1]
    turned_down = closes[last] < closes[last - 1]

    if prev_rsi <= oversold and turned_up:
        return {
            "direction": "LONG",
            "rsi": round(prev_rsi, 2),
            "score": round(min(10.0, 5.0 + (oversold - prev_rsi) / 4.0), 3),
            "reasons": ["rsi_oversold", "bar_turned_up"],
            "barTime": bar_time,
        }
    if prev_rsi >= overbought and turned_down:
        return {
            "direction": "SHORT",
            "rsi": round(prev_rsi, 2),
            "score": round(min(10.0, 5.0 + (prev_rsi - overbought) / 4.0), 3),
            "reasons": ["rsi_overbought", "bar_turned_down"],
            "barTime": bar_time,
        }
    return None
