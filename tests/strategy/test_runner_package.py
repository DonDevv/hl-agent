from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hl_agent.data.models import AccountState
from hl_agent.engine.loop import Engine
from hl_agent.strategy.loader import ScannerLoadError, load_scan
from hl_agent.strategy.mcp_shim import SenpiMcp
from hl_agent.strategy.package import load_package
from hl_agent.strategy.runner import ScannerRunner
from hl_agent.strategy.sources import ReplaySource
from hl_agent.strategy.spec import ScannerSpec
from hl_agent.strategy.state import StateStore
from tests.strategy.conftest import SENPI, STRATEGIES, H

# ---- loader -----------------------------------------------------------------------


def _write_pkg(d: Path, body: str, scoring: str = "X = 1\n") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "scoring.py").write_text(scoring, encoding="utf-8")
    (d / "scan.py").write_text("import scoring\n" + body, encoding="utf-8")
    return d


def test_loader_isolates_sibling_scoring_modules(tmp_path: Path) -> None:
    a = _write_pkg(tmp_path / "a", "def scan(inputs, ctx):\n    return [scoring.X]\n", "X = 'a'\n")
    b = _write_pkg(tmp_path / "b", "def scan(inputs, ctx):\n    return [scoring.X]\n", "X = 'b'\n")
    sa, sb = load_scan(a), load_scan(b)
    assert sa({}, None) == ["a"] and sb({}, None) == ["b"]
    assert "scoring" not in sys.modules
    with pytest.raises(ScannerLoadError):
        load_scan(tmp_path / "missing")
    with pytest.raises(ScannerLoadError):
        load_scan(_write_pkg(tmp_path / "c", "nope = 1\n"))


# ---- runner -----------------------------------------------------------------------


def _spec(**kw: object) -> ScannerSpec:
    base: dict[str, object] = {
        "name": "t",
        "type": "external_scanner",
        "interval_seconds": 60,
        "default_signal_validity_seconds": 120,
        "state_history_max_count": 5,
        "signal_data_schema": {"score": {"type": "number"}},
    }
    base.update(kw)
    return ScannerSpec.model_validate(base)


def test_runner_parses_rejects_and_rolls_back_state(replay: ReplaySource) -> None:
    calls: list[int] = []

    def scan(inputs: dict, ctx: object) -> list[dict]:  # type: ignore[type-arg]
        ctx.senpi_mcp.call_tool("market_get_prices", {})  # type: ignore[attr-defined]
        ctx.state.append({"n": len(calls)})  # type: ignore[attr-defined]
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return [
            {"asset": "BTC", "direction": "long", "marginPct": 10, "data": {"score": 1}},
            {"asset": "ETH", "direction": "short", "data": {"score": "bad"}},
            "garbage",
        ]

    r = ScannerRunner(_spec(), scan, SenpiMcp(replay, "0x"), StateStore(5), "0x")
    out = r.signals(0)
    assert [s.asset for s in out] == ["BTC"] and out[0].valid_until_ms == 120_000
    assert r.last_report is not None and r.last_report.proven
    assert len(r.last_report.rejected) == 2
    assert r.state is not None and r.state.last() == {"n": 0}

    assert r.signals(30_000) == ()  # not due yet
    assert r.signals(60_000) == ()  # tick 2 raised → nothing, state rolled back
    assert r.last_report is not None and not r.last_report.ok and "boom" in r.last_report.error
    assert r.state.last() == {"n": 0}


def test_runner_freezes_time_and_reports_unproven(replay: ReplaySource) -> None:
    import time

    def scan(inputs: dict, ctx: object) -> list[dict]:  # type: ignore[type-arg]
        return [{"asset": "BTC", "direction": "LONG", "data": {"score": time.time()}}]

    r = ScannerRunner(_spec(), scan, SenpiMcp(replay, "0x"), None, "0x", freeze_time=True)
    rep = r.tick(1_700_000_000_000, dry_run=True)
    assert rep.ok and not rep.proven  # read nothing → UNPROVEN
    assert rep.signals[0].data["score"] == 1_700_000_000.0


def test_runner_timeout_rolls_back(replay: ReplaySource) -> None:
    import time

    def scan(inputs: dict, ctx: object) -> list[dict]:  # type: ignore[type-arg]
        ctx.state.append({"x": 1})  # type: ignore[attr-defined]
        time.sleep(0.5)
        return []

    r = ScannerRunner(
        _spec(interval_seconds=1, timeout_seconds=1),
        scan,
        SenpiMcp(replay, "0x"),
        StateStore(5),
        "0x",
        enforce_timeout=True,
    )
    r.spec = _spec(interval_seconds=1, timeout_seconds=1)
    # shrink the budget below the sleep via a spec with a 1s timeout and a 0.5s sleep → passes
    assert r.tick(0, dry_run=False).ok
    slow = ScannerRunner(
        _spec(interval_seconds=1, timeout_seconds=1),
        lambda i, c: (time.sleep(1.5), [])[1],  # type: ignore[misc]
        SenpiMcp(replay, "0x"),
        StateStore(5),
        "0x",
        enforce_timeout=True,
    )
    rep = slow.tick(0, dry_run=False)
    assert not rep.ok and "ScanTimeoutError" in rep.error


