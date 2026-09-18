"""Push notifications: subscription store, message wording, and the live-run watcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hl_agent.web.app import STOP_FILE, WebConfig, create_app
from hl_agent.web.push import Notification, PushService, SubscriptionGone
from hl_agent.web.watch import RunWatcher, describe, fmt_held, fmt_px

from .test_app import NOW_MS, NOW_S, write_run

SUB = {"endpoint": "https://push.example/a", "keys": {"p256dh": "P", "auth": "A"}}
SUB2 = {"endpoint": "https://push.example/b", "keys": {"p256dh": "P", "auth": "A"}}


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.gone: set[str] = set()

    def __call__(self, sub: dict[str, Any], payload: str) -> None:
        if sub["endpoint"] in self.gone:
            raise SubscriptionGone(sub["endpoint"])
        self.sent.append((sub["endpoint"], json.loads(payload)))


# ---- PushService -------------------------------------------------------------------------


def test_subscriptions_persist_and_dedupe(tmp_path: Path) -> None:
    svc = PushService(tmp_path / "push", sender=FakeSender())
    assert svc.subscribe(SUB) == 1
    assert svc.subscribe(SUB) == 1  # same endpoint replaces, never duplicates
    assert svc.subscribe(SUB2) == 2
    again = PushService(tmp_path / "push", sender=FakeSender())
    assert [s["endpoint"] for s in again] == [SUB["endpoint"], SUB2["endpoint"]]
    assert again.unsubscribe(SUB["endpoint"]) == 1
    assert len(PushService(tmp_path / "push")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        {"endpoint": "http://plain/a", "keys": {"p256dh": "P", "auth": "A"}},
        {"endpoint": "https://push/a", "keys": {"p256dh": "P"}},
        {"endpoint": "https://push/a"},
    ],
)
def test_bad_subscriptions_are_rejected(tmp_path: Path, bad: Any) -> None:
    with pytest.raises(ValueError):
        PushService(tmp_path / "push").subscribe(bad)


def test_corrupt_store_is_ignored(tmp_path: Path) -> None:
    d = tmp_path / "push"
    d.mkdir()
    (d / "subscriptions.json").write_text("{not json", encoding="utf-8")
    assert len(PushService(d)) == 0


def test_notify_sends_to_all_and_forgets_gone_endpoints(tmp_path: Path) -> None:
    sender = FakeSender()
    svc = PushService(tmp_path / "push", sender=sender, log=lambda _m: None)
    svc.subscribe(SUB)
    svc.subscribe(SUB2)
    sender.gone.add(SUB2["endpoint"])
    assert svc.notify(Notification("t", "b", "/x", "tag")) == 1
    want = {"title": "t", "body": "b", "url": "/x", "tag": "tag"}
    assert sender.sent == [(SUB["endpoint"], want)]
    assert [s["endpoint"] for s in svc] == [SUB["endpoint"]]


def test_notify_never_raises(tmp_path: Path) -> None:
    logged: list[str] = []

    def boom(_sub: dict[str, Any], _payload: str) -> None:
        raise RuntimeError("apple is down")

    svc = PushService(tmp_path / "push", sender=boom, log=logged.append)
    svc.subscribe(SUB)
    assert svc.notify(Notification("t", "b")) == 0
    assert logged and "apple is down" in logged[0]
    assert len(svc) == 1  # a hiccup is not a dead subscription


def test_vapid_key_is_generated_once_and_never_exposed(tmp_path: Path) -> None:
    pytest.importorskip("py_vapid")
    svc = PushService(tmp_path / "push")
    key = svc.public_key
    assert len(key) == 87 and key[0] == "B"  # 65-byte uncompressed point, base64url
    assert (tmp_path / "push" / "vapid.pem").exists()
    assert PushService(tmp_path / "push").public_key == key
    assert "PRIVATE" not in key


# ---- wording -----------------------------------------------------------------------------


def test_formatting_helpers() -> None:
    assert fmt_px(65432.1) == "65 432,10"
    assert fmt_px(0.004567) == "0,0045670"
    assert fmt_px(0.0) == "—"
    assert fmt_held(45) == "45 min"
    assert fmt_held(125) == "2 h 05"
    assert fmt_held(3 * 1440) == "3 j"


def test_describe_opened_closed_and_gates() -> None:
    opened = {
        "kind": "opened",
        "asset": "BTC",
        "payload": {
            "direction": "LONG",
            "leverage": 3,
            "entry_price": 65000.0,
            "notional_usd": 150.0,
            "stop_price": 63000.0,
        },
    }
    n = describe("live", opened)
    assert n is not None
    assert n.title == "BTC LONG 3x ouvert"
    assert n.body == "Entrée 65 000,00 · 150 $ · stop 63 000,00"
    assert n.url == "/?run=live" and n.tag == "live:BTC"

    closed = {
        "kind": "closed",
        "asset": "ETH",
        "reason": "dsl_breach",
        "payload": {"direction": "SHORT", "roe_pct": -4.25, "pnl_usd": -2.1, "held_minutes": 90},
    }
    n = describe("live", closed)
    assert n is not None
    assert n.title == "ETH SHORT fermé -4,2 %"
    assert n.body == "Stop suiveur · PnL -2,10 $ · 1 h 30"

    gate = {"kind": "rejected", "asset": "SOL", "reason": "risk_gate_daily_loss", "payload": {}}
    n = describe("live", gate)
    assert n is not None
    assert n.title == "Garde-fou : perte journalière atteinte"

    assert describe("live", {"kind": "rejected", "asset": "SOL", "reason": "spread"}) is None
    assert describe("live", {"kind": "stop_moved", "asset": "SOL"}) is None
    assert describe("live", {"kind": "closed", "asset": "SOL", "payload": "junk"}) is not None


# ---- watcher -----------------------------------------------------------------------------


def line(t_ms: int, kind: str, reason: str = "", **payload: Any) -> str:
    return json.dumps(
        {"time_ms": t_ms, "kind": kind, "asset": "BTC", "reason": reason, "payload": payload}
    )


def append(run: Path, *lines: str) -> None:
    with (run / "events.jsonl").open("a", encoding="utf-8") as fh:
        for text in lines:
            fh.write(text + "\n")


def make_watcher(tmp_path: Path, clock: dict[str, float]) -> tuple[RunWatcher, FakeSender]:
    sender = FakeSender()
    svc = PushService(tmp_path / "push", sender=sender)
    svc.subscribe(SUB)
    return RunWatcher(tmp_path / "runs", svc, clock=lambda: clock["now"]), sender


def test_watcher_skips_history_and_reports_new_events(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    live = write_run(runs, "live", [100.0, 101.0])
    append(live, line(NOW_MS - 1000, "opened", direction="LONG", leverage=2, entry_price=1.0))
    back = write_run(runs, "bt", [100.0, 90.0])
    (back / "metrics.json").write_text("{}", encoding="utf-8")
    clock = {"now": NOW_S}
    w, sender = make_watcher(tmp_path, clock)

    assert w.poll() == []  # first call primes: yesterday's trades stay on disk
    assert w.poll() == []
    append(
        live,
        line(NOW_MS, "closed", "hard_timeout", direction="LONG", roe_pct=1.5, pnl_usd=0.3),
        line(NOW_MS, "rejected", "spread"),
    )
    append(back, line(NOW_MS, "closed", "hard_timeout"))  # backtests never buzz
    notes = w.poll()
    assert [n.title for n in notes] == ["BTC LONG fermé +1,5 %"]
    assert sender.sent[-1][1]["url"] == "/?run=live"
    assert w.poll() == []


def test_watcher_keeps_a_partial_line_for_next_time(tmp_path: Path) -> None:
    live = write_run(tmp_path / "runs", "live", [100.0])
    clock = {"now": NOW_S}
    w, _ = make_watcher(tmp_path, clock)
    w.poll()
    full = line(NOW_MS, "opened", direction="SHORT", leverage=5, entry_price=2.0)
    with (live / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(full[:20])
    assert w.poll() == []
    with (live / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(full[20:] + "\n")
    assert [n.title for n in w.poll()] == ["BTC SHORT 5x ouvert"]


def test_watcher_reports_each_guard_rail_once_a_day(tmp_path: Path) -> None:
    live = write_run(tmp_path / "runs", "live", [100.0])
    clock = {"now": NOW_S}
    w, _ = make_watcher(tmp_path, clock)
    w.poll()
    day = 86_400_000
    append(
        live,
        line(NOW_MS, "rejected", "risk_gate_daily_loss"),
        line(NOW_MS + 60_000, "rejected", "risk_gate_daily_loss"),
        line(NOW_MS + 60_000, "rejected", "risk_gate_cooldown"),
        line(NOW_MS + day, "rejected", "risk_gate_daily_loss"),
    )
    titles = [n.title for n in w.poll()]
    assert titles == [
        "Garde-fou : perte journalière atteinte",
        "Garde-fou : série de pertes, pause",
        "Garde-fou : perte journalière atteinte",
    ]


def test_watcher_reports_kill_switch_and_silence(tmp_path: Path) -> None:
    live = write_run(tmp_path / "runs", "live", [100.0])
    clock = {"now": NOW_S}
    w, _ = make_watcher(tmp_path, clock)
    w.poll()
    clock["now"] = NOW_S + 3600  # no equity tick for an hour
    assert [n.title for n in w.poll()] == ["Agent silencieux"]
    assert w.poll() == []
    write_run(tmp_path / "runs2", "x", [1.0])  # unrelated
    with (live / "equity.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time_ms": int(clock["now"] * 1000), "account_value": 100.0}) + "\n")
    assert [n.title for n in w.poll()] == ["Agent de retour"]
    (live / STOP_FILE).write_text("", encoding="utf-8")
    assert [n.title for n in w.poll()] == ["Kill switch"]
    clock["now"] += 3600
    assert w.poll() == []  # silence after a kill switch is expected, not news


def test_watcher_picks_up_runs_started_later(tmp_path: Path) -> None:
    write_run(tmp_path / "runs", "live", [100.0])
    clock = {"now": NOW_S}
    w, _ = make_watcher(tmp_path, clock)
    w.poll()
    new = write_run(tmp_path / "runs", "live2", [100.0])
    append(new, line(NOW_MS, "opened", direction="LONG", leverage=1, entry_price=3.0))
    assert [n.title for n in w.poll()] == ["BTC LONG 1x ouvert"]


# ---- API ---------------------------------------------------------------------------------


def client(env: dict[str, Any], push: PushService | None) -> TestClient:
    cfg = WebConfig("testnet", "0xme", env["runs"], env["tmp"] / "cache", 3)
    app = create_app(cfg, env["view"], lambda: env["feed"], clock=lambda: NOW_S, push=push)
    return TestClient(app)


def test_push_routes_404_without_service(env: dict[str, Any]) -> None:
    c = client(env, None)
    assert c.get("/api/push").json()["available"] is False
    assert c.post("/api/push/subscribe", json=SUB).status_code == 404


def test_push_routes(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pywebpush")
    sender = FakeSender()
    svc = PushService(env["tmp"] / "push", sender=sender)
    c = client(env, svc)
    info = c.get("/api/push").json()
    assert info["available"] is True and info["subscriptions"] == 0
    assert len(info["public_key"]) == 87

    assert c.post("/api/push/subscribe", json={"endpoint": "nope"}).status_code == 400
    r = c.post("/api/push/subscribe", json=SUB)
    assert r.status_code == 200 and r.json() == {"ok": True, "subscriptions": 1}

    r = c.post("/api/push/test")
    assert r.json() == {"sent": 1}
    assert sender.sent[0][1]["title"] == "Notifications actives"
    assert "testnet" in sender.sent[0][1]["body"]

    r = c.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert r.json() == {"ok": True, "subscriptions": 0}
