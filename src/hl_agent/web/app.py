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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
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
from hl_agent.web.catalog import cached_card, card_json, find_packages, runtime_text
from hl_agent.web.jobs import JobError, JobRegistry

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
    strategy_dirs: tuple[Path, ...] = ()
    settings_path: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)  # shown read-only on the page
    env: dict[str, str] = field(default_factory=dict)  # ${VAR} substitution for recipes


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


def run_kind(run: Path, meta: Mapping[str, Any]) -> str:
    if meta.get("kind") in ("live", "backtest", "walkforward"):
        return str(meta["kind"])
    if (run / "metrics.json").exists():
        return "walkforward" if run.name.startswith("fold") else "backtest"
    return "live"


def read_metrics(run: Path) -> dict[str, Any]:
    p = run / "metrics.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _parse_point(line: str) -> EquityPoint | None:
    try:
        raw = json.loads(line)
        return EquityPoint(int(raw["time_ms"]), float(raw["account_value"]))
    except (ValueError, KeyError, TypeError):
        return None


def equity_bounds(path: Path) -> tuple[EquityPoint | None, EquityPoint | None, int]:
    """First point, last point and line count without parsing the whole file (the tail is
    read by seeking; a large live file stays cheap to summarise)."""
    if not path.exists():
        return None, None, 0
    size = path.stat().st_size
    if size == 0:
        return None, None, 0
    with path.open("rb") as fh:
        first_line = fh.readline().decode("utf-8", "replace")
        fh.seek(max(0, size - 4096))
        tail = fh.read().decode("utf-8", "replace").splitlines()
    first = _parse_point(first_line)
    last = next((pt for ln in reversed(tail) if ln.strip() and (pt := _parse_point(ln))), None)
    # a rough count: the average line length over the sampled tail
    sample = [ln for ln in tail if ln.strip()]
    avg = (sum(len(ln) + 1 for ln in sample) / len(sample)) if sample else 0
    return first, last, round(size / avg) if avg else 0


_SUMMARY_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


def run_summary(run: Path, *, now_s: float, name: str | None = None) -> dict[str, Any]:
    eq = run / "equity.jsonl"
    stat = eq.stat() if eq.exists() else None
    key = (str(run), int(stat.st_mtime_ns) if stat else 0, stat.st_size if stat else 0)
    cached = _SUMMARY_CACHE.get(key)
    if cached is None:
        cached = _summarise(run)
        if len(_SUMMARY_CACHE) > 5000:
            _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE[key] = cached
    out = dict(cached)
    out["name"] = name or run.name
    last_ms = out["last_tick_ms"]
    age_s = now_s - last_ms / 1000 if last_ms else None
    out["alive"] = (
        out["kind"] == "live"
        and age_s is not None
        and age_s < max(LIVE_AFTER_S, 3 * float(out["interval_s"] or 0))
    )
    out["stop"] = (run / STOP_FILE).exists()
    return out


def _equity_return(first: EquityPoint | None, last: EquityPoint | None) -> float | None:
    """Fallback when there is no metrics.json (live runs): straight from the curve."""
    if first is None or last is None or not first.account_value:
        return None
    return (last.account_value / first.account_value - 1) * 100


def _summarise(run: Path) -> dict[str, Any]:
    first, last, ticks = equity_bounds(run / "equity.jsonl")
    meta = read_meta(run)
    m = read_metrics(run)
    kind = run_kind(run, meta)
    return {
        "name": run.name,
        "kind": kind,
        "strategy": meta.get("strategy"),
        "window": meta.get("window"),
        "package": meta.get("package"),
        "copy": meta.get("copy"),
        "network": meta.get("network"),
        "started_ms": meta.get("started_ms"),
        "interval_s": meta.get("interval_s"),
        "ticks": ticks,
        "last_tick_ms": last.time_ms if last else None,
        "initial": first.account_value if first else None,
        "value": last.account_value if last else None,
        "first_ms": first.time_ms if first else None,
        "mtime_ms": int(run.stat().st_mtime * 1000),
        "trades": m.get("trades"),
        "return_pct": m.get("return_pct", _equity_return(first, last)),
        "max_dd_pct": (m.get("drawdown") or {}).get("max_pct") if m else None,
        "win_rate": m.get("win_rate"),
        "profit_factor": m.get("profit_factor"),
    }


def is_run(d: Path) -> bool:
    return d.is_dir() and ((d / "equity.jsonl").exists() or (d / "events.jsonl").exists())


