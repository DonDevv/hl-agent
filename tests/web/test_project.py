"""Dashboard, whole-project scope: strategy catalog, jobs, nested runs, data cache, settings."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hl_agent.web.app import RUN_META, WebConfig, create_app, list_runs
from hl_agent.web.catalog import cached_card, card, find_packages, read_manifest
from hl_agent.web.jobs import JobError, JobRegistry, build_argv
from tests.web.test_app import NOW_MS, NOW_S, write_run

RECIPE = """
name: demo-main
group: demo
version: 0.1.0
description: A tiny recipe.
strategy:
  wallet: "${HL_WALLET}"
  slots: 1
  margin_pct: 10
  default_leverage: 2
  trading_risk: conservative
  enabled: true
scanners:
  - name: position_tracker
    type: position_tracker
    interval_seconds: 10
  - name: demo_signals
    type: external_scanner
    path: ./scanners
    entrypoint: scan.py
    interval_seconds: 900
    timeout_seconds: 60
    default_signal_validity_seconds: 1800
    inputs:
      assets: ["BTC", "ETH"]
      lookback: 5
"""
MANIFEST = """
id: demo
catalog:
  name: Demo
  emoji: "🐢"
  tagline: Slow and steady.
  risk_level: low
  tags: [trend, demo]
"""


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    root = tmp_path / "strategies"
    main = root / "demo" / "main"
    main.mkdir(parents=True)
    (main / "runtime.yaml").write_text(RECIPE, encoding="utf-8")
    (root / "demo" / "strategy.yaml").write_text(MANIFEST, encoding="utf-8")
    broken = root / "broken"
    broken.mkdir()
    (broken / "runtime.yaml").write_text("name: [unclosed", encoding="utf-8")
    (root / "demo" / "tests").mkdir()
    (root / "demo" / "tests" / "runtime.yaml").write_text(RECIPE, encoding="utf-8")  # skipped
    return root


def test_find_packages_skips_tests_and_reads_manifest(packages: Path) -> None:
    found = find_packages([packages, packages / "missing"])
    assert [pid for pid, _, _ in found] == ["broken", "demo/main"]
    assert read_manifest(packages / "demo" / "main")["emoji"] == "🐢"
    assert read_manifest(packages / "broken") == {}


def test_card_reads_recipe_and_flags_broken_ones(packages: Path) -> None:
    c = card("demo/main", packages / "demo" / "main", packages, {"HL_WALLET": "0xme"})
    assert c.name == "demo-main" and c.group == "demo"
    assert c.leverage == 2 and c.assets == ["BTC", "ETH"]
    assert c.catalog["name"] == "Demo" and c.error == ""
    (sc,) = [s for s in c.scanners if s["type"] == "external_scanner"]
    assert sc["inputs"] == {"lookback": 5}  # assets are lifted out
    b = card("broken", packages / "broken", packages, {})
    assert b.error and b.name == "broken" and b.leverage == 0
    same = cached_card("demo/main", packages / "demo" / "main", packages, {"HL_WALLET": "0xme"})
    assert cached_card("demo/main", packages / "demo" / "main", packages, {}) is same


def known(packages: Path) -> dict[str, Path]:
    return {pid: d for pid, d, _ in find_packages([packages])}


def test_build_argv_whitelists_every_kind(packages: Path) -> None:
    pk = lambda: known(packages)  # noqa: E731
    demo = str(packages / "demo" / "main")
    assert build_argv(
        "fetch",
        {"assets": "btc, eth", "intervals": "1H", "since": "2025-01-01"},
        packages=pk,
        network="testnet",
    ) == ["fetch", "--assets", "BTC", "ETH", "--intervals", "1h", "--since", "2025-01-01"]
    assert build_argv("validate", {"package": "demo/main"}, packages=pk, network="testnet") == [
        "validate",
        demo,
    ]
    bt = build_argv(
        "backtest",
        {
            "package": "demo/main",
            "start": "2025-01-01",
            "end": "2025-03-01",
            "cash": 500,
            "leverage": 3,
            "step_hours": 4,
            "out": "bt-demo",
        },
        packages=pk,
        network="testnet",
    )
    assert bt == [
        "backtest",
        demo,
        "--start",
        "2025-01-01",
        "--end",
        "2025-03-01",
        "--cash",
        "500",
        "--step-hours",
        "4",
        "--leverage",
        "3",
        "--out",
        "bt-demo",
    ]
    wf = build_argv(
        "walkforward", {"package": "demo/main", "folds": 3}, packages=pk, network="testnet"
    )
    assert wf[:4] == ["walkforward", demo, "--folds", "3"] and wf[4] == "--out"
    assert wf[5].startswith("wf-demo-main-")  # a run name is always assigned
    live = build_argv(
        "run",
        {
            "package": "demo/main",
            "name": "copy-live",
            "copy": "0x" + "AB" * 20,
            "poll": 60,
            "budget": 999,
        },
        packages=pk,
        network="testnet",
    )
    assert live == [
        "run",
        demo,
        "--name",
        "copy-live",
        "--copy",
        "0x" + "ab" * 20,
        "--poll",
        "60",
        "--budget",
        "999",
    ]


@pytest.mark.parametrize(
    ("kind", "params", "msg"),
    [
        ("nope", {}, "unknown job kind"),
        ("validate", {"package": "../etc"}, "unknown package"),
        ("backtest", {"package": "demo/main", "start": "yesterday"}, "bad start"),
        ("backtest", {"package": "demo/main", "cash": 1}, "cash must be"),
        ("backtest", {"package": "demo/main", "leverage": 50}, "leverage must be"),
        ("backtest", {"package": "demo/main", "out": "../x"}, "bad run name"),
        ("run", {"package": "demo/main", "copy": "not-an-address"}, "bad trader"),
        ("run", {"package": "demo/main", "interval": 1}, "interval must be"),
        ("fetch", {"assets": "BTC;rm"}, "bad asset"),
    ],
)
def test_build_argv_rejects_bad_input(
    packages: Path, kind: str, params: dict[str, Any], msg: str
) -> None:
    with pytest.raises(JobError, match=msg):
        build_argv(kind, params, packages=lambda: known(packages), network="testnet")


def test_mainnet_live_run_needs_the_consent_flag(packages: Path) -> None:
    pk = lambda: known(packages)  # noqa: E731
    with pytest.raises(JobError, match="real money"):
        build_argv("run", {"package": "demo/main"}, packages=pk, network="mainnet")
    argv = build_argv(
        "run", {"package": "demo/main", "accept_real_money": True}, packages=pk, network="mainnet"
    )
    assert argv[-1] == "--i-accept-real-money"
    # testnet never adds it, even if the page sends the flag
    argv = build_argv(
        "run", {"package": "demo/main", "accept_real_money": True}, packages=pk, network="testnet"
    )
    assert "--i-accept-real-money" not in argv


FAKE_CLI = """
import sys, time
print("argv:", sys.argv[1:], flush=True)
if "--sleep" in sys.argv or sys.argv[1:2] == ["run"]:
    time.sleep(30)
