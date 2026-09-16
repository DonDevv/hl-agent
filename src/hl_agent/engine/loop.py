"""The deterministic runtime step, in Senpi's order:

1. **reconcile** — positions the venue no longer shows are ``closed_externally``;
2. **exits** — every tracked position runs one DSL tick; a close reason closes it,
   otherwise a moved floor updates the exchange stop;
3. **entries** — signals go through expiry → dedup → slots/held → account gates →
   asset gate → sizing → order; every rejection is emitted with its reason code.

``Engine`` owns mutable bookkeeping; all decisions are made by the pure modules around it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hl_agent.data.models import AccountState
from hl_agent.engine.config import DslConfig, GuardRails, StrategyConfig
from hl_agent.engine.dedup import DedupState
from hl_agent.engine.dsl import CloseReason, DslState, roe_pct, stop_price, tick
from hl_agent.engine.guardrails import GateReason, GuardRailState
from hl_agent.engine.ports import Broker, Fill, MarketView, SignalSource
from hl_agent.engine.signals import Signal
from hl_agent.engine.sizing import OrderPlan, plan_order

DEFAULT_DEDUP_WINDOW_MS = 4 * 3_600_000


@dataclass(frozen=True, slots=True)
class Event:
    time_ms: int
    kind: str  # "opened" | "closed" | "rejected" | "stop_moved"
    asset: str
    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tracked:
    signal: Signal
    dsl: DslState
    size: float
    stop_price: float
    fees_usd: float


@dataclass(frozen=True, slots=True)
class EngineConfig:
    strategy: StrategyConfig
    dsl: DslConfig
    rails: GuardRails = field(default_factory=GuardRails)
    dedup_window_ms: int = DEFAULT_DEDUP_WINDOW_MS
    banned_assets: frozenset[str] = frozenset()


class Engine:
    def __init__(
        self,
        cfg: EngineConfig,
        market: MarketView,
        broker: Broker,
        source: SignalSource,
        *,
        now_ms: int,
    ) -> None:
        self._cfg = cfg
        self._market = market
        self._broker = broker
        self._source = source
        self._positions: dict[str, Tracked] = {}
        self._rails = GuardRailState.start(now_ms, market.account().account_value)
        self._dedup = DedupState()

    # ---- read-only views -------------------------------------------------------

    @property
    def positions(self) -> Mapping[str, Tracked]:
        return dict(self._positions)

    @property
    def rails(self) -> GuardRailState:
        return self._rails

    # ---- the step ----------------------------------------------------------------

    def step(self, now_ms: int) -> list[Event]:
        events: list[Event] = []
        account = self._market.account()
        self._rails = self._rails.observe(self._cfg.rails, now_ms, account.account_value)

        self._reconcile(account, now_ms, events)
        self._run_exits(now_ms, events)
        for signal in self._source.signals(now_ms):
            if self._try_enter(signal, account, now_ms, events):
                account = self._market.account()  # margin moved; size the next one honestly
        return events

    def close_all(self, now_ms: int, reason: CloseReason = CloseReason.MANUAL_CLOSE) -> list[Event]:
        """Flatten every tracked position (kill switch, end of a backtest)."""
        events: list[Event] = []
        for asset in list(self._positions):
            fill = self._broker.close(asset, reason, now_ms)
            self._forget(asset, reason, fill.price, fill.fee_usd, now_ms, events)
        return events

    # ---- 1. reconcile ------------------------------------------------------------

    def _reconcile(self, account: AccountState, now_ms: int, events: list[Event]) -> None:
        for asset in list(self._positions):
            venue = account.position(asset)
            if venue is not None and venue.size > 0:
                continue
            known = self._broker.external_close(asset, now_ms)
            if known is not None:
                reason, fill = known
                self._forget(asset, reason, fill.price, fill.fee_usd, fill.time_ms, events)
            else:
                price = self._market.price(asset) or self._positions[asset].dsl.entry_price
                self._forget(asset, CloseReason.CLOSED_EXTERNALLY, price, 0.0, now_ms, events)

    # ---- 2. exits -----------------------------------------------------------------

    def _run_exits(self, now_ms: int, events: list[Event]) -> None:
        for asset, tracked in list(self._positions.items()):
            price = self._market.price(asset)
            if price is None:
                continue
            result = tick(self._cfg.dsl, tracked.dsl, price, now_ms)
            if result.close_reason is not None:
                fill = self._broker.close(asset, result.close_reason, now_ms)
                self._forget(asset, result.close_reason, fill.price, fill.fee_usd, now_ms, events)
                continue
            stop = result.stop_price
            if stop != tracked.stop_price:
                self._broker.set_stop(asset, stop, now_ms)
                events.append(
                    Event(
                        now_ms,
                        "stop_moved",
                        asset,
                        "tier_advanced" if result.tier_advanced else "floor_ratchet",
                        {"stop_price": stop, "roe": result.roe},
                    )
                )
            self._positions[asset] = Tracked(
                tracked.signal, result.state, tracked.size, stop, tracked.fees_usd
            )

    def _forget(
        self,
        asset: str,
        reason: CloseReason,
        exit_price: float,
        exit_fee: float,
        now_ms: int,
        events: list[Event],
    ) -> None:
        tracked = self._positions.pop(asset)
        d = tracked.dsl
        gross = (exit_price - d.entry_price) * d.direction.sign * tracked.size
        pnl = gross - tracked.fees_usd - exit_fee
        self._rails = self._rails.record_close(self._cfg.rails, asset, pnl, now_ms)
        events.append(
            Event(
                now_ms,
                "closed",
                asset,
                reason.value,
                {
                    "signal_id": tracked.signal.signal_id,
                    "scanner": tracked.signal.scanner,
                    "direction": d.direction.value,
                    "entry_price": d.entry_price,
                    "exit_price": exit_price,
                    "size": tracked.size,
                    "leverage": d.leverage,
                    "pnl_usd": pnl,
                    "roe_pct": roe_pct(d.entry_price, exit_price, d.direction, d.leverage),
                    "high_water_roe": d.high_water_roe,
                    "held_minutes": (now_ms - d.opened_at_ms) / 60_000.0,
                },
            )
        )

    # ---- 3. entries ----------------------------------------------------------------

    def _gate(self, signal: Signal, account: AccountState, now_ms: int) -> GateReason | None:
        cfg = self._cfg
        if not signal.is_valid(now_ms):
            return GateReason.SIGNAL_EXPIRED
        if signal.asset in cfg.banned_assets:
            return GateReason.ASSET_BANNED
        if self._dedup.is_duplicate(signal, window_ms=cfg.dedup_window_ms):
            return GateReason.DUPLICATE
        held = signal.asset in self._positions or account.position(signal.asset) is not None
        if held and not cfg.strategy.allow_pyramiding:
            return GateReason.ASSET_HELD
        if len(self._positions) >= cfg.strategy.slots:
            return GateReason.NO_SLOTS
        return self._rails.check_account(
            cfg.rails, now_ms, account.account_value
        ) or self._rails.check_asset(cfg.rails, now_ms, signal.asset)

    def _try_enter(
        self, signal: Signal, account: AccountState, now_ms: int, events: list[Event]
    ) -> bool:
        """Returns True when an order was placed."""
        rejected = self._gate(signal, account, now_ms)
        if rejected is not None:
            events.append(self._rejection(signal, rejected, now_ms))
            return False

        instrument = self._market.instrument(signal.asset)
        price = self._market.price(signal.asset)
        if instrument is None or instrument.delisted:
            events.append(self._rejection(signal, GateReason.ASSET_BANNED, now_ms))
            return False
        if price is None:
            events.append(self._rejection(signal, GateReason.SIGNAL_NOT_READY, now_ms))
            return False

        plan = plan_order(
            self._cfg.strategy,
            signal,
            instrument,
            price=price,
            withdrawable=account.withdrawable,
            free_margin=account.free_margin,
        )
        if isinstance(plan, GateReason):
            events.append(self._rejection(signal, plan, now_ms))
            return False

        self._open(signal, plan, now_ms, events)
        return True

    def _open(self, signal: Signal, plan: OrderPlan, now_ms: int, events: list[Event]) -> None:
        provisional = DslState.open(plan.reference_price, plan.direction, plan.leverage, now_ms)
        fill: Fill = self._broker.open(plan, stop_price(self._cfg.dsl, provisional), now_ms)
        dsl = DslState.open(fill.price, plan.direction, plan.leverage, fill.time_ms)
        stop = stop_price(self._cfg.dsl, dsl)
        if fill.price != plan.reference_price:
            self._broker.set_stop(plan.asset, stop, now_ms)
        self._positions[plan.asset] = Tracked(signal, dsl, fill.size, stop, fill.fee_usd)
        self._rails = self._rails.record_entry()
        self._dedup = self._dedup.remember(signal)
        events.append(
            Event(
                now_ms,
                "opened",
                plan.asset,
                GateReason.SUBMITTED.value,
                {
                    "signal_id": signal.signal_id,
                    "scanner": signal.scanner,
                    "direction": plan.direction.value,
                    "size": fill.size,
                    "entry_price": fill.price,
                    "leverage": plan.leverage,
                    "margin_usd": plan.margin_usd,
                    "notional_usd": plan.notional_usd,
                    "stop_price": stop,
                    "fee_usd": fill.fee_usd,
                    "data": dict(signal.data),
                },
            )
        )

    @staticmethod
    def _rejection(signal: Signal, reason: GateReason, now_ms: int) -> Event:
        return Event(
            now_ms,
            "rejected",
            signal.asset,
            reason.value,
            {
                "signal_id": signal.signal_id,
                "scanner": signal.scanner,
                "direction": signal.direction.value,
            },
        )