def walk_runs(runs_dir: Path, *, max_depth: int = 3) -> list[tuple[str, Path]]:
    """``(name, dir)`` for every run dir, nested ones as ``parent/child``."""
    out: list[tuple[str, Path]] = []
    if not runs_dir.is_dir():
        return out

    def rec(d: Path, rel: str, depth: int) -> None:
        for c in sorted(d.iterdir()):
            if not c.is_dir() or c.name.startswith(".") or c.name.startswith("_"):
                continue
            name = f"{rel}/{c.name}" if rel else c.name
            if is_run(c):
                out.append((name, c))
            elif depth < max_depth:
                rec(c, name, depth + 1)

    rec(runs_dir, "", 1)
    return out


def list_runs(runs_dir: Path, *, now_s: float) -> list[dict[str, Any]]:
    """Every run, folds grouped under their walk-forward parent."""
    flat = [run_summary(d, now_s=now_s, name=name) for name, d in walk_runs(runs_dir)]
    groups: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for r in flat:
        if r["kind"] == "walkforward" and "/" in r["name"]:
            groups.setdefault(r["name"].rsplit("/", 1)[0], []).append(r)
        else:
            out.append(r)
    for parent, folds in groups.items():
        folds.sort(key=lambda f: f["name"])
        rets = [f["return_pct"] for f in folds if f["return_pct"] is not None]
        compounded = 1.0
        for x in rets:
            compounded *= 1 + x / 100
        out.append(
            {
                "name": parent,
                "kind": "walkforward",
                "strategy": folds[0]["strategy"],
                "package": folds[0]["package"],
                "folds": folds,
                "profitable_folds": sum(1 for x in rets if x > 0),
                "return_pct": (compounded - 1) * 100 if rets else None,
                "max_dd_pct": max((f["max_dd_pct"] or 0) for f in folds),
                "trades": sum(f["trades"] or 0 for f in folds),
                "first_ms": min((f["first_ms"] or 0) for f in folds) or None,
                "last_tick_ms": max((f["last_tick_ms"] or 0) for f in folds) or None,
                "mtime_ms": max(f["mtime_ms"] for f in folds),
                "alive": False,
                "stop": False,
            }
        )
    order = {"live": 2, "walkforward": 1, "backtest": 0}
    out.sort(key=lambda r: (order[r["kind"]], r["mtime_ms"]), reverse=True)
    return out


def safe_run(runs_dir: Path, name: str) -> Path:
    parts = name.split("/") if name else []
    if not parts or any(not p or p in (".", "..") or "\\" in p or p.startswith(".") for p in parts):
        raise HTTPException(400, "bad run name")
    d = runs_dir.joinpath(*parts)
    if not d.is_dir():
        raise HTTPException(404, f"no run named {name}")
    return d


