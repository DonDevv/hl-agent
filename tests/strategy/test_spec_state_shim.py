from __future__ import annotations

from pathlib import Path

import pytest

from hl_agent.data.models import AccountState
from hl_agent.strategy.mcp_shim import SenpiMcp
from hl_agent.strategy.sources import ReplaySource
from hl_agent.strategy.spec import SpecError, load_runtime_spec, substitute_env
from hl_agent.strategy.state import StateStore, make_state
from tests.strategy.conftest import SENPI, STRATEGIES

# ---- runtime.yaml -----------------------------------------------------------------


def test_env_substitution() -> None:
    env = {"W": "0xabc"}
    assert substitute_env('wallet: "${W}"', env) == 'wallet: "0xabc"'
    assert substitute_env("${MISSING:-dflt} ${MISSING}", env) == "dflt "


def test_native_recipe_maps_to_engine_config() -> None:
    spec = load_runtime_spec(STRATEGIES / "compass", {"HL_WALLET": "0x1"})
    cfg = spec.engine_config()
    assert spec.strategy.wallet == "0x1"
    assert (
        cfg.strategy.slots == 2 and cfg.strategy.margin_pct == 20 and cfg.strategy.max_leverage == 3
    )
    assert cfg.dsl.phase1.max_loss_pct == 12.0 and not cfg.dsl.phase1.trailing_enabled
    assert [t.trigger_pct for t in cfg.dsl.tiers] == [10, 20, 40, 80]
    assert cfg.dsl.hard_timeout.enabled and cfg.dsl.hard_timeout.interval_minutes == 10080
    assert cfg.dsl.weak_peak_cut.min_value == 3.0 and not cfg.dsl.dead_weight_cut.enabled
    assert cfg.rails.max_consecutive_losses == 3 and cfg.rails.cooldown_seconds == 7200
    assert cfg.rails.per_asset_cooldown_seconds == 14400
    sc = spec.external_scanners[0]
    assert sc.name == "compass_signals" and sc.effective_timeout == 120
    assert spec.scanner_dir(sc) == (STRATEGIES / "compass" / "scanners").resolve()


@pytest.mark.skipif(not SENPI.exists(), reason="Senpi catalog not available locally")
def test_every_senpi_catalog_recipe_loads() -> None:
    files = sorted(SENPI.glob("*/*/runtime.yaml"))
    assert len(files) > 100
    failures = []
    for f in files:
        try:
            load_runtime_spec(f, {}).engine_config()
        except Exception as exc:
            failures.append(f"{f.parent.parent.name}/{f.parent.name}: {exc}")
    assert failures == []


@pytest.mark.skipif(not SENPI.exists(), reason="Senpi catalog not available locally")
def test_tortoise_recipe_details() -> None:
    spec = load_runtime_spec(SENPI / "tortoise" / "main", {"TORTOISE_WALLET": "0x2"})
    cfg = spec.engine_config()
    assert cfg.strategy.slots == 3 and cfg.strategy.margin_pct == 8
    assert cfg.dsl.phase1.max_loss_pct == 10.0
    assert [t.trigger_pct for t in cfg.dsl.tiers] == [25, 50, 100, 200]
    assert cfg.rails.daily_loss_limit_pct == 10 and cfg.rails.drawdown_halt_pct == 18


def test_missing_validity_is_a_spec_error(tmp_path: Path) -> None:
    (tmp_path / "runtime.yaml").write_text(
        "name: x\nstrategy: {slots: 1, default_leverage: 2}\n"
        "scanners:\n  - {name: s, type: external_scanner, interval_seconds: 60}\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecError):
        load_runtime_spec(tmp_path)


# ---- ctx.state --------------------------------------------------------------------


def test_state_is_bounded_transactional_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    st = StateStore(3, path)
    for i in range(5):
        st.begin()
        st.append({"i": i})
        st.commit()
    assert len(st) == 3 and st.last() == {"i": 4} and st.recent(2) == [{"i": 3}, {"i": 4}]
    st.begin()
    st.append({"i": 99})
    st.rollback()
    assert st.last() == {"i": 4}
    with pytest.raises(TypeError):
        st.append("nope")  # type: ignore[arg-type]
    reloaded = StateStore(3, path)
    assert list(reloaded) == [{"i": 2}, {"i": 3}, {"i": 4}]
    assert make_state(0) is None


# ---- MCP shim ---------------------------------------------------------------------


def test_shim_routes_reads_blocks_mutations_and_stubs_proprietary(replay: ReplaySource) -> None:
    mcp = SenpiMcp(replay, "0xw")
    md = mcp.call_tool("market_get_asset_data", {"asset": "BTC", "candle_intervals": ["1h", "4h"]})
    assert md["success"] and set(md["data"]["candles"]) == {"1h", "4h"}
    assert len(md["data"]["candles"]["1h"]) == 300 and md["data"]["asset_context"]["coin"] == "BTC"
    bars = md["data"]["candles"]["4h"]
    assert bars[-1]["T"] <= replay.now_ms  # never a bar from the future
    assert {"t", "o", "h", "l", "c", "v"} <= set(bars[-1])

    ch = mcp.call_tool("strategy_get_clearinghouse_state", {"strategy_wallet": "0xw"})
    assert ch["data"]["main"]["marginSummary"]["accountValue"] == 100.0
    lst = mcp.call_tool("market_list_instruments", {})
    assert [r["name"] for r in lst["data"]["instruments"]] == ["BTC", "ETH", "SOL"]
    assert mcp.call_tool("leaderboard_get_markets", {"limit": 10})["data"]["markets"] == []
    with pytest.raises(PermissionError):
        mcp.call_tool("create_position", {})
    with pytest.raises(KeyError):
        mcp.call_tool("market_no_such_tool", {})
    assert mcp.calls == 5


def test_replay_source_respects_clock(replay: ReplaySource) -> None:
    replay.now_ms = 50 * 3_600_000
    assert len(replay.candles("BTC", "1h", 300)) == 50
    assert replay.price("BTC") == replay.candles("BTC", "1h", 1)[-1].close
    assert replay.account() == AccountState(100.0, 100.0, 0.0, ())
