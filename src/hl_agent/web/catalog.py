"""Strategy packages on disk, as the dashboard lists them.

A package is any directory holding a ``runtime.yaml`` under one of the configured
``strategy_dirs`` (``strategies/``, ``config/strategies/`` and, typically, the Senpi
catalog checkout). Its id is the path relative to that root, so ``tortoise/main`` or
``copy``. When a Senpi ``strategy.yaml`` manifest sits one level up, its ``catalog`` block
(name, emoji, tagline, risk, tier, tags) enriches the card.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hl_agent.strategy.spec import RuntimeSpec, load_runtime_spec, substitute_env

MAX_DEPTH = 3


@dataclass(frozen=True, slots=True)
class StrategyCard:
    id: str
    path: str
    root: str
    name: str
    group: str
    version: str
    description: str
    slots: int
    margin_pct: float | None
    leverage: int
    risk: str
    assets: list[str] = field(default_factory=list)
    scanners: list[dict[str, Any]] = field(default_factory=list)
    catalog: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def find_packages(roots: list[Path] | tuple[Path, ...]) -> list[tuple[str, Path, Path]]:
    """``(id, package_dir, root)`` for every runtime.yaml under the roots, depth-limited."""
    out: list[tuple[str, Path, Path]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("runtime.yaml")):
            d = p.parent
            rel = d.relative_to(root)
            if len(rel.parts) > MAX_DEPTH or d.resolve() in seen:
                continue
            if any(part.startswith(".") or part in ("tests", "__pycache__") for part in rel.parts):
                continue
            seen.add(d.resolve())
            out.append((rel.as_posix(), d, root))
    return out


def read_manifest(package_dir: Path) -> dict[str, Any]:
    """Senpi ``strategy.yaml`` next to or above the package: its ``catalog`` block."""
    for d in (package_dir, package_dir.parent):
        p = d / "strategy.yaml"
        if p.is_file():
            try:
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                return {}
            cat = raw.get("catalog") if isinstance(raw, dict) else None
            return dict(cat) if isinstance(cat, dict) else {}
    return {}


def _assets(spec: RuntimeSpec) -> list[str]:
    names: list[str] = []
    for sc in spec.scanners:
        for key in ("assets", "asset", "symbols"):
            v = sc.inputs.get(key)
            if isinstance(v, str):
                v = [v]
            if isinstance(v, list):
                names += [str(x) for x in v if str(x) not in names]
    return names


def card(id_: str, package_dir: Path, root: Path, env: dict[str, str]) -> StrategyCard:
    manifest = read_manifest(package_dir)
    try:
        spec = load_runtime_spec(package_dir, env)
    except Exception as exc:  # a broken recipe still shows, flagged
        return StrategyCard(
            id=id_,
            path=str(package_dir),
            root=str(root),
            name=manifest.get("name") or package_dir.name,
            group="",
            version="",
            description="",
            slots=0,
            margin_pct=None,
            leverage=0,
            risk="",
            catalog=manifest,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    return StrategyCard(
        id=id_,
        path=str(package_dir),
        root=str(root),
        name=spec.name,
        group=spec.group,
        version=spec.version,
        description=spec.description.strip(),
        slots=spec.strategy.slots,
        margin_pct=spec.strategy.margin_pct,
        leverage=spec.strategy.default_leverage,
        risk=spec.strategy.trading_risk,
        assets=_assets(spec),
        scanners=[
            {
                "name": sc.name,
                "type": sc.type,
                "interval_s": sc.interval_seconds,
                "inputs": {k: v for k, v in sc.inputs.items() if k != "assets"},
            }
            for sc in spec.scanners
        ],
        catalog=manifest,
    )


_CACHE: dict[tuple[str, int], StrategyCard] = {}


def cached_card(id_: str, package_dir: Path, root: Path, env: dict[str, str]) -> StrategyCard:
    """``card`` memoised on the recipe's mtime (the catalog has 100+ packages)."""
    try:
        stamp = (package_dir / "runtime.yaml").stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(package_dir), stamp)
    c = _CACHE.get(key)
    if c is None or c.id != id_:
        c = card(id_, package_dir, root, env)
        _CACHE[key] = c
    return c


def card_json(c: StrategyCard) -> dict[str, Any]:
    return asdict(c)


def runtime_text(package_dir: Path, env: dict[str, str]) -> str:
    return substitute_env((package_dir / "runtime.yaml").read_text(encoding="utf-8"), env)