# ---- native packages end to end -------------------------------------------------------


@pytest.mark.parametrize("name", ["compass", "pendulum"])
def test_native_package_validates_and_emits_on_synthetic_data(
    name: str, replay: ReplaySource, tmp_path: Path
) -> None:
    pkg = load_package(
        STRATEGIES / name, replay, state_dir=tmp_path, freeze_time=True, env={"HL_WALLET": "0x"}
    )
    report = pkg.validate(replay.now_ms)
    (rep,) = report.values()
    assert rep.ok and rep.proven and rep.rejected == ()

    # sweep the clock over the synthetic history: the scanner must fire at least once
    seen = []
    for hour in range(200, 600):
        replay.now_ms = hour * H
        seen.extend(pkg.source.signals(replay.now_ms))
    assert seen, f"{name} never emitted on synthetic data"
    assert all(s.margin_pct and s.leverage == 2 and s.data["score"] >= 5 for s in seen)
    # dedup per bar: no two signals for the same asset and bar
    keys = [(s.asset, s.data["barTime"]) for s in seen]
    assert len(keys) == len(set(keys))


def test_package_drives_engine_end_to_end(replay: ReplaySource, tmp_path: Path) -> None:
    """runtime.yaml → scanner → engine.step with a minimal paper broker."""
    from hl_agent.engine.dsl import CloseReason
    from hl_agent.engine.ports import Fill
    from hl_agent.engine.sizing import OrderPlan

    class Paper:
        def __init__(self) -> None:
            self.cash = 100.0
            self.pos: dict[str, tuple[float, float, int, int]] = {}  # size, entry, lev, sign

        def account(self) -> AccountState:
            from hl_agent.data.models import Direction, Position

            rows, used, upnl = [], 0.0, 0.0
            for a, (sz, entry, lev, sign) in self.pos.items():
                px = replay.price(a) or entry
                m, u = sz * entry / lev, (px - entry) * sign * sz
                used += m
                upnl += u
                d = Direction.LONG if sign > 0 else Direction.SHORT
                rows.append(Position(a, d, sz, entry, lev, m, u, None, u / m * 100))
            return AccountState(self.cash + upnl, self.cash - used, used, tuple(rows))

        def price(self, a: str) -> float | None:
            return replay.price(a)

        def instrument(self, a: str):  # type: ignore[no-untyped-def]
            return next((i for i in replay.instruments() if i.name == a), None)

        def open(self, plan: OrderPlan, stop: float, now_ms: int) -> Fill:
            px = replay.price(plan.asset) or plan.reference_price
            self.pos[plan.asset] = (plan.size, px, plan.leverage, plan.direction.sign)
            return Fill(plan.asset, plan.direction, plan.size, px, 0.0, now_ms)

        def close(self, a: str, reason: CloseReason, now_ms: int) -> Fill:
            from hl_agent.data.models import Direction

            sz, entry, _lev, sign = self.pos.pop(a)
            px = replay.price(a) or entry
            self.cash += (px - entry) * sign * sz
            return Fill(a, Direction.LONG if sign > 0 else Direction.SHORT, sz, px, 0.0, now_ms)

        def set_stop(self, a: str, stop: float, now_ms: int) -> None:
            pass

        def external_close(self, asset: str, now_ms: int) -> tuple[CloseReason, Fill] | None:
            return None

    paper = Paper()
    replay._account = paper.account  # the replay's clearinghouse view is the paper wallet
    pkg = load_package(STRATEGIES / "pendulum", replay, freeze_time=True, env={"HL_WALLET": "0x"})
    replay.now_ms = 200 * H
    engine = Engine(pkg.engine_config, paper, paper, pkg.source, now_ms=replay.now_ms)
    events = []
    for hour in range(200, 600):
        replay.now_ms = hour * H
        events.extend(engine.step(replay.now_ms))
    kinds = {e.kind for e in events}
    assert "opened" in kinds and "closed" in kinds
    closes = [e for e in events if e.kind == "closed"]
    assert all(e.reason in {r.value for r in CloseReason} for e in closes)
    assert all("pnl_usd" in e.payload for e in closes)


@pytest.mark.skipif(not SENPI.exists(), reason="Senpi catalog not available locally")
def test_senpi_tortoise_runs_unmodified_on_our_shim(replay: ReplaySource, tmp_path: Path) -> None:
    pkg = load_package(
        SENPI / "tortoise" / "main",
        replay,
        state_dir=tmp_path,
        freeze_time=True,
        env={"TORTOISE_WALLET": "0x"},
    )
    rep = pkg.validate(replay.now_ms)["tortoise_main_signals"]
    assert rep.ok and rep.proven, rep
    first = pkg.source.signals(replay.now_ms)
    assert (
        [s.asset for s in first] == ["BTC"] and first[0].margin_pct == 8 and first[0].leverage == 2
    )
    # next tick within the 24h cadence emits nothing for BTC, picks the next never-DCA'd asset
    replay.now_ms += 1800 * 1000
    second = pkg.source.signals(replay.now_ms)
    assert [s.asset for s in second] == ["ETH"]
