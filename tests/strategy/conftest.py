from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from hl_agent.data.history import CandleStore
from hl_agent.data.models import AccountState, Candle, Instrument
from hl_agent.strategy.sources import ReplaySource

REPO = Path(__file__).resolve().parents[2]
STRATEGIES = REPO / "strategies"
SENPI = Path(r"C:\Users\userr\Desktop\senpi-skills-main\senpi-skills-main\strategies")

H = 3_600_000
INSTRUMENTS = [
    Instrument("BTC", size_decimals=5, max_leverage=40),
    Instrument("ETH", size_decimals=4, max_leverage=25),
    Instrument("SOL", size_decimals=2, max_leverage=20),
]


def synth(
    asset: str, interval: str, n: int, start_ms: int, step_ms: int, base: float
) -> list[Candle]:
    """Deterministic wave with a trend: enough structure to trigger both native scanners."""
    out = []
    px = base
    for i in range(n):
        drift = 1.0 + 0.004 * math.sin(i / 9.0) + (0.0015 if i > n // 2 else -0.0005)
        nxt = px * drift
        hi, lo = max(px, nxt) * 1.003, min(px, nxt) * 0.997
        t = start_ms + i * step_ms
        out.append(Candle(asset, interval, t, t + step_ms - 1, px, hi, lo, nxt, 100.0, 10))
        px = nxt
    return out


@pytest.fixture
def store(tmp_path: Path) -> CandleStore:
    s = CandleStore(tmp_path / "cache")
    for inst, base in zip(INSTRUMENTS, (50_000.0, 3_000.0, 150.0), strict=True):
        s.append(inst.name, "1h", synth(inst.name, "1h", 600, 0, H, base))
        s.append(inst.name, "4h", synth(inst.name, "4h", 300, 0, 4 * H, base))
    return s


@pytest.fixture
def flat_account() -> Callable[[], AccountState]:
    return lambda: AccountState(100.0, 100.0, 0.0, ())


@pytest.fixture
def replay(store: CandleStore, flat_account: Callable[[], AccountState]) -> ReplaySource:
    src = ReplaySource(store, INSTRUMENTS, flat_account)
    src.now_ms = 599 * H
    return src