sys.exit(3 if "validate" in sys.argv else 0)
"""


@pytest.fixture
def registry(tmp_path: Path, packages: Path, monkeypatch: pytest.MonkeyPatch) -> JobRegistry:
    # a stand-in for ``python -m hl_agent.cli``: a stub module on the child's path
    stub = tmp_path / "stub"
    (stub / "hl_agent").mkdir(parents=True)
    (stub / "hl_agent" / "__init__.py").write_text("", encoding="utf-8")
    (stub / "hl_agent" / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(stub))
    return JobRegistry(
        log_dir=tmp_path / "jobs",
        cwd=tmp_path,
        settings_path=tmp_path / "settings.toml",
        packages=lambda: known(packages),
        network="testnet",
        python=sys.executable,
    )


def wait_done(reg: JobRegistry, jid: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = reg.get(jid)
        assert job is not None
        job.poll()
        if not job.running:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_registry_runs_logs_and_reports_exit_codes(registry: JobRegistry) -> None:
    job = registry.launch("validate", {"package": "demo/main"})
    assert job.label == "validate demo/main" and job.run == "" and job.running
    assert registry.base_argv()[1:3] == ["-m", "hl_agent.cli"]
    wait_done(registry, job.id)
    j = job.to_json(log_lines=20)
    assert j["returncode"] == 3 and j["ended_ms"] and "argv: ['--settings'" in j["log"]
    assert j["log"].startswith("$ hl-agent validate")
    bt = registry.launch("backtest", {"package": "demo/main", "out": "bt-x"})
    assert bt.run == "bt-x" and bt.label == "backtest demo/main"
    wait_done(registry, bt.id)
    assert [x.id for x in registry.list()] == [bt.id, job.id]  # newest first


def test_registry_allows_one_live_run_and_can_kill_it(registry: JobRegistry) -> None:
    live = registry.launch("run", {"package": "demo/main", "name": "live"})
    assert live.run == "live"
    with pytest.raises(JobError, match="already running"):
        registry.launch("run", {"package": "demo/main", "name": "live2"})
    killed = registry.kill(live.id)
    assert not killed.running and killed.returncode is not None
    with pytest.raises(JobError, match="no job"):
        registry.kill("nope")
    registry.launch("run", {"package": "demo/main", "name": "live3"})  # slot is free again
    registry.kill(registry.list()[0].id)


# ---- routes -------------------------------------------------------------------------


def make_app(env: dict[str, Any], packages: Path, *, token: str = "", jobs: Any = None) -> Any:
    cfg = WebConfig(
        "testnet",
        "0xme",
        env["runs"],
        env["tmp"] / "cache",
        3,
        token=token,
        strategy_dirs=(packages,),
        settings_path=env["tmp"] / "s.toml",
        settings={"min_notional_usd": 10.0, "taker_pct": 0.045},
        env={"HL_WALLET": "0xme"},
    )
    return create_app(cfg, env["view"], lambda: env["feed"], clock=lambda: NOW_S, jobs=jobs)


def test_nested_runs_are_grouped_and_addressable(env: dict[str, Any], packages: Path) -> None:
    runs = env["runs"]
    for i, vals in enumerate([[100.0, 110.0], [110.0, 99.0]]):
        d = write_run(runs, f"wf-demo/fold{i}", vals, meta=False, end_ms=NOW_MS - 3_600_000)
        (d / RUN_META).write_text(
            json.dumps(
                {
                    "kind": "walkforward",
                    "package": str(packages / "demo" / "main"),
                    "strategy": "demo-main",
                    "window": [NOW_MS - 7_200_000, NOW_MS],
                }
            ),
            encoding="utf-8",
        )
    d = write_run(runs, "bt-demo", [100.0, 120.0], meta=False)
    (d / RUN_META).write_text(
        json.dumps({"kind": "backtest", "package": str(packages / "demo" / "main")}),
        encoding="utf-8",
    )
    listed = {r["name"]: r for r in list_runs(runs, now_s=NOW_S)}
    wf = listed["wf-demo"]
    assert wf["kind"] == "walkforward" and [f["name"] for f in wf["folds"]] == [
        "wf-demo/fold0",
        "wf-demo/fold1",
    ]
    assert wf["profitable_folds"] == 1 and wf["return_pct"] == pytest.approx(-1.0)
    assert listed["bt-demo"]["kind"] == "backtest" and listed["bt-demo"][
        "return_pct"
    ] == pytest.approx(20.0)
    assert [r["kind"] for r in list_runs(runs, now_s=NOW_S)][:1] == ["live"]  # live first

    c = TestClient(make_app(env, packages))
    eq = c.get("/api/runs/wf-demo/fold1/equity").json()
    assert eq[-1][1] == 99.0
    assert c.get("/api/runs/wf-demo/fold9/equity").status_code == 404
    cards = {s["id"]: s for s in c.get("/api/strategies").json()}
    assert cards["demo/main"]["runs"] == 2 and cards["broken"]["error"]  # wf counts once
    one = c.get("/api/strategies/demo/main").json()
    assert "demo-main" in one["runtime_yaml"] and '"0xme"' in one["runtime_yaml"]
    assert {r["name"] for r in one["run_list"]} == {"wf-demo", "bt-demo"}
    assert c.get("/api/strategies/nope").status_code == 404


def test_jobs_routes(env: dict[str, Any], packages: Path, registry: JobRegistry) -> None:
    c = TestClient(make_app(env, packages, jobs=registry))
    assert c.get("/api/jobs").json() == []
    r = c.post("/api/jobs", json={"kind": "run", "params": {"package": "demo/main"}})
    assert r.status_code == 403  # no token: no live runs from the page
    r = c.post("/api/jobs", json={"kind": "backtest", "params": {"package": "nope"}})
    assert r.status_code == 400 and "unknown package" in r.json()["error"]
    r = c.post("/api/jobs", json={"kind": "validate", "params": {"package": "demo/main"}})
    assert r.status_code == 200
    jid = r.json()["id"]
    wait_done(registry, jid)
    j = c.get(f"/api/jobs/{jid}?lines=5").json()
    assert j["returncode"] == 3 and "argv:" in j["log"] and j["label"] == "validate demo/main"
    assert c.get("/api/jobs/nope").status_code == 404
    assert c.post("/api/jobs/nope/kill").status_code == 404
    assert [x["id"] for x in c.get("/api/jobs").json()] == [jid]

    auth = TestClient(make_app(env, packages, token="t", jobs=registry))
    h = {"authorization": "Bearer t"}
    r = auth.post("/api/jobs", json={"kind": "run", "params": {"package": "demo/main"}}, headers=h)
    assert r.status_code == 200 and r.json()["running"]
    k = auth.post(f"/api/jobs/{r.json()['id']}/kill", headers=h).json()
    assert not k["running"]


def test_data_and_settings_routes(env: dict[str, Any], packages: Path) -> None:
    import polars as pl

    cache = env["tmp"] / "cache"
    cache.mkdir(parents=True)
    pl.DataFrame({"open_ms": [1_000, 2_000, 3_000], "close": [1.0, 2.0, 3.0]}).write_parquet(
        cache / "BTC_1h.parquet"
    )
    c = TestClient(make_app(env, packages))
    d = c.get("/api/data").json()
    assert d["cache_dir"] == str(cache)
    (s,) = d["series"]
    assert s["asset"] == "BTC" and s["interval"] == "1h" and s["bars"] == 3
    assert s["first_ms"] == 1_000 and s["last_ms"] == 3_000
    st = c.get("/api/settings").json()
    assert st["network"] == "testnet" and st["max_leverage"] == 3 and st["auth"] is False
    assert st["strategy_dirs"] == [str(packages)] and st["taker_pct"] == 0.045
    assert st["settings_path"].endswith("s.toml")
