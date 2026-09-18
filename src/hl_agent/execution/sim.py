"""Simulated venue: a ``Broker`` + ``MarketView`` over a ``ReplaySource``.

Honest by construction:

* entries and time-based exits fill at the last close, moved *against* us by ``slippage_bps``;
* every fill pays ``taker_fee_bps`` (Hyperliquid base taker tier is 3.5 bps);
* stops are checked **intra-bar** on every bar that closed since the last mark, using the bar's
  low (long) / high (short); a gap through the stop fills at the bar's open, not at the stop;
* funding is charged hourly on notional at ``funding_hourly`` (longs pay when positive);
* a position whose loss reaches its margin is ``liquidated`` (maintenance margin ignored, which
  is slightly generous at low leverage and irrelevant at the 3x cap).

The account it reports is what Hyperliquid would show: ``accountValue = cash + unrealised``,
``withdrawable`` never counts unrealised profit.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hl_agent.data.models import AccountState, Candle, Direction, Instrument, Position
from hl_agent.engine.dsl import CloseReason
from hl_agent.engine.guardrails import Book
from hl_agent.engine.ports import Fill
from hl_agent.engine.sizing import OrderPlan
from hl_agent.strategy.sources import ReplaySource

HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class SimConfig:
    taker_fee_bps: float = 3.5
    slippage_bps: float = 5.0
    funding_hourly: float = 0.0  # fraction of notional per hour, e.g. 0.0000125 = 1.25e-3 %


DEFAULT_SIM = SimConfig()


@dataclass(frozen=True, slots=True)
class SimPosition:
    direction: Direction
    size: float
    entry_price: float
    leverage: int
    margin: float
    stop_price: float
    opened_ms: int
    funded_until_ms: int


class SimBroker:
    def __init__(
        self, source: ReplaySource, cfg: SimConfig = DEFAULT_SIM, initial_cash: float = 100.0
    ) -> None:
        self._src = source
        self._cfg = cfg
        self.cash = initial_cash
        self.fees_paid = 0.0
        self.funding_paid = 0.0
        self._positions: dict[str, SimPosition] = {}
        self._closed: dict[str, tuple[CloseReason, Fill]] = {}
        self._last_mark_ms = source.now_ms

    # ---- helpers ------------------------------------------------------------------

    def _slip(self, price: float, side: int) -> float:
        """``side`` +1 = we buy (price moves up on us), -1 = we sell."""
        return price * (1.0 + side * self._cfg.slippage_bps / 10_000.0)

    def _fee(self, notional: float) -> float:
        fee = notional * self._cfg.taker_fee_bps / 10_000.0
        self.fees_paid += fee
        return fee

    def _settle(self, asset: str, pos: SimPosition, raw_price: float, now_ms: int) -> Fill:
        price = self._slip(raw_price, -pos.direction.sign)
        fee = self._fee(pos.size * price)
        self.cash += (price - pos.entry_price) * pos.direction.sign * pos.size - fee
        del self._positions[asset]
        return Fill(asset, pos.direction, pos.size, price, fee, now_ms)

    def _charge_funding(self, pos: SimPosition, price: float, now_ms: int) -> SimPosition:
        hours = (now_ms - pos.funded_until_ms) // HOUR_MS
        if hours <= 0 or self._cfg.funding_hourly == 0.0:
            return pos
        paid = pos.size * price * self._cfg.funding_hourly * hours * pos.direction.sign
        self.cash -= paid
        self.funding_paid += paid
        return replace(pos, funded_until_ms=pos.funded_until_ms + hours * HOUR_MS)

    @staticmethod
    def _stop_hit(pos: SimPosition, bar: Candle) -> float | None:
        """Price at which the stop filled during ``bar``, or ``None``."""
        if pos.direction is Direction.LONG:
            if bar.open <= pos.stop_price:
                return bar.open
            return pos.stop_price if bar.low <= pos.stop_price else None
        if bar.open >= pos.stop_price:
            return bar.open
        return pos.stop_price if bar.high >= pos.stop_price else None

    def _liquidation_price(self, pos: SimPosition) -> float:
        return pos.entry_price * (1.0 - pos.direction.sign / pos.leverage)

    # ---- clock --------------------------------------------------------------------

    def mark(self, now_ms: int) -> None:
        """Advance to ``now_ms``: replay every bar closed since the last mark against open
        stops, then charge funding. Must run before ``Engine.step`` for the same instant."""
        for asset, pos in list(self._positions.items()):
            bars = [
                b
                for b in self._src.candles(asset, self._src.price_interval, 0)
                if b.open_ms >= pos.opened_ms and self._last_mark_ms < b.close_ms <= now_ms
            ]
            for bar in bars:
                liq = self._liquidation_price(pos)
                liquidates = (liq - pos.stop_price) * pos.direction.sign > 0  # liq inside stop
                trigger = replace(pos, stop_price=liq) if liquidates else pos
                if (px := self._stop_hit(trigger, bar)) is not None:
                    fill = self._settle(asset, pos, px, bar.close_ms)
                    reason = CloseReason.LIQUIDATED if liquidates else CloseReason.EXCHANGE_SL_HIT
                    self._closed[asset] = (reason, fill)
                    break
                pos = self._charge_funding(pos, bar.close, bar.close_ms + 1)
                self._positions[asset] = pos
        self._last_mark_ms = now_ms

    # ---- MarketView ---------------------------------------------------------------

    def price(self, asset: str) -> float | None:
        return self._src.price(asset)

    def instrument(self, asset: str) -> Instrument | None:
        return next((i for i in self._src.instruments() if i.name == asset), None)

    def order_book(self, asset: str) -> Book | None:
        return None  # candles only: the liquidity gate is a live-venue concern

    def account(self) -> AccountState:
        rows: list[Position] = []
        used = upnl = 0.0
        for asset, pos in self._positions.items():
            px = self._src.price(asset) or pos.entry_price
            u = (px - pos.entry_price) * pos.direction.sign * pos.size
            used += pos.margin
            upnl += u
            rows.append(
                Position(
                    asset,
                    pos.direction,
                    pos.size,
                    pos.entry_price,
                    pos.leverage,
                    pos.margin,
                    u,
                    self._liquidation_price(pos),
                    u / pos.margin * 100.0,
                )
            )
        withdrawable = max(0.0, self.cash + min(upnl, 0.0) - used)
        return AccountState(self.cash + upnl, withdrawable, used, tuple(rows))

    # ---- Broker -------------------------------------------------------------------

    def open(self, plan: OrderPlan, stop_price: float, now_ms: int) -> Fill:
        if plan.asset in self._positions:
            raise ValueError(f"{plan.asset}: already open")
        raw = self._src.price(plan.asset)
        if raw is None:
            raise ValueError(f"{plan.asset}: no price at {now_ms}")
        price = self._slip(raw, plan.direction.sign)
        fee = self._fee(plan.size * price)
        self.cash -= fee
        self._positions[plan.asset] = SimPosition(
            plan.direction,
            plan.size,
            price,
            plan.leverage,
            plan.size * price / plan.leverage,
            stop_price,
            now_ms,
            now_ms,
        )
        self._closed.pop(plan.asset, None)
        return Fill(plan.asset, plan.direction, plan.size, price, fee, now_ms)

    def close(self, asset: str, reason: CloseReason, now_ms: int) -> Fill:
        pos = self._positions[asset]
        raw = self._src.price(asset) or pos.entry_price
        return self._settle(asset, pos, raw, now_ms)

    def set_stop(self, asset: str, stop_price: float, now_ms: int) -> None:
        pos = self._positions[asset]
        self._positions[asset] = replace(pos, stop_price=stop_price)

    def external_close(self, asset: str, now_ms: int) -> tuple[CloseReason, Fill] | None:
        return self._closed.pop(asset, None)
