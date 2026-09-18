"""Hyperliquid venue: ``LiveMarketSource`` (reads) and ``HlBroker`` (writes).

Secrets: the agent private key is read from ``HL_AGENT_PRIVATE_KEY`` **only**, by
``load_signer``; it is never logged, stored or passed around as a string. The account whose
positions we manage is ``HL_AGENT_ADDRESS`` (the wallet that approved the agent key).

Every order is a taker IOC; every open position carries a reduce-only stop-market order on
the exchange, so a crashed bot still has its floor. The SDK is behind two small Protocols so
the whole layer is testable with fakes.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from hl_agent.data.hyperliquid_client import (
    INTERVAL_MS,
    MAINNET_URL,
    TESTNET_URL,
    HyperliquidClient,
    split_asset,
)
from hl_agent.data.models import (
    AccountState,
    AssetContext,
    Candle,
    Direction,
    FundingRate,
    Instrument,
)
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.guardrails import Book
from hl_agent.engine.ports import Fill
from hl_agent.engine.sizing import OrderPlan

PRIVATE_KEY_ENV = "HL_AGENT_PRIVATE_KEY"
ADDRESS_ENV = "HL_AGENT_ADDRESS"
_PERP_PRICE_DECIMALS = 6
_SIG_FIGS = 5


class Network(StrEnum):
    TESTNET = "testnet"
    MAINNET = "mainnet"

    @property
    def url(self) -> str:
        return TESTNET_URL if self is Network.TESTNET else MAINNET_URL


class BrokerError(RuntimeError):
    pass


def round_price(price: float, size_decimals: int) -> float:
    """Hyperliquid tick rules: at most 5 significant figures and ``6 - szDecimals`` decimals."""
    sig = float(f"{price:.{_SIG_FIGS}g}")
    return round(sig, max(0, _PERP_PRICE_DECIMALS - size_decimals))


def load_signer(env: Mapping[str, str] | None = None) -> Any:
    """The agent's signing account, built from ``HL_AGENT_PRIVATE_KEY``. Returns an
    ``eth_account`` ``LocalAccount``; the key string itself never leaves this function."""
    env = os.environ if env is None else env
    key = env.get(PRIVATE_KEY_ENV, "")
    if not key:
        raise BrokerError(f"{PRIVATE_KEY_ENV} is not set")
    from eth_account import Account

    return Account.from_key(key)


def load_address(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    address = env.get(ADDRESS_ENV, "")
    if not address.startswith("0x") or len(address) != 42:
        raise BrokerError(f"{ADDRESS_ENV} must be a 0x-prefixed 20-byte address")
    return address


# ---- narrow views of the SDK -------------------------------------------------------


class ExchangeApi(Protocol):
    def update_leverage(self, leverage: int, name: str, is_cross: bool = True) -> Any: ...
    def market_open(
        self, name: str, is_buy: bool, sz: float, px: float | None = None, slippage: float = 0.05
    ) -> Any: ...
    def market_close(
        self, coin: str, sz: float | None = None, px: float | None = None, slippage: float = 0.05
    ) -> Any: ...
    def order(
        self,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: Any,
        reduce_only: bool = False,
    ) -> Any: ...
    def cancel(self, name: str, oid: int) -> Any: ...


class InfoApi(Protocol):
    def user_fills(self, address: str) -> Any: ...
    def open_orders(self, address: str, dex: str = "") -> Any: ...


def _statuses(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise BrokerError(f"exchange refused: {response!r}"[:300])
    data = response.get("response", {}).get("data", {})
    statuses = data.get("statuses", []) if isinstance(data, dict) else []
    for st in statuses:
        if isinstance(st, dict) and "error" in st:
            raise BrokerError(f"order error: {st['error']}")
    return [st for st in statuses if isinstance(st, dict)]


def _filled(response: Any) -> tuple[float, float, int]:
    """``(size, avg_price, oid)`` of a filled IOC, or ``BrokerError``."""
    for st in _statuses(response):
        if "filled" in st:
            f = st["filled"]
            return float(f["totalSz"]), float(f["avgPx"]), int(f["oid"])
    raise BrokerError(f"order did not fill: {response!r}"[:300])


def _resting_oid(response: Any) -> int:
    for st in _statuses(response):
        if "resting" in st:
            return int(st["resting"]["oid"])
    raise BrokerError(f"stop order not resting: {response!r}"[:300])


# ---- reads -------------------------------------------------------------------------


class LiveMarketSource:
    """``MarketSource`` (for scanners) + ``MarketView`` (for the engine) over ``/info``.

    Reads are memoised until ``refresh``; the runner calls it once per tick so every scanner
    and the engine see one consistent snapshot and the API is not hammered.
    """

    def __init__(
        self,
        client: HyperliquidClient,
        address: str,
        *,
        dexs: Sequence[str] = ("",),
        instrument_refresh_ms: int = 3_600_000,
    ) -> None:
        self._client = client
        self._address = address
        self._dexs = tuple(dexs)
        self._instrument_refresh_ms = instrument_refresh_ms
        self._instruments: dict[str, Instrument] = {}
        self._instruments_at_ms = -1
        self._tick: dict[tuple[Any, ...], Any] = {}
        self.now_ms = 0

    def refresh(self, now_ms: int) -> None:
        self.now_ms = now_ms
        self._tick.clear()
        if now_ms - self._instruments_at_ms >= self._instrument_refresh_ms:
            self._instruments = {
                i.name: i for dex in self._dexs for i in self._client.instruments(dex)
            }
            self._instruments_at_ms = now_ms

    def _memo(self, key: tuple[Any, ...], compute: Any) -> Any:
        if key not in self._tick:
            self._tick[key] = compute()
        return self._tick[key]

    # MarketSource
    def candles(self, asset: str, interval: str, limit: int) -> Sequence[Candle]:
        bars: list[Candle] = self._memo(
            ("candles", asset, interval, limit),
            lambda: self._client.candles(
                asset, interval, self.now_ms - INTERVAL_MS[interval] * limit, self.now_ms
            ),
        )
        return [c for c in bars if c.close_ms <= self.now_ms]

    def asset_context(self, asset: str) -> AssetContext | None:
        dex, _ = split_asset(asset)
        ctxs: dict[str, AssetContext] = self._memo(
            ("ctx", dex), lambda: self._client.asset_contexts(dex)
        )
        return ctxs.get(asset)

    def instruments(self, dex: str = "") -> Sequence[Instrument]:
        prefix = f"{dex}:" if dex else ""
        return [
            i
            for i in self._instruments.values()
            if (i.name.startswith(prefix) if dex else ":" not in i.name)
        ]

    def mids(self, dex: str = "") -> Mapping[str, float]:
        out: dict[str, float] = self._memo(("mids", dex), lambda: self._client.all_mids(dex))
        return out

    def account(self) -> AccountState:
        state: AccountState = self._memo(("account",), self._read_account)
        return state

    def _read_account(self) -> AccountState:
        main = self._client.account_state(self._address)
        extra = tuple(
            p
            for dex in self._dexs
            if dex
            for p in self._client.account_state(self._address, dex).positions
        )
        return AccountState(
            main.account_value,
            main.withdrawable,
            main.total_margin_used,
            main.positions + extra,
        )

    def funding_history(self, asset: str, limit: int) -> Sequence[FundingRate]:
        rows: list[FundingRate] = self._memo(
            ("funding", asset, limit),
            lambda: self._client.funding_history(asset, self.now_ms - limit * 3_600_000),
        )
        return rows[-limit:]

    # MarketView
    def price(self, asset: str) -> float | None:
        dex, _ = split_asset(asset)
        return self.mids(dex).get(asset)

    def instrument(self, asset: str) -> Instrument | None:
        return self._instruments.get(asset)

    def order_book(self, asset: str) -> Book | None:
        book: Book = self._memo(("book", asset), lambda: self._client.order_book(asset, depth=50))
        return book


# ---- writes ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveOrder:
    direction: Direction
    size: float
    entry_price: float
    size_decimals: int
    stop_oid: int | None


class HlBroker:
    def __init__(
        self,
        exchange: ExchangeApi,
        info: InfoApi,
        address: str,
        *,
        market: LiveMarketSource,
        slippage: float = 0.01,
        taker_fee_bps: float = 3.5,
    ) -> None:
        self._ex = exchange
        self._info = info
        self._address = address
        self._market = market
        self._slippage = slippage
        self._fee_bps = taker_fee_bps
        self._orders: dict[str, LiveOrder] = {}

    @property
    def orders(self) -> Mapping[str, LiveOrder]:
        return dict(self._orders)

    def _fee(self, size: float, price: float) -> float:
        return size * price * self._fee_bps / 10_000.0

    def _place_stop(self, asset: str, order: LiveOrder, stop_price: float) -> int:
        px = round_price(stop_price, order.size_decimals)
        resp = self._ex.order(
            asset,
            order.direction is Direction.SHORT,  # a stop closes: buy back a short
            order.size,
            px,
            {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        return _resting_oid(resp)

    def _cancel_stop(self, asset: str) -> None:
        order = self._orders.get(asset)
        if order is None or order.stop_oid is None:
            return
        with contextlib.suppress(Exception):  # already filled or cancelled is fine
            self._ex.cancel(asset, order.stop_oid)

    def adopt(self, account: AccountState, stops: Mapping[str, int]) -> None:
        """Track positions that already exist on the venue (restart), with their stop oids."""
        for p in account.positions:
            inst = self._market.instrument(p.asset)
            decimals = inst.size_decimals if inst else 0
            self._orders[p.asset] = LiveOrder(
                p.direction, p.size, p.entry_price, decimals, stops.get(p.asset)
            )

    # Broker
    def open(self, plan: OrderPlan, stop_price: float, now_ms: int) -> Fill:
        inst = self._market.instrument(plan.asset)
        decimals = inst.size_decimals if inst else 0
        self._ex.update_leverage(plan.leverage, plan.asset, is_cross=True)
        is_buy = plan.direction is Direction.LONG
        size, price, _ = _filled(
            self._ex.market_open(plan.asset, is_buy, plan.size, None, self._slippage)
        )
        order = LiveOrder(plan.direction, size, price, decimals, None)
        try:
            oid = self._place_stop(plan.asset, order, stop_price)
        except BrokerError:
            self._ex.market_close(plan.asset, None, None, self._slippage)
            raise
        self._orders[plan.asset] = LiveOrder(plan.direction, size, price, decimals, oid)
        return Fill(plan.asset, plan.direction, size, price, self._fee(size, price), now_ms)

    def close(self, asset: str, reason: CloseReason, now_ms: int) -> Fill:
        order = self._orders.get(asset)
        self._cancel_stop(asset)
        size, price, _ = _filled(self._ex.market_close(asset, None, None, self._slippage))
        self._orders.pop(asset, None)
        direction = order.direction if order else Direction.LONG
        return Fill(asset, direction, size, price, self._fee(size, price), now_ms)

    def set_stop(self, asset: str, stop_price: float, now_ms: int) -> None:
        order = self._orders[asset]
        self._cancel_stop(asset)
        oid = self._place_stop(asset, order, stop_price)
        self._orders[asset] = LiveOrder(
            order.direction, order.size, order.entry_price, order.size_decimals, oid
        )

    def external_close(self, asset: str, now_ms: int) -> tuple[CloseReason, Fill] | None:
        order = self._orders.pop(asset, None)
        if order is None:
            return None
        fills = [
            f
            for f in self._info.user_fills(self._address)
            if isinstance(f, dict) and f.get("coin") == asset
        ]
        by_stop = [
            f for f in fills if order.stop_oid is not None and f.get("oid") == order.stop_oid
        ]
        liquidated = [f for f in fills if "liquidat" in str(f.get("dir", "")).lower()]
        if by_stop:
            reason, rows = CloseReason.EXCHANGE_SL_HIT, by_stop
        elif liquidated:
            reason, rows = CloseReason.LIQUIDATED, liquidated
        else:
            return None
        size = sum(float(f["sz"]) for f in rows)
        price = sum(float(f["px"]) * float(f["sz"]) for f in rows) / size if size else 0.0
        fee = sum(float(f.get("fee", 0.0)) for f in rows)
        time_ms = max(int(f.get("time", now_ms)) for f in rows)
        return reason, Fill(asset, order.direction, size, price, fee, time_ms)
