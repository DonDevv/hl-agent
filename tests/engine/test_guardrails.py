from __future__ import annotations

from hl_agent.engine.config import GuardRails
from hl_agent.engine.guardrails import GateReason, GuardRailState

DAY = 86_400_000
H = 3_600_000


def test_daily_loss_limit_and_reset_on_new_day() -> None:
    cfg = GuardRails(daily_loss_limit_pct=5.0)
    s = GuardRailState.start(0, 100.0)
    s = s.record_close(cfg, "BTC", -4.99, H)
    assert s.check_account(cfg, H, 95.0) is None
    s = s.record_close(cfg, "ETH", -0.01, 2 * H)
    assert s.check_account(cfg, 2 * H, 95.0) is GateReason.RISK_GATE_DAILY_LOSS
    s = s.observe(cfg, DAY + H, 95.0)
    assert s.realized_today == 0.0 and s.day_start_equity == 95.0
    assert s.check_account(cfg, DAY + H, 95.0) is None


def test_max_entries_with_profit_bypass() -> None:
    cfg = GuardRails(max_entries_per_day=2, bypass_max_entries_per_day_on_profit=True)
    s = GuardRailState.start(0, 100.0).record_entry().record_entry()
    assert s.check_account(cfg, H, 100.0) is GateReason.RISK_GATE_MAX_ENTRIES
    s = s.record_close(cfg, "BTC", 1.0, H)
    assert s.check_account(cfg, H, 101.0) is None
    strict = GuardRails(max_entries_per_day=2)
    assert s.check_account(strict, H, 101.0) is GateReason.RISK_GATE_MAX_ENTRIES


def test_consecutive_losses_trigger_cooldown() -> None:
    cfg = GuardRails(max_consecutive_losses=3, cooldown_seconds=600)
    s = GuardRailState.start(0, 100.0)
    s = s.record_close(cfg, "A", -1, H).record_close(cfg, "B", -1, H)
    s = s.record_close(cfg, "C", 1, H)  # a win resets the streak
    assert s.consecutive_losses == 0
    for i in range(3):
        s = s.record_close(cfg, "D", -1, H + i)
    assert s.cooldown_until_ms == H + 2 + 600_000
    assert s.check_account(cfg, H + 3, 95.0) is GateReason.RISK_GATE_COOLDOWN
    assert s.check_account(cfg, s.cooldown_until_ms, 95.0) is None


def test_drawdown_halt_from_peak_and_optional_daily_reset() -> None:
    cfg = GuardRails(drawdown_halt_pct=10.0)
    s = GuardRailState.start(0, 100.0).observe(cfg, H, 120.0)
    assert s.peak_equity == 120.0
    assert s.check_account(cfg, H, 108.1) is None
    assert s.check_account(cfg, H, 108.0) is GateReason.RISK_GATE_MAX_DRAWDOWN
    # next day, no reset → still halted
    s2 = s.observe(cfg, DAY + H, 108.0)
    assert s2.check_account(cfg, DAY + H, 108.0) is GateReason.RISK_GATE_MAX_DRAWDOWN
    reset = GuardRails(drawdown_halt_pct=10.0, drawdown_reset_on_day_rollover=True)
    s3 = s.observe(reset, DAY + H, 108.0)
    assert s3.peak_equity == 108.0 and s3.check_account(reset, DAY + H, 108.0) is None


def test_per_asset_cooldown() -> None:
    cfg = GuardRails(per_asset_cooldown_seconds=300)
    s = GuardRailState.start(0, 100.0).record_close(cfg, "BTC", 1.0, H)
    assert s.check_asset(cfg, H + 299_999, "BTC") is GateReason.RISK_GATE_ASSET_COOLDOWN
    assert s.check_asset(cfg, H + 300_000, "BTC") is None
    assert s.check_asset(cfg, H, "ETH") is None


def test_disabled_rails_never_block() -> None:
    cfg = GuardRails()
    s = GuardRailState.start(0, 100.0)
    for _ in range(50):
        s = s.record_entry().record_close(cfg, "BTC", -5.0, H)
    assert s.check_account(cfg, H, 1.0) is None
    assert s.check_asset(cfg, H, "BTC") is None
