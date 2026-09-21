"""Backtest every Senpi runtime book on the local candle cache and tabulate the survivors.

Each book (``<strategy>/<sleeve>/runtime.yaml`` plus its ``scanners/``) is copied to
``runs/screen/pkg/<name>`` and replayed over the same window with the same fees, so the numbers
are comparable. Books whose scanners need data the backtester cannot provide (leaderboards,
LLM gates, on-chain feeds) emit nothing and show up with zero trades.

    python scripts/screen_senpi.py [--senpi DIR] [--start 2024-03-01] [--end 2026-09-21]
                                   [--workers 3] [--only caribou,tortoise]

Writes ``runs/screen/results.csv`` incrementally, so a killed run keeps what it had.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "screen"
FIELDS = [
    "book",
    "return_pct",
    "trades",
    "win_rate",
    "profit_factor",
    "max_dd_pct",
    "neg_months_pct",
    "top3_share_pct",
    "fees",
    "avg_held_h",
    "seconds",
    "status",
]


def books(senpi: Path, only: set[str]) -> list[tuple[str, Path]]:
    out = []
    for rt in sorted(senpi.glob("*/**/runtime.yaml")):
        rel = rt.relative_to(senpi).parent
        if rel.parts[0] == "tests":
            continue
        name = "-".join(rel.parts)
        if only and rel.parts[0] not in only:
            continue
        if not (rt.parent / "scanners").is_dir():
            continue
        out.append((name, rt.parent))
    return out


def stage(name: str, src: Path) -> Path:
    dst = OUT / "pkg" / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy(src / "runtime.yaml", dst / "runtime.yaml")
    skip = shutil.ignore_patterns("__pycache__")
    shutil.copytree(src / "scanners", dst / "scanners", ignore=skip)
    return dst


def summarise(name: str, run: Path, seconds: float, status: str) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(FIELDS, "")
    row.update(book=name, seconds=round(seconds), status=status)
    mp = run / "metrics.json"
    if not mp.exists():
        return row
    m = json.loads(mp.read_text(encoding="utf-8"))
    monthly = m.get("monthly") or {}
    neg = sum(1 for v in monthly.values() if v < 0)
    top3 = ""
    trades = m.get("trades") or 0
    if trades and (run / "events.jsonl").exists():
        pnls = []
        for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("kind") == "closed":
                pnls.append(float((ev.get("payload") or {}).get("pnl_usd") or 0))
        gross = sum(p for p in pnls if p > 0)
        if gross > 0:
            top3 = round(100 * sum(sorted(pnls, reverse=True)[:3]) / gross, 1)
    def num(x: object, dec: int) -> float | str:  # metrics use null for "undefined"
        return round(float(x), dec) if isinstance(x, (int, float)) else ""

    row.update(
        return_pct=num(m.get("return_pct"), 1),
        trades=trades,
        win_rate=num(m.get("win_rate"), 1),
        profit_factor=num(m.get("profit_factor"), 2),
        max_dd_pct=num((m.get("drawdown") or {}).get("max_pct"), 1),
        neg_months_pct=round(100 * neg / len(monthly), 0) if monthly else "",
        top3_share_pct=top3,
        fees=num(m.get("fees_paid"), 0),
        avg_held_h=num(m.get("avg_held_hours"), 1),
    )
    return row


def run_one(name: str, src: Path, args: argparse.Namespace) -> dict[str, object]:
    pkg = stage(name, src)
    out_name = f"screen/{name}"
    run_dir = REPO / "runs" / out_name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    cmd = [
        sys.executable,
        "-m",
        "hl_agent.cli",
        "backtest",
        str(pkg),
        "--start",
        args.start,
        "--end",
        args.end,
        "--cash",
        "1000",
        "--fee-bps",
        "4.5",
        "--out",
        out_name,
    ]
    t0 = time.time()
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        p = subprocess.run(
            cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=args.timeout
        )
        status = "ok" if p.returncode == 0 else "error"
        (OUT / "logs").mkdir(parents=True, exist_ok=True)
        (OUT / "logs" / f"{name}.log").write_text(p.stdout[-20000:] + p.stderr[-20000:], "utf-8")
    except subprocess.TimeoutExpired:
        status = "timeout"
    return summarise(name, run_dir, time.time() - t0, status)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--senpi", default=os.environ.get("SENPI_STRATEGIES", ""))
    ap.add_argument("--start", default="2024-03-01")
    ap.add_argument("--end", default="2026-09-21")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--only", default="")
    ap.add_argument("--resummarise", action="store_true", help="rebuild the csv from runs on disk")
    args = ap.parse_args()
    senpi = Path(args.senpi)
    only = {s for s in args.only.split(",") if s}
    todo = books(senpi, only)
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "results.csv"
    if args.resummarise:
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            for name, _src in todo:
                run_dir = REPO / "runs" / "screen" / name
                if (run_dir / "metrics.json").exists():
                    w.writerow(summarise(name, run_dir, 0, "ok"))
        return
    done = set()
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as fh:
            done = {r["book"] for r in csv.DictReader(fh)}
    todo = [t for t in todo if t[0] not in done]
    print(f"{len(todo)} books to screen ({len(done)} already done)", flush=True)
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(run_one, n, s, args): n for n, s in todo}
            for f in as_completed(futs):
                row = f.result()
                w.writerow(row)
                fh.flush()
                print(
                    f"{row['book']:<28} {row['status']:<7} ret {row['return_pct']!s:>7} "
                    f"trades {row['trades']!s:>5} PF {row['profit_factor']!s:>5} "
                    f"DD {row['max_dd_pct']!s:>5} neg {row['neg_months_pct']!s:>4}% "
                    f"({row['seconds']}s)",
                    flush=True,
                )


if __name__ == "__main__":
    main()
