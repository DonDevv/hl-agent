"""Long-running CLI commands launched from the dashboard.

Every job is a child ``hl-agent <cmd> ...`` process with the same settings file and
environment as the web server (so the agent key, if the server has it, is inherited
without ever being read here). Output goes to ``<cache_dir>/jobs/<id>.log``; the registry
keeps the last ``KEEP`` jobs in memory and can kill a running one.

The argv is built from a whitelist per kind: the page never passes raw arguments.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KEEP = 50
KINDS = ("fetch", "validate", "backtest", "walkforward", "run")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ASSET = re.compile(r"^[A-Za-z0-9:._-]{1,32}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class JobError(ValueError):
    pass


@dataclass
class Job:
    id: str
    kind: str
    argv: list[str]
    log_path: Path
    started_ms: int
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    returncode: int | None = None
    ended_ms: int | None = None
    label: str = ""
    run: str = ""  # run name the job writes to (backtest/walkforward/run), if any

    @property
    def running(self) -> bool:
        return self.returncode is None

    def poll(self) -> None:
        if self.proc is not None and self.returncode is None:
            rc = self.proc.poll()
            if rc is not None:
                self.returncode, self.ended_ms = rc, int(time.time() * 1000)

    def tail(self, lines: int = 200) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def to_json(self, *, log_lines: int = 0) -> dict[str, Any]:
        self.poll()
        out = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "run": self.run,
            "argv": self.argv,
            "started_ms": self.started_ms,
            "ended_ms": self.ended_ms,
            "running": self.running,
            "returncode": self.returncode,
        }
        if log_lines:
            out["log"] = self.tail(log_lines)
        return out


def _name(v: Any, what: str) -> str:
    s = str(v or "")
    if not _NAME.match(s):
        raise JobError(f"bad {what}: {s!r}")
    return s


def _num(v: Any, what: str, lo: float, hi: float) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise JobError(f"bad {what}: {v!r}") from exc
    if not lo <= f <= hi:
        raise JobError(f"{what} must be within [{lo}, {hi}]")
    return f"{f:g}"


def _assets(v: Any) -> list[str]:
    items = v if isinstance(v, list) else str(v or "").replace(",", " ").split()
    out = [str(a).strip().upper() for a in items if str(a).strip()]
    for a in out:
        if not _ASSET.match(a):
            raise JobError(f"bad asset {a!r}")
    return out


def _package(v: Any, packages: Callable[[], dict[str, Path]]) -> str:
    pid = str(v or "")
    known = packages()
    if pid not in known:
        raise JobError(f"unknown package {pid!r}")
    return str(known[pid])


def build_argv(
    kind: str, p: dict[str, Any], *, packages: Callable[[], dict[str, Path]], network: str
) -> list[str]:
    """Whitelisted translation of a request body into ``hl-agent`` arguments."""
    if kind not in KINDS:
        raise JobError(f"unknown job kind {kind!r}")
    if kind == "fetch":
        argv = ["fetch"]
        if p.get("assets"):
            argv += ["--assets", *_assets(p["assets"])]
        if p.get("intervals"):
            ivs = [str(i) for i in _assets(p["intervals"])]
            argv += ["--intervals", *[i.lower() for i in ivs]]
        if p.get("since"):
            if not _DATE.match(str(p["since"])):
                raise JobError("bad since date")
            argv += ["--since", str(p["since"])]
        if p.get("source") in ("hyperliquid", "binance", "both"):
            argv += ["--source", str(p["source"])]
        return argv
    package = _package(p.get("package"), packages)
    if kind == "validate":
        argv = ["validate", package]
        if p.get("assets"):
            argv += ["--assets", *_assets(p["assets"])]
        return argv
    if kind in ("backtest", "walkforward"):
        argv = [kind, package]
        if p.get("assets"):
            argv += ["--assets", *_assets(p["assets"])]
        for key in ("start", "end"):
            if p.get(key):
                if not _DATE.match(str(p[key])):
                    raise JobError(f"bad {key} date")
                argv += [f"--{key}", str(p[key])]
        if p.get("cash") is not None:
            argv += ["--cash", _num(p["cash"], "cash", 10, 1e9)]
        if p.get("step_hours") is not None:
            argv += [
                "--step-hours",
                str(int(_num(p["step_hours"], "step_hours", 1, 24).split(".")[0])),
            ]
        if p.get("leverage"):
            argv += ["--leverage", str(int(float(_num(p["leverage"], "leverage", 1, 10))))]
        if kind == "walkforward" and p.get("folds"):
            argv += ["--folds", str(int(float(_num(p["folds"], "folds", 2, 12))))]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        slug = re.sub(r"[^A-Za-z0-9]+", "-", str(p.get("package"))).strip("-")[:40]
        default = f"{'bt' if kind == 'backtest' else 'wf'}-{slug}-{stamp}"
        argv += ["--out", _name(p.get("out") or default, "run name")]
        return argv
    # live run
    argv = ["run", package, "--name", _name(p.get("name") or "live", "run name")]
    if p.get("interval") is not None:
        argv += ["--interval", _num(p["interval"], "interval", 5, 3600)]
    if p.get("copy"):
        addr = str(p["copy"])
        if not _ADDR.match(addr):
            raise JobError("bad trader address")
        argv += ["--copy", addr.lower()]
        if p.get("poll") is not None:
            argv += ["--poll", _num(p["poll"], "poll", 30, 3600)]
        if p.get("budget") is not None:
            argv += ["--budget", _num(p["budget"], "budget", 10, 1e9)]
    if network == "mainnet":
        if not p.get("accept_real_money"):
            raise JobError("mainnet: tick 'I accept real money' to launch")
        argv.append("--i-accept-real-money")
    return argv


def _label(kind: str, p: dict[str, Any]) -> str:
    if kind == "fetch":
        return "fetch " + " ".join(_assets(p.get("assets"))) if p.get("assets") else "fetch"
    return f"{kind} {p.get('package') or ''}".strip()


def _run_name(argv: list[str]) -> str:
    for flag in ("--out", "--name"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return ""


class JobRegistry:
    def __init__(
        self,
        *,
        log_dir: Path,
        cwd: Path,
        settings_path: Path | None,
        packages: Callable[[], dict[str, Path]],
        network: str,
        python: str = sys.executable,
    ) -> None:
        self.log_dir, self.cwd = log_dir, cwd
        self.settings_path = settings_path
        self.packages, self.network, self.python = packages, network, python
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def base_argv(self) -> list[str]:
        argv = [self.python, "-m", "hl_agent.cli"]
        if self.settings_path is not None:
            argv += ["--settings", str(self.settings_path)]
        return argv

    def launch(self, kind: str, params: dict[str, Any]) -> Job:
        tail = build_argv(kind, params, packages=self.packages, network=self.network)
        with self._lock:
            if kind == "run" and any(j.kind == "run" and j.running for j in self._jobs.values()):
                raise JobError("a live run is already running from this dashboard")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        jid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        log_path = self.log_dir / f"{jid}.log"
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        with log_path.open("wb") as fh:
            fh.write(("$ hl-agent " + " ".join(tail) + "\n").encode())
            proc = subprocess.Popen(
                [*self.base_argv(), *tail],
                cwd=self.cwd,
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        job = Job(
            jid,
            kind,
            tail,
            log_path,
            int(time.time() * 1000),
            proc=proc,
            label=_label(kind, params),
            run=_run_name(tail),
        )
        with self._lock:
            self._jobs[jid] = job
            self._trim()
        return job

    def _trim(self) -> None:
        done = [j for j in self._jobs.values() if not j.running]
        for j in done[: max(0, len(self._jobs) - KEEP)]:
            self._jobs.pop(j.id, None)

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def list(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            j.poll()
        return sorted(jobs, key=lambda j: j.started_ms, reverse=True)

    def kill(self, jid: str) -> Job:
        job = self.get(jid)
        if job is None:
            raise JobError(f"no job {jid}")
        job.poll()
        if job.running and job.proc is not None:
            if os.name == "nt":
                job.proc.terminate()
            else:
                job.proc.send_signal(signal.SIGINT)
            try:
                job.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.proc.kill()
            job.poll()
        return job
