"""Turns what live runs write on disk into push notifications.

Every few seconds the watcher tails ``events.jsonl`` of each live run from where it left
off and looks at the run's heartbeat. It never touches the agent: a crash here costs a
notification, never a trade. On start it skips history, so restarting the dashboard does not
replay yesterday's trades to the phone.

What gets a notification: a position opened or closed (with PnL and the exit reason), an
account-level guard rail firing (once per rail per UTC day), the kill switch, and a live run
going silent or coming back.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hl_agent.web.app import run_summary, walk_runs
from hl_agent.web.push import Notification, PushService

POLL_S = 5.0
ACCOUNT_GATES = {
    "risk_gate_daily_loss": "perte journalière atteinte",
    "risk_gate_max_drawdown": "drawdown maximal atteint",
    "risk_gate_cooldown": "série de pertes, pause",
}
CLOSE_LABEL = {
    "exchange_sl_hit": "Stop exchange",
    "dsl_breach": "Stop suiveur",
    "hard_timeout": "Durée max",
    "weak_peak_cut": "Sommet faible",
    "dead_weight_cut": "Poids mort",
    "flipped": "Retournement",
    "manual_close": "Fermeture manuelle",
    "source_closed": "Trader source sorti",
    "closed_externally": "Fermée hors agent",
    "liquidated": "LIQUIDATION",
}


def fmt_px(x: float) -> str:
    if not x or not math.isfinite(x):
        return "—"
    dec = 2 if abs(x) >= 1 else min(8, 4 - math.floor(math.log10(abs(x))))
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


def fmt_held(minutes: float) -> str:
    m = round(minutes)
    if m < 60:
        return f"{m} min"
    if m < 48 * 60:
        return f"{m // 60} h {m % 60:02d}"
    return f"{m // 1440} j"


def _sign(x: float, dec: int) -> str:
    return f"{x:+.{dec}f}".replace(".", ",")


def describe(run: str, ev: dict[str, Any]) -> Notification | None:
    """The notification for one event line, or ``None`` when it is not worth a buzz."""
    kind, asset = str(ev.get("kind")), str(ev.get("asset"))
    p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    assert isinstance(p, dict)
    url = f"/?run={run}"
    lev = p.get("leverage")
    direction = str(p.get("direction") or "")
    if kind == "opened":
        stop = p.get("stop_price")
        body = f"Entrée {fmt_px(float(p.get('entry_price') or 0))}"
        if p.get("notional_usd") is not None:
            body += f" · {float(p['notional_usd']):.0f} $"
        if stop:
            body += f" · stop {fmt_px(float(stop))}"
        return Notification(f"{asset} {direction} {lev}x ouvert", body, url, f"{run}:{asset}")
    if kind == "closed":
        roe = float(p.get("roe_pct") or 0)
        pnl = float(p.get("pnl_usd") or 0)
        label = CLOSE_LABEL.get(str(ev.get("reason")), str(ev.get("reason")))
        body = f"{label} · PnL {_sign(pnl, 2)} $"
        if p.get("held_minutes") is not None:
            body += f" · {fmt_held(float(p['held_minutes']))}"
        return Notification(
            f"{asset} {direction} fermé {_sign(roe, 1)} %", body, url, f"{run}:{asset}"
        )
    if kind == "rejected" and str(ev.get("reason")) in ACCOUNT_GATES:
        label = ACCOUNT_GATES[str(ev["reason"])]
        return Notification(
            f"Garde-fou : {label}", f"{run} · entrées suspendues", url, f"{run}:gate"
        )
    return None


@dataclass
class _RunState:
    offset: int = 0
    alive: bool | None = None
    stop: bool = False
    gated: set[tuple[str, int]] = field(default_factory=set)  # (reason, utc day)


class RunWatcher:
    def __init__(
        self,
        runs_dir: Path,
        push: PushService,
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
    ) -> None:
        self.runs_dir, self.push = runs_dir, push
        self._clock, self._log = clock, log
        self._runs: dict[str, _RunState] = {}
        self._primed = False

    # ---- one pass ------------------------------------------------------------------

    def _live_runs(self) -> list[tuple[str, Path, dict[str, Any]]]:
        now = self._clock()
        out = []
        for name, d in walk_runs(self.runs_dir):
            s = run_summary(d, now_s=now, name=name)
            if s["kind"] == "live":
                out.append((name, d, s))
        return out

    def prime(self) -> None:
        """Remember where every log ends so only what happens from now on is reported."""
        for name, d, s in self._live_runs():
            st = self._runs.setdefault(name, _RunState())
            ev = d / "events.jsonl"
            st.offset = ev.stat().st_size if ev.exists() else 0
            st.alive, st.stop = bool(s["alive"]), bool(s["stop"])
        self._primed = True

    def poll(self) -> list[Notification]:
        if not self._primed:
            self.prime()
            return []
        notes: list[Notification] = []
        for name, d, s in self._live_runs():
            st = self._runs.get(name)
            if st is None:  # a run that appeared after start: report it from its first line
                st = self._runs[name] = _RunState(alive=bool(s["alive"]), stop=bool(s["stop"]))
            notes += self._tail(name, d / "events.jsonl", st)
            notes += self._heartbeat(name, s, st)
        for n in notes:
            self.push.notify(n)
        return notes

    def _tail(self, name: str, path: Path, st: _RunState) -> list[Notification]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < st.offset:  # truncated / rewritten
            st.offset = 0
        if size == st.offset:
            return []
        with path.open("rb") as fh:
            fh.seek(st.offset)
            chunk = fh.read(size - st.offset)
        # keep a partial last line (crash mid-write, or a write in progress) for next time
        cut = chunk.rfind(b"\n") + 1
        st.offset += cut
        out: list[Notification] = []
        for line in chunk[:cut].splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            note = describe(name, ev)
            if note is None:
                continue
            if ev.get("kind") == "rejected":
                key = (str(ev.get("reason")), int(ev.get("time_ms") or 0) // 86_400_000)
                if key in st.gated:
                    continue
                st.gated.add(key)
            out.append(note)
        return out

    def _heartbeat(self, name: str, s: dict[str, Any], st: _RunState) -> list[Notification]:
        out: list[Notification] = []
        url = f"/?run={name}"
        stop = bool(s["stop"])
        if stop and not st.stop:
            out.append(
                Notification("Kill switch", f"{name} : l'agent ferme tout et s'arrête", url, name)
            )
        st.stop = stop
        alive = bool(s["alive"])
        if st.alive is not None and alive != st.alive and not stop:
            if alive:
                out.append(Notification("Agent de retour", f"{name} tourne à nouveau", url, name))
            else:
                last = s.get("last_tick_ms")
                mins = int((self._clock() - float(last) / 1000) / 60) if last else 0
                out.append(
                    Notification(
                        "Agent silencieux",
                        f"{name} · aucun tick depuis {mins} min (arrêté ou planté ?)",
                        url,
                        name,
                    )
                )
        st.alive = alive
        return out

    # ---- thread -------------------------------------------------------------------

    def start(self, interval_s: float = POLL_S) -> threading.Thread:
        def loop() -> None:
            while True:
                try:
                    self.poll()
                except Exception as exc:  # never let the watcher die on a bad line
                    self._log(f"watcher: {exc!r}")
                time.sleep(interval_s)

        t = threading.Thread(target=loop, name="hl-agent-push-watcher", daemon=True)
        t.start()
        return t
