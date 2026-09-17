"""Dashboard API + PWA (``hl-agent web``).

A thin read-mostly window on what the agent already writes to ``runs/<name>/``
(``events.jsonl``, ``equity.jsonl``, ``run.json``) plus the live account from ``/info``.
The only writes are the kill switch (``STOP`` file) and clearing it.

Meant to sit on the VPS next to the agent, behind Tailscale or a reverse proxy. An optional
shared token (``HL_AGENT_WEB_TOKEN`` or ``[web] token``) gates every route when set; it is
compared in constant time and never logged.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hl_agent.copy.discovery import LeaderRow, TraderProfile, blend, fetch_leaderboard, profile
from hl_agent.copy.discovery import sort_profiles as _sort_profiles
from hl_agent.copy.mirror import MirrorPlan, TraderFeed, simulate_mirror
from hl_agent.data.models import AccountState
from hl_agent.execution.backtest import EquityPoint
from hl_agent.execution.runner import STOP_FILE
from hl_agent.telemetry.events import EventLog, to_dict
from hl_agent.telemetry.metrics import compute, trades_from_events

STATIC_DIR = Path(__file__).with_name("static")
RUN_META = "run.json"
TOKEN_ENV = "HL_AGENT_WEB_TOKEN"
COOKIE = "hl_token"
LIVE_AFTER_S = 300.0  # a run whose last equity point is older than this is shown as stale
TRADERS_TTL_S = 30 * 60


@dataclass(frozen=True, slots=True)
class WebConfig:
    network: str
    address: str
    runs_dir: Path
    cache_dir: Path
    max_leverage: int
    token: str = ""  # "" → open (rely on the network layer)


class AccountView:
    """What the page needs from the venue: the account, prices, the agent-key verdict."""

    def __init__(
        self,
        account: Callable[[], AccountState],
        prices: Callable[[], Mapping[str, float]],
        agent_key: Callable[[], str],
    ) -> None:
        self.account, self.prices, self.agent_key = account, prices, agent_key


# ---- run directory helpers -------------------------------------------------------------


def read_equity(path: Path) -> list[EquityPoint]:
    if not path.exists():
        return []
    out: list[EquityPoint] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            out.append(EquityPoint(int(raw["time_ms"]), float(raw["account_value"])))
        except (ValueError, KeyError):
            continue
    return out


def read_meta(run: Path) -> dict[str, Any]:
    p = run / RUN_META
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except ValueError:
        return {}


def run_summary(run: Path, *, now_s: float) -> dict[str, Any]:
    equity = read_equity(run / "equity.jsonl")
    last = equity[-1] if equity else None
    meta = read_meta(run)
    age_s = now_s - last.time_ms / 1000 if last else None
    return {
        "name": run.name,
        "package": meta.get("package"),
        "copy": meta.get("copy"),
        "network": meta.get("network"),
        "started_ms": meta.get("started_ms"),
        "interval_s": meta.get("interval_s"),
        "ticks": len(equity),
        "last_tick_ms": last.time_ms if last else None,
        "alive": age_s is not None
        and age_s < max(LIVE_AFTER_S, 3 * float(meta.get("interval_s") or 0)),
        "stop": (run / STOP_FILE).exists(),
        "initial": equity[0].account_value if equity else None,
        "value": last.account_value if last else None,
        "kind": "backtest" if (run / "metrics.json").exists() else "live",
    }


def list_runs(runs_dir: Path, *, now_s: float) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    runs = [d for d in runs_dir.iterdir() if d.is_dir() and (d / "equity.jsonl").exists()]
    out = [run_summary(d, now_s=now_s) for d in runs]
    out.sort(key=lambda r: (r["kind"] == "live", r["last_tick_ms"] or 0), reverse=True)
    return out


def safe_run(runs_dir: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad run name")
    d = runs_dir / name
    if not d.is_dir():
        raise HTTPException(404, f"no run named {name}")
    return d


# ---- traders cache ---------------------------------------------------------------------


class TradersCache:
    """The leaderboard blend is ~20 s of mainnet calls: computed at most every TTL,
    refreshed in a background thread so the page never waits on it."""

    def __init__(
        self,
        feed_factory: Callable[[], TraderFeed],
        cache_dir: Path,
        *,
        max_leverage: int,
        ttl_s: float = TRADERS_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._feed_factory = feed_factory
        self._cache_dir = cache_dir
        self._max_leverage = max_leverage
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        self._at: float = 0.0
        self._error = ""
        self._refreshing = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "rows": list(self._rows),
                "updated_ms": int(self._at * 1000) if self._at else None,
                "refreshing": self._refreshing,
                "error": self._error,
            }

    def maybe_refresh(self, *, background: bool = True) -> None:
        with self._lock:
            if self._refreshing or self._clock() - self._at < self._ttl:
                return
            self._refreshing = True
        if background:
            threading.Thread(target=self._refresh, daemon=True).start()
        else:
            self._refresh()

    def _refresh(self) -> None:
        try:
            rows = self.compute()
            with self._lock:
                self._rows, self._at, self._error = rows, self._clock(), ""
        except Exception as exc:  # surfaced on the page, never fatal
            with self._lock:
                self._error, self._at = repr(exc), self._clock()
        finally:
            with self._lock:
                self._refreshing = False

    def compute(self, *, top: int = 20, budget_usd: float = 100.0) -> list[dict[str, Any]]:
        rows = fetch_leaderboard(self._cache_dir / "leaderboard.json")
        by_addr = {r.address: r for r in rows}
        feed = self._feed_factory()
        prices = feed.all_mids()
        profiles: list[TraderProfile] = []
        for addr, views in blend(rows, top=top).items():
            state = feed.account_state(addr)
            profiles.append(
                profile(
                    by_addr[addr],
                    state,
                    prices,
                    budget_usd=budget_usd,
                    seen_in=views,
                    max_leverage=self._max_leverage,
                )
            )
        ranked = [p for p in _sort_profiles(profiles) if p.open_positions > 0]
        return [profile_json(p) for p in ranked]


def profile_json(p: TraderProfile) -> dict[str, Any]:
    r: LeaderRow = p.row
    return {
        "address": r.address,
        "name": r.display_name,
        "equity": r.account_value,
        "roi_7d": r.roi["week"] * 100,
        "roi_30d": r.roi["month"] * 100,
        "pnl_30d": r.pnl["month"],
        "positions": p.open_positions,
        "longs": p.longs,
        "top_asset_share": p.top_asset_share * 100,
        "margin_ratio": p.margin_ratio * 100,
        "fit": p.fit,
        "fresh_pct": p.plan.fresh_notional_pct,
        "opens": len(p.plan.to_open),
        "lines": len(p.plan.lines),
        "min_budget": p.plan.min_budget_usd,
        "seen_in": list(p.seen_in),
        "flags": list(p.flags),
    }


def plan_json(plan: MirrorPlan) -> dict[str, Any]:
    return {
        "budget": plan.budget_usd,
        "scale": plan.scale_factor,
        "og_equity": plan.og_account_value,
        "opens": len(plan.to_open),
        "margin_committed": plan.margin_committed_usd,
        "min_budget": plan.min_budget_usd,
        "fresh_pct": plan.fresh_notional_pct,
        "lines": [
            {
                "asset": ln.asset,
                "direction": ln.direction.value,
                "og_entry": ln.og_entry_price,
                "og_leverage": ln.og_leverage,
                "allocation": ln.allocation * 100,
                "moved": ln.moved_from_entry_pct,
                "price": ln.price,
                "leverage": ln.leverage,
                "margin": ln.margin_usd,
                "notional": ln.notional_usd,
                "verdict": ln.verdict,
            }
            for ln in plan.lines
        ],
    }


# ---- app -------------------------------------------------------------------------------


def create_app(
    cfg: WebConfig,
    view: AccountView,
    feed_factory: Callable[[], TraderFeed],
    *,
    clock: Callable[[], float] = time.time,
    traders: TradersCache | None = None,
) -> FastAPI:
    app = FastAPI(title="hl-agent", docs_url=None, redoc_url=None)
    traders = traders or TradersCache(
        feed_factory, cfg.cache_dir, max_leverage=cfg.max_leverage, clock=clock
    )

    def authed(request: Request) -> None:
        if not cfg.token:
            return
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        given = given or request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(given.encode(), cfg.token.encode()):
            raise HTTPException(401, "token required")

    api = Depends(authed)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "auth": bool(cfg.token), "network": cfg.network}

    @app.post("/api/login")
    def login(body: dict[str, str], response: Response) -> dict[str, bool]:
        token = str(body.get("token", ""))
        if cfg.token and not hmac.compare_digest(token.encode(), cfg.token.encode()):
            raise HTTPException(401, "wrong token")
        if cfg.token:
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=90 * 86400)
        return {"ok": True}

    @app.get("/api/state", dependencies=[api])
    def state() -> dict[str, Any]:
        acct = view.account()
        prices = view.prices()
        positions = []
        for p in acct.positions:
            px = prices.get(p.asset)
            positions.append(
                {
                    "asset": p.asset,
                    "direction": p.direction.value,
                    "size": p.size,
                    "entry": p.entry_price,
                    "price": px,
                    "leverage": p.leverage,
                    "margin": p.margin_used,
                    "notional": p.notional,
                    "upnl": p.unrealized_pnl,
                    "roe": p.roe_pct,
                    "liquidation": p.liquidation_price,
                }
            )
        return {
            "network": cfg.network,
            "address": cfg.address,
            "now_ms": int(clock() * 1000),
            "account": {
                "value": acct.account_value,
                "withdrawable": acct.withdrawable,
                "margin_used": acct.total_margin_used,
                "positions": positions,
            },
            "runs": list_runs(cfg.runs_dir, now_s=clock()),
            "agent_key": view.agent_key(),
        }

    @app.get("/api/runs/{name}/equity", dependencies=[api])
    def equity(name: str) -> list[list[float]]:
        d = safe_run(cfg.runs_dir, name)
        return [[p.time_ms, p.account_value] for p in read_equity(d / "equity.jsonl")]

    @app.get("/api/runs/{name}/events", dependencies=[api])
    def events(name: str, limit: int = 100) -> list[dict[str, Any]]:
        d = safe_run(cfg.runs_dir, name)
        evs = EventLog(d / "events.jsonl").read()
        return [to_dict(e) for e in reversed(evs[-max(1, min(limit, 1000)) :])]

    @app.get("/api/runs/{name}/report", dependencies=[api])
    def report(name: str) -> dict[str, Any]:
        d = safe_run(cfg.runs_dir, name)
        evs = EventLog(d / "events.jsonl").read()
        eq = read_equity(d / "equity.jsonl")
        if not eq:
            return {"trades": [], "metrics": None}
        metrics = compute(evs, eq, initial=eq[0].account_value)
        return {
            "metrics": asdict(metrics),
            "trades": [asdict(t) for t in reversed(trades_from_events(evs))],
        }

    @app.post("/api/runs/{name}/stop", dependencies=[api])
    def stop(name: str) -> dict[str, Any]:
        d = safe_run(cfg.runs_dir, name)
        (d / STOP_FILE).touch()
        return {"ok": True, "stop": True}

    @app.post("/api/runs/{name}/clear-stop", dependencies=[api])
    def clear_stop(name: str) -> dict[str, Any]:
        d = safe_run(cfg.runs_dir, name)
        (d / STOP_FILE).unlink(missing_ok=True)
        return {"ok": True, "stop": False}

    @app.get("/api/traders", dependencies=[api])
    def traders_route(refresh: bool = False) -> dict[str, Any]:
        if refresh:
            traders.maybe_refresh(background=False)
        else:
            traders.maybe_refresh()
        return traders.snapshot()

    @app.get("/api/mirror/{address}", dependencies=[api])
    def mirror(address: str, budget: float = 100.0) -> dict[str, Any]:
        addr = address.lower()
        if not addr.startswith("0x") or len(addr) != 42:
            raise HTTPException(400, "bad address")
        feed = feed_factory()
        og = feed.account_state(addr)
        plans = {
            str(int(b)): plan_json(
                simulate_mirror(og, b, feed.all_mids(), max_leverage=cfg.max_leverage)
            )
            for b in sorted({budget, 100.0})
        }
        return {
            "address": addr,
            "equity": og.account_value,
            "positions": len(og.positions),
            "plans": plans,
        }

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/sw.js", include_in_schema=False)
    def sw() -> FileResponse:
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(HTTPException)
    async def _http_error(_r: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def resolve_token(explicit: str | None, settings_token: str) -> str:
    return explicit or os.environ.get(TOKEN_ENV, "") or settings_token
