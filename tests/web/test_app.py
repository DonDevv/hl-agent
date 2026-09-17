"""Dashboard API: state, runs, kill switch, auth, traders cache and mirror plans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hl_agent.engine.loop import Event
from hl_agent.web.app import (
    COOKIE,
    RUN_META,
    STOP_FILE,
    TradersCache,
    WebConfig,
    create_app,
    resolve_token,
    run_summary,
    safe_run,
)

NOW_S = 1_800_000_000.0
NOW_MS = int(NOW_S * 1000)
TRADER = "0x" + "cd" * 20


def write_run(
    runs: Path,
    name: str,
    values: list[float],
    *,
    step_s: int = 60,
    end_ms: int = NOW_MS,
    meta: bool = True,
) -> Path:
    d = runs / name
    d.mkdir(parents=True)
    lines = [
        json.dumps({"time_ms": end_ms - (len(values) - 1 - i) * step_s * 1000, "account_value": v})
        for i, v in enumerate(values)
    ]
    (d / "equity.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if meta:
        (d / RUN_META).write_text(
            json.dumps({"package": "copy", "copy": TRADER, "network": "testnet"}), encoding="utf-8"
        )
    return d


def closed(t_ms: int, pnl: float) -> Event:
    return Event(
        t_ms,
        "closed",
        "BTC",
        "tp",
        {"direction": "LONG", "held_minutes": 30, "pnl_usd": pnl, "roe_pct": pnl, "leverage": 2},
    )


def make_app(env: dict[str, Any], *, token: str = "", traders: TradersCache | None = None) -> Any:
    cfg = WebConfig("testnet", "0xme", env["runs"], env["tmp"] / "cache", 3, token=token)
    return create_app(cfg, env["view"], lambda: env["feed"], clock=lambda: NOW_S, traders=traders)


def test_state_reports_account_positions_runs_and_key(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    s = c.get("/api/state").json()
    assert s["network"] == "testnet" and s["address"] == "0xme" and s["now_ms"] == NOW_MS
    assert s["account"]["value"] == 10_000.0 and s["agent_key"].startswith("agent key")
    (p,) = s["account"]["positions"]
    assert p["asset"] == "ETH" and p["direction"] == "LONG" and p["price"] == 3100.0
    assert p["upnl"] == 10.0 and p["roe"] == 10.0 and p["liquidation"] == 2100.0
    runs = {r["name"]: r for r in s["runs"]}
    live = runs["copy-live"]
    assert live["kind"] == "live" and live["alive"] and not live["stop"]
    assert live["copy"] == TRADER and live["ticks"] == 4
    assert live["initial"] == 1000.0 and live["value"] == 1020.0
    assert runs["old"]["kind"] == "backtest" and not runs["old"]["alive"]
    assert s["runs"][0]["name"] == "copy-live"  # most recent first


def test_run_summary_without_meta_is_tolerant(tmp_path: Path) -> None:
    d = write_run(tmp_path, "bare", [1.0], meta=False)
    s = run_summary(d, now_s=NOW_S)
    assert s["package"] is None and s["copy"] is None and s["ticks"] == 1


def test_equity_events_and_report(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    eq = c.get("/api/runs/copy-live/equity").json()
    assert eq[0] == [NOW_MS - 180_000, 1000.0] and eq[-1] == [NOW_MS, 1020.0]
    evs = c.get("/api/runs/copy-live/events?limit=2").json()
    assert [e["kind"] for e in evs] == ["closed", "closed"]  # newest first, limited
    assert evs[0]["payload"]["pnl_usd"] == -4.0
    rep = c.get("/api/runs/copy-live/report").json()
    assert rep["metrics"]["trades"] == 2 and rep["metrics"]["win_rate"] == 50.0
    assert rep["metrics"]["net_pnl"] == 20.0  # from the equity curve, not the trade list
    assert [t["pnl_usd"] for t in rep["trades"]] == [-4.0, 12.0]
    assert rep["metrics"]["drawdown"]["max_pct"] == pytest.approx(100 * 20 / 1010)


def test_report_on_empty_run(env: dict[str, Any]) -> None:
    (env["runs"] / "empty").mkdir()
    c = TestClient(make_app(env))
    assert c.get("/api/runs/empty/report").json() == {"trades": [], "metrics": None}
    assert c.get("/api/runs/empty/equity").json() == []


def test_stop_and_clear_stop_toggle_the_kill_switch(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    stop = env["runs"] / "copy-live" / STOP_FILE
    assert c.post("/api/runs/copy-live/stop").json() == {"ok": True, "stop": True}
    assert stop.exists()
    assert c.get("/api/state").json()["runs"][0]["stop"] is True
    assert c.post("/api/runs/copy-live/clear-stop").json() == {"ok": True, "stop": False}
    assert not stop.exists()


def test_bad_run_names_are_rejected(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    assert c.get("/api/runs/nope/equity").status_code == 404
    assert c.get("/api/runs/nope/equity").json() == {"error": "no run named nope"}
    assert c.post("/api/runs/.hidden/stop").status_code == 400
    for bad in ("", "..", "a/../b", "/a", "a/", "a" + chr(92) + "b", ".hidden", "a/.b"):
        with pytest.raises(HTTPException) as err:
            safe_run(env["runs"], bad)
        assert err.value.status_code == 400
    assert not (env["runs"] / ".hidden").exists()
    (env["runs"] / "wf" / "fold0").mkdir(parents=True)
    assert safe_run(env["runs"], "wf/fold0") == env["runs"] / "wf" / "fold0"  # nested runs are fine


def test_token_gates_the_api_via_bearer_or_cookie(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env, token="s3cret"))
    assert c.get("/api/health").json()["auth"] is True  # health is always open
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/state", headers={"authorization": "Bearer s3cret"}).status_code == 200
    assert c.post("/api/login", json={"token": "nope"}).status_code == 401
    r = c.post("/api/login", json={"token": "s3cret"})
    assert r.status_code == 200 and COOKIE in r.cookies
    assert c.get("/api/state").status_code == 200  # cookie jar carries it


def test_without_token_everything_is_open(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    assert c.get("/api/health").json()["auth"] is False
    r = c.post("/api/login", json={"token": "anything"})
    assert r.status_code == 200 and COOKIE not in r.cookies


def test_static_shell_and_pwa_files(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    assert "<title>俺び寂び</title>" in c.get("/").text
    m = c.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "standalone"
    assert "javascript" in c.get("/sw.js").headers["content-type"]
    for f in ("app.js", "app.css", "logo.png", "favicon.png", "icon-180.png", "icon-512.png"):
        assert c.get(f"/static/{f}").status_code == 200, f


def test_mirror_plans_include_requested_and_100_dollar_budgets(env: dict[str, Any]) -> None:
    c = TestClient(make_app(env))
    m = c.get(f"/api/mirror/{TRADER}?budget=999").json()
    assert m["address"] == TRADER and m["positions"] == 1
    assert set(m["plans"]) == {"100", "999"}
    (ln,) = m["plans"]["999"]["lines"]
    assert ln["asset"] == "BTC" and ln["leverage"] == 3 and ln["og_leverage"] == 5
    assert ln["verdict"] == "open" and m["plans"]["999"]["opens"] == 1
    assert c.get("/api/mirror/abc").status_code == 400


def test_traders_cache_refreshes_on_demand_and_respects_ttl(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import hl_agent.web.app as web
    from hl_agent.copy.discovery import LeaderRow

    zero = dict.fromkeys(("day", "week", "month", "allTime"), 0.0)
    row = LeaderRow(
        TRADER,
        10_000.0,
        pnl={**zero, "month": 5000.0},  # type: ignore[arg-type]
        roi={**zero, "week": 0.2, "month": 0.5},  # type: ignore[arg-type]
        volume=zero,  # type: ignore[arg-type]
    )
    fetches = 0

    def fake_fetch(path: Path, **_: Any) -> list[LeaderRow]:
        nonlocal fetches
        fetches += 1
        return [row]

    monkeypatch.setattr(web, "fetch_leaderboard", fake_fetch)
    clock = [NOW_S]
    cache = TradersCache(
        lambda: env["feed"], env["tmp"] / "cache", max_leverage=3, ttl_s=100, clock=lambda: clock[0]
    )
    c = TestClient(make_app(env, traders=cache))
    t = c.get("/api/traders?refresh=1").json()
    assert fetches == 1 and t["error"] == "" and t["updated_ms"] == NOW_MS
    (r,) = t["rows"]
    assert r["address"] == TRADER and r["roi_30d"] == 50.0 and r["fit"] == "good"
    assert r["opens"] == 1 and r["seen_in"] == ["7d_roi", "30d_roi", "30d_pnl"]
    c.get("/api/traders?refresh=1")
    assert fetches == 1  # inside the TTL: served from memory
    clock[0] += 200
    c.get("/api/traders?refresh=1")
    assert fetches == 2


def test_traders_cache_surfaces_errors(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import hl_agent.web.app as web

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("leaderboard down")

    monkeypatch.setattr(web, "fetch_leaderboard", boom)
    cache = TradersCache(lambda: env["feed"], env["tmp"] / "c", max_leverage=3, clock=lambda: NOW_S)
    t = TestClient(make_app(env, traders=cache)).get("/api/traders?refresh=1").json()
    assert t["rows"] == [] and "leaderboard down" in t["error"] and t["refreshing"] is False


def test_resolve_token_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HL_AGENT_WEB_TOKEN", raising=False)
    assert resolve_token(None, "cfg") == "cfg"
    monkeypatch.setenv("HL_AGENT_WEB_TOKEN", "env")
    assert resolve_token(None, "cfg") == "env"
    assert resolve_token("cli", "cfg") == "cli"
