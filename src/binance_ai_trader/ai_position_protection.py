"""BT-015: PAPER-only operator controls and public-price position protection.

No persistence, account writes, orders, stop execution, API route or worker.
Only produces an advisory risk signal. An alert or REDUCE_ONLY_REVIEW is
NEVER proof that a stop-loss, take-profit or emergency exit was executed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict

from binance_ai_trader.ai_constitution import SPOT_SUPPORTED, BotState
from binance_ai_trader.market_data import MarketDataError
from binance_ai_trader.paper_spot import PaperSpotPosition
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservation,
    PublicSpotObservationClient,
    validate_public_observation,
)
from binance_ai_trader.risk_schemas import RiskSettings


class StopBehavior(StrEnum):
    """An explicit operator setting; neither choice executes a trade."""

    MONITOR_ONLY = "MONITOR_ONLY"
    REVIEW_REDUCE_ONLY = "REVIEW_REDUCE_ONLY"


class ProtectionAction(StrEnum):
    NO_POSITION = "NO_POSITION"
    OBSERVATION_BLOCKED = "OBSERVATION_BLOCKED"
    MONITORING = "MONITORING"
    ALERT_STOP_BREACH = "ALERT_STOP_BREACH"
    ALERT_TAKE_PROFIT = "ALERT_TAKE_PROFIT"
    ALERT_EMERGENCY = "ALERT_EMERGENCY"
    ALERT_STOPPED_POSITION_OPEN = "ALERT_STOPPED_POSITION_OPEN"
    REDUCE_ONLY_REVIEW = "REDUCE_ONLY_REVIEW"


@dataclass(frozen=True, slots=True)
class BotControlSnapshot:
    """In-memory versioned control; NOT a durable operator configuration."""

    state: BotState
    revision: int
    settings_version: int
    stop_behavior: StopBehavior
    operator_confirmed_reduce_only: bool


@dataclass(frozen=True, slots=True)
class ProtectionDecision:
    action: ProtectionAction
    reason_code: str
    trade_id: str | None = None
    observed_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    @property
    def would_execute(self) -> bool:
        """Explicit invariant: this component can never execute a trade."""
        return False


class _ProtectionFields(TypedDict):
    trade_id: str
    observed_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None


class BotControlRejected(ValueError):
    """Invalid or stale PAPER operator control transition."""


def change_paper_bot_state(
    settings: RiskSettings,
    current: BotControlSnapshot,
    *,
    expected_revision: int,
    next_state: BotState,
    operator_authorized: bool,
) -> BotControlSnapshot:
    """Fail-closed optimistic state transition, without persistence.

    RUNNING requires an explicit operator action, eligible PAPER settings,
    and a PAUSED predecessor. STOPPED cannot jump directly to RUNNING.
    EMERGENCY_FLATTEN does not itself close positions.
    """

    if (
        not isinstance(settings, RiskSettings)
        or settings.mode != "paper"
        or settings.market_type != "spot"
        or not isinstance(current, BotControlSnapshot)
        or not isinstance(current.state, BotState)
        or not isinstance(current.stop_behavior, StopBehavior)
        or type(current.revision) is not int
        or current.revision < 1
        or type(current.settings_version) is not int
        or current.settings_version < 1
        or type(current.operator_confirmed_reduce_only) is not bool
        or type(expected_revision) is not int
        or expected_revision != current.revision
        or not isinstance(next_state, BotState)
        or type(operator_authorized) is not bool
    ):
        raise BotControlRejected("invalid_or_stale_paper_control")
    if next_state == current.state:
        raise BotControlRejected("no_op_transition_requires_new_revision")
    if next_state is BotState.RUNNING:
        if (
            not operator_authorized
            or current.state is not BotState.PAUSED
            or not settings.bot_enabled
            or settings.manual_approval
            or settings.max_leverage != 1
        ):
            raise BotControlRejected("explicit_paper_start_not_authorized")
    elif next_state is BotState.EMERGENCY_FLATTEN:
        if not operator_authorized:
            raise BotControlRejected("emergency_requires_operator_authorization")
    elif next_state is BotState.PAUSED:
        if current.state not in (BotState.RUNNING, BotState.STOPPED):
            raise BotControlRejected("unsupported_paper_state_transition")
    elif next_state is BotState.STOPPED:
        pass
    else:
        raise BotControlRejected("unsupported_paper_state_transition")
    return replace(current, state=next_state, revision=current.revision + 1)


def _blocked(reason: str, trade_id: str | None = None) -> ProtectionDecision:
    return ProtectionDecision(ProtectionAction.OBSERVATION_BLOCKED, reason, trade_id)


def _positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def evaluate_paper_position_protection(
    settings: RiskSettings,
    control: BotControlSnapshot,
    position: PaperSpotPosition | None,
    sample: PublicSpotObservation | None,
    *,
    settings_version: int,
    now_ms: int,
    take_profit_price: Decimal | None = None,
) -> ProtectionDecision:
    """Read-only monitor: verified freshness before STOP/TP/emergency alerts.

    PublicSpotObservation is an untrusted data record by itself. Only the
    observe_public_paper_position_once wrapper fetches it from the bounded
    server-owned public REST client. Synthetic instances are test-only.
    """
    if (
        not isinstance(settings, RiskSettings)
        or settings.mode != "paper"
        or settings.market_type != "spot"
        or settings.max_leverage != 1
        or not settings.stop_loss_required
        or not isinstance(control, BotControlSnapshot)
        or not isinstance(control.state, BotState)
        or not isinstance(control.stop_behavior, StopBehavior)
        or type(control.operator_confirmed_reduce_only) is not bool
        or type(control.revision) is not int
        or control.revision < 1
        or type(settings_version) is not int
        or settings_version < 1
        or control.settings_version != settings_version
        or type(now_ms) is not int
        or now_ms <= 0
    ):
        return _blocked("PAPER_CONTROL_OR_SETTINGS_MISMATCH")
    if position is None:
        return ProtectionDecision(ProtectionAction.NO_POSITION, "NO_OPEN_POSITION")
    if (
        not isinstance(position, PaperSpotPosition)
        or position.symbol not in SPOT_SUPPORTED
        or position.symbol not in settings.allowed_symbols
        or not _positive(position.quantity)
        or not _positive(position.entry_fill_price)
        or not _positive(position.stop_price)
        or position.stop_price >= position.entry_fill_price
        or not position.trade_id
        or (
            take_profit_price is not None
            and (not _positive(take_profit_price) or take_profit_price <= position.entry_fill_price)
        )
    ):
        return _blocked("INVALID_POSITION_OR_TARGET")
    if sample is None or type(sample) is not PublicSpotObservation:
        return _blocked("PUBLIC_QUOTE_UNAVAILABLE", position.trade_id)
    if sample.symbol != position.symbol or sample.source != "binance_public_spot_ticker_price":
        return _blocked("PUBLIC_QUOTE_SYMBOL_OR_SOURCE_MISMATCH", position.trade_id)
    try:
        validate_public_observation(sample, now_ms=now_ms)
    except (MarketDataError, ValueError, TypeError, AttributeError):
        return _blocked("PUBLIC_QUOTE_STALE_OR_INVALID", position.trade_id)
    price = sample.reference_price
    base: _ProtectionFields = {
        "trade_id": position.trade_id,
        "observed_price": price,
        "stop_price": position.stop_price,
        "take_profit_price": take_profit_price,
    }
    if control.state is BotState.STOPPED:
        # STOPPED does not promise an automated protective exit.
        return ProtectionDecision(
            ProtectionAction.ALERT_STOPPED_POSITION_OPEN,
            "STOPPED_HAS_UNPROTECTED_EXPOSURE",
            **base,
        )
    if control.state is BotState.EMERGENCY_FLATTEN:
        if (
            control.stop_behavior is StopBehavior.REVIEW_REDUCE_ONLY
            and control.operator_confirmed_reduce_only
        ):
            return ProtectionDecision(
                ProtectionAction.REDUCE_ONLY_REVIEW,
                "EMERGENCY_EXIT_REQUIRES_REVIEW",
                **base,
            )
        return ProtectionDecision(
            ProtectionAction.ALERT_EMERGENCY, "EMERGENCY_NOT_EXECUTED", **base
        )
    if price <= position.stop_price:
        if (
            control.stop_behavior is StopBehavior.REVIEW_REDUCE_ONLY
            and control.operator_confirmed_reduce_only
        ):
            return ProtectionDecision(
                ProtectionAction.REDUCE_ONLY_REVIEW, "STOP_EXIT_REQUIRES_REVIEW", **base
            )
        return ProtectionDecision(
            ProtectionAction.ALERT_STOP_BREACH, "STOP_BREACHED_NOT_EXECUTED", **base
        )
    if take_profit_price is not None and price >= take_profit_price:
        return ProtectionDecision(
            ProtectionAction.ALERT_TAKE_PROFIT, "TAKE_PROFIT_NOT_EXECUTED", **base
        )
    return ProtectionDecision(ProtectionAction.MONITORING, "PUBLIC_PAPER_MONITOR_ONLY", **base)


def observe_public_paper_position_once(
    observer: PublicSpotObservationClient,
    settings: RiskSettings,
    control: BotControlSnapshot,
    position: PaperSpotPosition | None,
    *,
    settings_version: int,
    take_profit_price: Decimal | None = None,
) -> ProtectionDecision:
    """One explicit public-REST observation; NEVER scheduled or executed.

    Enforce exact public adapter class; no supplied quote or caller HEALTHY
    assertion. MockTransport is supported by existing client only for tests.
    """
    if type(observer) is not PublicSpotObservationClient:
        return _blocked("TRUSTED_PUBLIC_OBSERVER_REQUIRED")
    if position is None:
        return evaluate_paper_position_protection(
            settings,
            control,
            None,
            None,
            settings_version=settings_version,
            now_ms=observer.now_ms(),
            take_profit_price=take_profit_price,
        )
    if not isinstance(position, PaperSpotPosition) or position.symbol not in SPOT_SUPPORTED:
        return _blocked("INVALID_POSITION_OR_TARGET")
    try:
        sample = observer.observe(symbol=position.symbol, interval="1m", limit=100)
        now_ms = observer.now_ms()
    except (MarketDataError, ValueError, TypeError):
        return _blocked("PUBLIC_QUOTE_UNAVAILABLE", position.trade_id)
    return evaluate_paper_position_protection(
        settings,
        control,
        position,
        sample,
        settings_version=settings_version,
        now_ms=now_ms,
        take_profit_price=take_profit_price,
    )