def cache_summary(cache_dir: Path) -> dict[str, Any]:
    """Assets x intervals in the Parquet cache with their windows; cheap (row-group stats)."""
    series: list[dict[str, Any]] = []
    if cache_dir.is_dir():
        for p in sorted(cache_dir.glob("*_*.parquet")):
            stem, _, interval = p.stem.rpartition("_")
            try:
                lf = pl.scan_parquet(p).select(
                    pl.col("open_ms").min().alias("lo"),
                    pl.col("open_ms").max().alias("hi"),
                    pl.len().alias("n"),
                )
                row = lf.collect().row(0)
            except Exception:  # unreadable file: still listed
                row = (None, None, 0)
            series.append(
                {
                    "asset": stem.replace("_", ":"),
                    "interval": interval,
                    "first_ms": row[0],
                    "last_ms": row[1],
                    "bars": row[2],
                    "bytes": p.stat().st_size,
                }
            )
    inst = cache_dir / "instruments.json"
    n_inst = 0
    if inst.exists():
        try:
            n_inst = len(json.loads(inst.read_text(encoding="utf-8")))
        except ValueError:
            n_inst = 0
    return {"series": series, "instruments": n_inst, "cache_dir": str(cache_dir)}


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
    jobs: JobRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="hl-agent", docs_url=None, redoc_url=None)
    traders = traders or TradersCache(
        feed_factory, cfg.cache_dir, max_leverage=cfg.max_leverage, clock=clock
    )

    def packages() -> dict[str, Path]:
        return {pid: d for pid, d, _root in find_packages(cfg.strategy_dirs)}

    jobs = jobs or JobRegistry(
        log_dir=cfg.cache_dir / "jobs",
        cwd=Path.cwd(),
        settings_path=cfg.settings_path,
        packages=packages,
        network=cfg.network,
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

    @app.get("/api/runs/{name:path}/equity", dependencies=[api])
    def equity(name: str) -> list[list[float]]:
        d = safe_run(cfg.runs_dir, name)
        return [[p.time_ms, p.account_value] for p in read_equity(d / "equity.jsonl")]

    @app.get("/api/runs/{name:path}/events", dependencies=[api])
    def events(name: str, limit: int = 100) -> list[dict[str, Any]]:
        d = safe_run(cfg.runs_dir, name)
        evs = EventLog(d / "events.jsonl").read()
        return [to_dict(e) for e in reversed(evs[-max(1, min(limit, 1000)) :])]

    @app.get("/api/runs/{name:path}/report", dependencies=[api])
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

    @app.post("/api/runs/{name:path}/stop", dependencies=[api])
    def stop(name: str) -> dict[str, Any]:
        d = safe_run(cfg.runs_dir, name)
        (d / STOP_FILE).touch()
        return {"ok": True, "stop": True}

    @app.post("/api/runs/{name:path}/clear-stop", dependencies=[api])
    def clear_stop(name: str) -> dict[str, Any]:
        d = safe_run(cfg.runs_dir, name)
        (d / STOP_FILE).unlink(missing_ok=True)
        return {"ok": True, "stop": False}

    # ---- strategies ----------------------------------------------------------------

    @app.get("/api/strategies", dependencies=[api])
    def strategies() -> list[dict[str, Any]]:
        runs = list_runs(cfg.runs_dir, now_s=clock())
        by_pkg: dict[str, int] = {}
        for r in runs:
            key = str(r.get("package") or "")
            if key:
                by_pkg[key] = by_pkg.get(key, 0) + 1
        out = []
        for pid, d, root in find_packages(cfg.strategy_dirs):
            c = card_json(cached_card(pid, d, root, cfg.env))
            c["runs"] = by_pkg.get(str(d), 0) + by_pkg.get(d.as_posix(), 0)
            out.append(c)
        return out

    @app.get("/api/strategies/{pid:path}", dependencies=[api])
    def strategy(pid: str) -> dict[str, Any]:
        known = {p: (d, r) for p, d, r in find_packages(cfg.strategy_dirs)}
        if pid not in known:
            raise HTTPException(404, f"no package {pid}")
        d, root = known[pid]
        out = card_json(cached_card(pid, d, root, cfg.env))
        out["runtime_yaml"] = runtime_text(d, cfg.env)
        keys = {str(d), d.as_posix()}
        out["run_list"] = [
            r for r in list_runs(cfg.runs_dir, now_s=clock()) if str(r.get("package")) in keys
        ]
        return out

    # ---- jobs ----------------------------------------------------------------------

    @app.get("/api/jobs", dependencies=[api])
    def jobs_list() -> list[dict[str, Any]]:
        return [j.to_json() for j in jobs.list()]

    @app.post("/api/jobs", dependencies=[api])
    def jobs_launch(body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind", ""))
        if kind == "run" and not cfg.token:
            raise HTTPException(403, "set a web token before launching live runs from the page")
        try:
            job = jobs.launch(kind, dict(body.get("params") or {}))
        except JobError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.to_json()

    @app.get("/api/jobs/{jid}", dependencies=[api])
    def job_get(jid: str, lines: int = 200) -> dict[str, Any]:
        job = jobs.get(jid)
        if job is None:
            raise HTTPException(404, f"no job {jid}")
        return job.to_json(log_lines=max(1, min(lines, 2000)))

    @app.post("/api/jobs/{jid}/kill", dependencies=[api])
    def job_kill(jid: str) -> dict[str, Any]:
        try:
            return jobs.kill(jid).to_json()
        except JobError as exc:
            raise HTTPException(404, str(exc)) from exc

    # ---- data cache + settings -----------------------------------------------------

    @app.get("/api/data", dependencies=[api])
    def data() -> dict[str, Any]:
        return cache_summary(cfg.cache_dir)

    @app.get("/api/settings", dependencies=[api])
    def settings() -> dict[str, Any]:
        return {
            "network": cfg.network,
            "address": cfg.address,
            "max_leverage": cfg.max_leverage,
            "runs_dir": str(cfg.runs_dir),
            "cache_dir": str(cfg.cache_dir),
            "strategy_dirs": [str(d) for d in cfg.strategy_dirs],
            "settings_path": str(cfg.settings_path) if cfg.settings_path else None,
            "auth": bool(cfg.token),
            **cfg.settings,
        }

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
        # Always revalidate the shell so a new ``?v=`` asset stamp reaches phones.
        return FileResponse(STATIC_DIR / "index.html", headers={"cache-control": "no-cache"})

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
