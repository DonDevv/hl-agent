"""Human-readable and machine-readable renderings of ``Metrics``."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from hl_agent.telemetry.metrics import Metrics


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _num(v: float, digits: int = 2) -> str:
    return "inf" if math.isinf(v) else f"{v:.{digits}f}"


def to_json(metrics: Metrics) -> str:
    def clean(obj: Any) -> Any:
        if isinstance(obj, float) and math.isinf(obj):
            return None
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        return obj

    return json.dumps(clean(asdict(metrics)), indent=2)


def render_text(m: Metrics, *, title: str = "") -> str:
    dd = m.drawdown
    lines = [
        f"== {title} ==" if title else "== results ==",
        f"equity      {_num(m.initial)} -> {_num(m.final)}  "
        f"({m.return_pct:+.2f}%, net {m.net_pnl:+.2f})",
        f"trades      {m.trades}  wins {m.wins}  losses {m.losses}  win rate {m.win_rate:.1f}%",
        f"profit fac  {_num(m.profit_factor)}   payoff {_num(m.payoff_ratio)}   "
        f"expectancy {m.expectancy:+.3f}/trade",
        f"avg win     {m.avg_win:+.2f}   avg loss {m.avg_loss:+.2f}   "
        f"worst streak {m.max_consecutive_losses}   avg hold {m.avg_held_hours:.1f}h",
        f"max DD      {dd.max_pct:.1f}%  ({_date(dd.peak_ms)} -> {_date(dd.trough_ms)}), "
        f"longest under water {dd.longest_ms / 86_400_000:.0f}d",
        f"sharpe      {m.sharpe_daily:.2f} (daily, annualised)   "
        f"fees {m.fees_paid:.2f}   funding {m.funding_paid:+.2f}",
    ]
    if m.by_reason:
        lines.append("exits       " + ", ".join(f"{k} {v}" for k, v in sorted(m.by_reason.items())))
    if m.by_asset:
        lines.append("by asset    " + ", ".join(f"{k} {v:+.2f}" for k, v in m.by_asset.items()))
    if m.rejections:
        top = sorted(m.rejections.items(), key=lambda kv: -kv[1])[:6]
        lines.append("rejections  " + ", ".join(f"{k} {v}" for k, v in top))
    if m.monthly:
        lines.append("monthly     " + "  ".join(f"{k} {v:+.2f}" for k, v in m.monthly.items()))
    return "\n".join(lines)


def render_comparison(rows: Sequence[tuple[str, Metrics]]) -> str:
    """One line per strategy/fold, aligned, for side-by-side reading."""
    head = f"{'name':<28}{'return':>9}{'trades':>8}{'win%':>7}{'PF':>7}{'maxDD':>8}{'sharpe':>8}"
    out = [head, "-" * len(head)]
    for name, m in rows:
        out.append(
            f"{name[:28]:<28}{m.return_pct:>+8.2f}%{m.trades:>8}{m.win_rate:>6.1f}%"
            f"{_num(m.profit_factor):>7}{m.drawdown.max_pct:>7.1f}%{m.sharpe_daily:>8.2f}"
        )
    return "\n".join(out)
