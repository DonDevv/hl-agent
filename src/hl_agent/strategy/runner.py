"""Drive one external scanner on its cadence and turn its dicts into engine ``Signal``s.

Contract points reproduced from Senpi's scaffold (``scan-contract.md`` / ``runtime-concepts.md``):

* ``scan(inputs, ctx)`` is called every ``interval_seconds`` with a frozen ``ctx``;
* ``ctx.state`` advances only on a clean tick — an exception, a timeout or a persist failure
  rolls it back;
* a malformed signal is a loud per-signal reject (stderr), never a silent resize;
* a tick that returned without reading anything is reported as **unproven** by ``validate``.

For backtests ``freeze_time=True`` makes ``time.time()`` inside the scanner return the simulated
clock, which is what every catalog scanner uses for its cadence math.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from hl_agent.engine.signals import InvalidSignalError, Signal, parse_signal
from hl_agent.strategy.loader import ScanFn
from hl_agent.strategy.mcp_shim import SenpiMcp
from hl_agent.strategy.spec import ScannerSpec
from hl_agent.strategy.state import StateStore


class ScanTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class ScanContext:
    """Exactly Senpi's ``ctx`` surface; frozen so a scanner cannot grow it."""

    senpi_mcp: SenpiMcp
    state: StateStore | None
    wallet: str
    scanner_name: str
    interval_seconds: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class TickReport:
    ok: bool
    proven: bool  # at least one MCP read happened
    signals: tuple[Signal, ...] = ()
    rejected: tuple[str, ...] = ()
    error: str = ""
    elapsed_s: float = 0.0


@dataclass
class ScannerRunner:
    spec: ScannerSpec
    scan: ScanFn
    mcp: SenpiMcp
    state: StateStore | None
    wallet: str
    freeze_time: bool = False
    enforce_timeout: bool = False
    _next_due_ms: int | None = field(default=None, init=False)
    last_report: TickReport | None = field(default=None, init=False)

    # ---- SignalSource --------------------------------------------------------------

    def signals(self, now_ms: int) -> Sequence[Signal]:
        if self._next_due_ms is not None and now_ms < self._next_due_ms:
            return ()
        self._next_due_ms = now_ms + self.spec.interval_seconds * 1000
        self.last_report = self.tick(now_ms, dry_run=False)
        return self.last_report.signals

    # ---- one tick ------------------------------------------------------------------

    def tick(self, now_ms: int, *, dry_run: bool) -> TickReport:
        ctx = ScanContext(
            senpi_mcp=self.mcp,
            state=self.state,
            wallet=self.wallet,
            scanner_name=self.spec.name,
            interval_seconds=self.spec.interval_seconds,
            dry_run=dry_run,
        )
        self.mcp.calls = 0
        if self.state is not None:
            self.state.begin()
        started = time.perf_counter()
        try:
            raw = self._call(ctx, now_ms)
            signals, rejected = self._parse(raw, now_ms)
            if self.state is not None:
                # a validation tick proves the read path; it never advances state
                self.state.rollback() if dry_run else self.state.commit()
        except Exception as exc:
            if self.state is not None:
                self.state.rollback()
            print(f"[{self.spec.name}] tick failed: {exc!r}", file=sys.stderr)
            return TickReport(
                ok=False,
                proven=self.mcp.calls > 0,
                error=repr(exc),
                elapsed_s=time.perf_counter() - started,
            )
        return TickReport(
            ok=True,
            proven=self.mcp.calls > 0,
            signals=tuple(signals),
            rejected=tuple(rejected),
            elapsed_s=time.perf_counter() - started,
        )

    def _call(self, ctx: ScanContext, now_ms: int) -> Any:
        inputs = dict(self.spec.inputs)
        if not self.enforce_timeout:
            return self._invoke(inputs, ctx, now_ms)

        result: list[Any] = []
        error: list[BaseException] = []

        def target() -> None:
            try:
                result.append(self._invoke(inputs, ctx, now_ms))
            except BaseException as exc:
                error.append(exc)

        worker = threading.Thread(target=target, name=f"scan-{self.spec.name}", daemon=True)
        worker.start()
        worker.join(self.spec.effective_timeout)
        if worker.is_alive():
            raise ScanTimeoutError(f"scan exceeded {self.spec.effective_timeout}s")
        if error:
            raise error[0]
        return result[0]

    def _invoke(self, inputs: dict[str, Any], ctx: ScanContext, now_ms: int) -> Any:
        if not self.freeze_time:
            return self.scan(inputs, ctx)
        with mock.patch("time.time", lambda: now_ms / 1000.0):
            return self.scan(inputs, ctx)

    def _parse(self, raw: Any, now_ms: int) -> tuple[list[Signal], list[str]]:
        if raw is None:
            return [], []
        if not isinstance(raw, list):
            raise TypeError(f"scan() must return a list, got {type(raw).__name__}")
        validity = self.spec.default_signal_validity_seconds or self.spec.interval_seconds
        signals: list[Signal] = []
        rejected: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                rejected.append(f"not a dict: {item!r}"[:200])
                continue
            try:
                signals.append(
                    parse_signal(
                        item,
                        scanner=self.spec.name,
                        now_ms=now_ms,
                        default_validity_s=validity,
                        data_schema=self.spec.signal_data_schema,
                    )
                )
            except InvalidSignalError as exc:
                rejected.append(str(exc))
                print(f"[{self.spec.name}] signal rejected: {exc}", file=sys.stderr)
        return signals, rejected
