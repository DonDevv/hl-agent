"""COMPASS scoring — pure functions over candle dicts. No I/O, no MCP."""

from __future__ import annotations

from typing import Any


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def ema(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def evaluate(
    candles: list[dict[str, Any]], fast: int, slow: int, breakout: int
) -> dict[str, Any] | None:
    """Return ``{direction, score, reasons, barTime}`` for the last bar, or ``None``."""
    need = max(slow * 2, breakout + 2)
    if len(candles) < need:
        return None
    closes = [_f(c.get("c")) for c in candles]
    highs = [_f(c.get("h")) for c in candles]
    lows = [_f(c.get("l")) for c in candles]
    f, s = ema(closes, fast), ema(closes, slow)
    last = len(candles) - 1
    close = closes[last]
    stack_up = f[last] > s[last] and s[last] > s[last - 1]
    stack_down = f[last] < s[last] and s[last] < s[last - 1]
    prior_high = max(highs[last - breakout : last])
    prior_low = min(lows[last - breakout : last])
    bar_time = _f(candles[last].get("t"))

    if stack_up and close > prior_high and close > f[last]:
        stretch = (close - s[last]) / s[last] * 100.0
        return {
            "direction": "LONG",
            "score": round(min(10.0, 5.0 + stretch), 3),
            "reasons": ["ema_stack_up", f"breakout_{breakout}"],
            "barTime": bar_time,
        }
    if stack_down and close < prior_low and close < f[last]:
        stretch = (s[last] - close) / s[last] * 100.0
        return {
            "direction": "SHORT",
            "score": round(min(10.0, 5.0 + stretch), 3),
            "reasons": ["ema_stack_down", f"breakdown_{breakout}"],
            "barTime": bar_time,
        }
    return None
