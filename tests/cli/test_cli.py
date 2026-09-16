from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hl_agent import cli
from hl_agent.data.binance_client import BinanceClient
from hl_agent.data.history import CandleStore
from hl_agent.data.hyperliquid_client import HyperliquidClient
from hl_agent.execution.live import LiveMarketSource, Network
from hl_agent.execution.runner import STOP_FILE
from tests.data.test_binance import kline_handler
from tests.execution.test_live import ADDR, FakeExchange, FakeInfo, info_handler
from tests.strategy.conftest import INSTRUMENTS, STRATEGIES, H


@pytest.fixture
def settings(tmp_path: Path, store: CandleStore, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A settings file whose cache is the synthetic store and whose runs live in tmp."""
    cache = store.path("BTC", "1h").parent
    cli.save_instruments(cache, INSTRUMENTS)
    p = tmp_path / "settings.toml"
    p.write_text(
        "[network]\nname = 'testnet'\n"
        f"[account]\naddress = '{ADDR}'\n"
        "[risk]\nmax_leverage = 5\n"
        f"[data]\ncache_dir = '{cache.as_posix()}'\n"
        f"runs_dir = '{(tmp_path / 'runs').as_posix()}'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HL_WALLET", raising=False)
    return p


def run_cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Exit code and combined stdout+stderr."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_settings_and_dates(tmp_path: Path, settings: Path) -> None:
    assert cli.Settings.load(None).network is Network.TESTNET  # no file: defaults
    s = cli.Settings.load(settings)
    assert s.address == ADDR and s.max_leverage == 5  # settings may raise the cap...
    (tmp_path / "wild.toml").write_text("[risk]\nmax_leverage = 50\n", encoding="utf-8")
    assert cli.Settings.load(tmp_path / "wild.toml").max_leverage == 10  # ...up to 10x, no more
    (tmp_path / "bad.toml").write_text("[network]\nname = 'moon'\n", encoding="utf-8")
    with pytest.raises(cli.CliError):
        cli.Settings.load(tmp_path / "bad.toml")
    with pytest.raises(cli.CliError):
        cli.Settings.load(tmp_path / "missing.toml")
    assert cli.parse_when("1970-01-02") == 86_400_000 == cli.parse_when("86400000")
    assert cli.fmt_when(86_400_000) == "1970-01-02 00:00"
    with pytest.raises(cli.CliError):
        cli.parse_when("yesterday")
    env = cli.package_env(STRATEGIES / "compass", "0xabc")
    assert env["HL_WALLET"] == "0xabc"


def test_validate_backtest_report_walkforward(
    settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_cli(
        "--settings", str(settings), "validate", str(STRATEGIES / "compass"), capsys=capsys
    )
    assert code == 0 and "compass_signals" in out and "OK" in out

    code, out = run_cli(
        "--settings",
        str(settings),
        "backtest",
        str(STRATEGIES / "pendulum"),
        "--start",
        str(350 * H),
        "--end",
        str(599 * H),
        "--out",
        "bt1",
        capsys=capsys,
    )
    assert code == 0 and "== pendulum-main ==" in out
    runs = cli.Settings.load(settings).runs_dir
    assert (runs / "bt1" / "metrics.json").exists()
    equity = cli.read_equity(runs / "bt1" / "equity.jsonl")
    assert equity[0].time_ms == 350 * H and equity[-1].time_ms == 599 * H
    metrics = json.loads((runs / "bt1" / "metrics.json").read_text())

    code, out = run_cli("--settings", str(settings), "report", "bt1", "--json", capsys=capsys)
    assert code == 0 and f"{metrics['final']:.2f}" in out
    code, _ = run_cli("--settings", str(settings), "report", "nope", capsys=capsys)
    assert code == 2

    code, out = run_cli(
        "--settings",
        str(settings),
        "walkforward",
        str(STRATEGIES / "pendulum"),
        "--start",
        str(350 * H),
        "--end",
        str(599 * H),
        "--folds",
        "2",
        "--out",
        "wf",
        capsys=capsys,
    )
    assert code == 0 and "fold0" in out and "fold1" in out and "profitable folds" in out
    assert (runs / "wf" / "fold1" / "events.jsonl").exists()


def test_fetch_uses_mainnet_history(
    tmp_path: Path,
    settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def fake_client(url: str) -> HyperliquidClient:
        seen.append(url)
        return HyperliquidClient(transport=httpx.MockTransport(info_handler))

    monkeypatch.setattr(cli, "HyperliquidClient", fake_client)
    monkeypatch.setattr(
        cli,
        "BinanceClient",
        lambda: BinanceClient(transport=httpx.MockTransport(kline_handler), sleep=lambda s: None),
    )
    code, out = run_cli(
        "--settings",
        str(settings),
        "fetch",
        "--assets",
        "BTC",
        "--intervals",
        "1h",
        "--since",
        "0",
        capsys=capsys,
    )
    assert code == 0 and seen == [Network.MAINNET.url] and out.startswith("BTC     1h  hl")
    assert "binance" in out  # default --source both: Binance backfilled the older bars
    store = CandleStore(cli.Settings.load(settings).cache_dir)
    assert store.first_open_ms("BTC", "1h") == 0
    code, _ = run_cli("--settings", str(settings), "fetch", "--assets", "DOGE", capsys=capsys)
    assert code == 2


def test_status_stop_and_run(
    settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ex, info = FakeExchange(), FakeInfo()

    def fake_venue(s: cli.Settings, address: str, *, trading: bool) -> cli.Venue:
        client = HyperliquidClient(transport=httpx.MockTransport(info_handler))
        market = LiveMarketSource(client, address)
        return cli.Venue(market, ex if trading else None, info if trading else None)

    monkeypatch.setattr(cli, "build_venue", fake_venue)
    monkeypatch.setattr(cli, "_now_ms", lambda: 1_700_000_000_000)

    code, out = run_cli("--settings", str(settings), "status", capsys=capsys)
    assert code == 0 and "account value 100.00" in out

    code, out = run_cli(
        "--settings",
        str(settings),
        "run",
        str(STRATEGIES / "compass"),
        "--name",
        "live1",
        "--interval",
        "0",
        "--max-ticks",
        "2",
        capsys=capsys,
    )
    runs = cli.Settings.load(settings).runs_dir
    assert code == 0 and "stopped: max_ticks after 2 ticks" in out
    assert len(cli.read_equity(runs / "live1" / "equity.jsonl")) == 2
    assert ex.calls == []  # flat synthetic tape: nothing to trade

    code, out = run_cli("--settings", str(settings), "stop", "live1", capsys=capsys)
    assert code == 0 and (runs / "live1" / STOP_FILE).exists()
    code, out = run_cli("--settings", str(settings), "status", capsys=capsys)
    assert "STOP present" in out
    code, _ = run_cli(
        "--settings",
        str(settings),
        "run",
        str(STRATEGIES / "compass"),
        "--name",
        "live1",
        capsys=capsys,
    )
    assert code == 2  # refuses to start over a STOP file


def test_mainnet_needs_explicit_consent(
    tmp_path: Path, settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = settings.read_text(encoding="utf-8").replace("'testnet'", "'mainnet'")
    (tmp_path / "main.toml").write_text(text, encoding="utf-8")
    code, out = run_cli(
        "--settings",
        str(tmp_path / "main.toml"),
        "run",
        str(STRATEGIES / "compass"),
        capsys=capsys,
    )
    assert code == 2 and "i-accept-real-money" in out
