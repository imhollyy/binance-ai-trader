"""Deterministic PAPER risk preflight. No exchange client or order execution.

Checks whether a proposed *simulated* position fits the stored risk limits.
The calculated stop loss is an estimate, not a guaranteed maximum loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.risk_schemas import RiskSettings

HUNDRED = Decimal("100")
MAX_STOP_DISTANCE_PCT = Decimal("20")


class PaperRiskStatus(StrEnum):
    REJECTED = "rejected"
    APPROVAL_REQUIRED = "approval_required"
    ELIGIBLE = "eligible"


@dataclass(frozen=True, slots=True)
class PaperRiskRequest:
    proposed_notional_usdt: Decimal
    stop_distance_pct: Decimal
    leverage: int = 1


@dataclass(frozen=True, slots=True)
class PaperRiskState:
    """Already reserved USDT capital means notional for Spot, margin for Futures."""

    equity_usdt: Decimal
    day_start_equity_usdt: Decimal
    peak_equity_usdt: Decimal
    reserved_capital_usdt: Decimal
    open_positions: int


@dataclass(frozen=True, slots=True)
class PaperRiskDecision:
    status: PaperRiskStatus
    reason: str
    estimated_stop_loss_usdt: Decimal = Decimal("0")
    max_notional_usdt: Decimal = Decimal("0")


def _pct(value: float) -> Decimal:
    return Decimal(str(value)) / HUNDRED


def _reject(reason: str) -> PaperRiskDecision:
    return PaperRiskDecision(status=PaperRiskStatus.REJECTED, reason=reason)


def evaluate_paper_risk(
    settings: RiskSettings,
    request: PaperRiskRequest,
    state: PaperRiskState,
) -> PaperRiskDecision:
    """Apply fixed fail-closed rules to a paper-only hypothetical position.

    This performs no trade, price lookup, network request, balance change,
    portfolio reconciliation or stop order submission.
    """

    if settings.mode != "paper":
        return _reject("non_paper_mode")
    if not settings.bot_enabled:
        return _reject("paper_bot_disabled")
    if (
        not request.proposed_notional_usdt.is_finite()
        or request.proposed_notional_usdt <= 0
        or not request.stop_distance_pct.is_finite()
        or not 0 < request.stop_distance_pct <= MAX_STOP_DISTANCE_PCT
    ):
        return _reject("invalid_stop_or_notional")
    if (
        type(request.leverage) is not int
        or request.leverage < 1
        or request.leverage > settings.max_leverage
        or (settings.market_type == "spot" and request.leverage != 1)
    ):
        return _reject("leverage_out_of_bounds")

    monetary_state = (
        state.equity_usdt,
        state.day_start_equity_usdt,
        state.peak_equity_usdt,
        state.reserved_capital_usdt,
    )
    if (
        any(not number.is_finite() for number in monetary_state)
        or state.equity_usdt <= 0
        or state.day_start_equity_usdt <= 0
        or state.peak_equity_usdt <= 0
        or state.reserved_capital_usdt < 0
        or type(state.open_positions) is not int
        or state.open_positions < 0
    ):
        return _reject("invalid_paper_account_state")

    if state.equity_usdt <= state.day_start_equity_usdt * (
        1 - _pct(settings.daily_loss_limit_pct)
    ):
        return _reject("daily_loss_limit")
    if state.equity_usdt <= state.peak_equity_usdt * (
        1 - _pct(settings.max_drawdown_pct)
    ):
        return _reject("maximum_drawdown_limit")
    if state.open_positions >= settings.max_open_positions:
        return _reject("maximum_open_positions")

    allocated = Decimal(
        settings.spot_capital_usdt
        if settings.market_type == "spot"
        else settings.futures_capital_usdt
    )
    if allocated <= 0 or state.reserved_capital_usdt >= allocated:
        return _reject("paper_capital_unavailable")

    available = allocated - state.reserved_capital_usdt
    leverage = Decimal(request.leverage)
    estimated_loss = request.proposed_notional_usdt * request.stop_distance_pct / HUNDRED
    risk_budget = allocated * _pct(settings.risk_per_trade_pct)
    max_by_risk = risk_budget * HUNDRED / request.stop_distance_pct
    max_by_capital = available * leverage
    max_notional = min(max_by_risk, max_by_capital)

    if request.proposed_notional_usdt / leverage > available:
        return PaperRiskDecision(
            status=PaperRiskStatus.REJECTED,
            reason="paper_capital_limit",
            estimated_stop_loss_usdt=estimated_loss,
            max_notional_usdt=max_notional,
        )
    if estimated_loss > risk_budget:
        return PaperRiskDecision(
            status=PaperRiskStatus.REJECTED,
            reason="estimated_trade_risk_limit",
            estimated_stop_loss_usdt=estimated_loss,
            max_notional_usdt=max_notional,
        )
    status = (
        PaperRiskStatus.APPROVAL_REQUIRED
        if settings.manual_approval
        else PaperRiskStatus.ELIGIBLE
    )
    return PaperRiskDecision(
        status=status,
        reason="preflight_only_no_execution",
        estimated_stop_loss_usdt=estimated_loss,
        max_notional_usdt=max_notional,
    )
