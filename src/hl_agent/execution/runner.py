"""The live loop: wall clock -> refresh venue -> engine step -> sink events.

Safety controls live here, not in the engine:

* a ``STOP`` file next to the run directory flattens everything and exits (kill switch);
* ``Network.MAINNET`` is refused unless ``accept_real_money`` is set explicitly by the caller
  (the CLI maps ``--i-accept-real-money`` onto it);
* an exception in one tick is reported and the loop continues; ``max_consecutive_errors``
  in a row stops it with positions left as they are (their exchange stops still protect them);
* network errors (DNS, connect, timeout) never count towards that streak: the venue is
  simply unreachable, so the loop backs off (up to ``max_backoff_s``) and keeps retrying.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.loop import Engine, Event
from hl_agent.execution.live import LiveMarketSource, Network

STOP_FILE = "STOP"


class RefusedError(RuntimeError):
    pass


def _is_network_error(exc: BaseException) -> bool:
    """DNS / connect / timeout anywhere in the cause chain: the venue, not the agent."""
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, httpx.TransportError | OSError):
            return True
        e = e.__cause__ or e.__context__
    return False


def check_network(network: Network, *, accept_real_money: bool) -> None:
    if network is Network.MAINNET and not accept_real_money:
        raise RefusedError("mainnet requires an explicit --i-accept-real-money")


@dataclass(frozen=True, slots=True)
class LoopConfig:
    interval_s: float = 60.0
    run_dir: Path = Path("runs")
    max_consecutive_errors: int = 10
    max_backoff_s: float = 300.0


DEFAULT_LOOP = LoopConfig()


class LiveRunner:
    def __init__(
        self,
        engine: Engine,
        market: LiveMarketSource,
        cfg: LoopConfig = DEFAULT_LOOP,
        *,
        sink: Callable[[Event], None] = lambda _e: None,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._market = market
        self._cfg = cfg
        self._sink = sink
        self._log = log
        self._clock = clock
        self._sleep = sleep
        self.ticks = 0
        self.errors = 0

    @property
    def stop_requested(self) -> bool:
        return (self._cfg.run_dir / STOP_FILE).exists()

    def tick(self) -> list[Event]:
        now_ms = int(self._clock() * 1000)
        self._market.refresh(now_ms)
        events = self._engine.step(now_ms)
        for e in events:
            self._sink(e)
        self.ticks += 1
        return events

    def flatten(self) -> list[Event]:
        now_ms = int(self._clock() * 1000)
        self._market.refresh(now_ms)
        events = self._engine.close_all(now_ms, CloseReason.MANUAL_CLOSE)
        for e in events:
            self._sink(e)
        return events

    def run(self, *, max_ticks: int | None = None) -> str:
        """Blocks until the kill switch, ``max_ticks`` or too many errors. Returns why."""
        streak = outages = 0
        while max_ticks is None or self.ticks < max_ticks:
            if self.stop_requested:
                self._log("STOP file found: flattening and exiting")
                self.flatten()
                return "stop_file"
            started = self._clock()
            try:
                self.tick()
                streak = outages = 0
            except Exception as exc:
                self.errors += 1
                if _is_network_error(exc):
                    outages += 1
                    wait = min(self._cfg.interval_s * 2**outages, self._cfg.max_backoff_s)
                    self._log(f"venue unreachable ({exc!r}); retrying in {wait:.0f}s")
                    self._sleep(wait)
                    continue
                streak += 1
                self._log(f"tick failed ({streak}/{self._cfg.max_consecutive_errors}): {exc!r}")
                self._log(traceback.format_exc())
                if streak >= self._cfg.max_consecutive_errors:
                    return "too_many_errors"
            self._sleep(max(0.0, self._cfg.interval_s - (self._clock() - started)))
        return "max_ticks"
