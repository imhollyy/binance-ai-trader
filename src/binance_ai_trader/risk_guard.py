"""BT-024 versioned, AI-independent, paper-only deterministic risk gate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from binance_ai_trader.ai_constitution import BotState


class RiskReason(StrEnum):
    ALLOW = "ALLOW"
    MISSING_PROFILE = "MISSING_PROFILE"
    STALE_CONFIGURATION = "STALE_CONFIGURATION"
    BOT_NOT_RUNNING = "BOT_NOT_RUNNING"
    LOSS_LIMIT = "LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    LOSS_STREAK = "LOSS_STREAK"
    POSITION_LIMIT = "POSITION_LIMIT"
    CAPITAL_LIMIT = "CAPITAL_LIMIT"
    TRADE_RISK_LIMIT = "TRADE_RISK_LIMIT"
    INVALID_COST_OR_STOPS = "INVALID_COST_OR_STOPS"
    NET_EDGE_UNVERIFIED = "NET_EDGE_UNVERIFIED"
    EMERGENCY_ENGINE_UNAVAILABLE = "EMERGENCY_ENGINE_UNAVAILABLE"
    PROTECTION_UNDEFINED = "PROTECTION_UNDEFINED"


@dataclass(frozen=True, slots=True)
class RiskProfile:
    schema_version: str
    profile_version: int
    risk_level: int
    max_positions: int | None = None
    risk_per_trade_pct: Decimal | None = None
    max_open_capital: Decimal | None = None
    spot_budget: Decimal | None = None
    daily_loss_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_consecutive_losses: int | None = None
    user_hard_limits: tuple[tuple[str, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class RiskRequest:
    settings_version: int
    bot_state: BotState
    proposed_notional: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    reference_price: Decimal
    fee_bps_round_trip: Decimal | None
    spread_bps: Decimal | None
    slippage_bps: Decimal | None
    expected_gross_edge_bps: Decimal | None
    min_net_edge_bps: Decimal | None
    current_open_capital: Decimal
    open_positions: int
    equity: Decimal
    day_start_equity: Decimal
    peak_equity: Decimal
    consecutive_losses: int
    protection_behavior_configured: bool = False
    closure_engine_tested: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: RiskReason
    audit_code: str
    profile_version: int | None
    bot_state: BotState
    allowed_notional: Decimal = Decimal(0)
    net_edge_bps: Decimal | None = None


class RiskAuditRecorder(Protocol):
    def __call__(self, request: RiskRequest, decision: RiskDecision) -> None: ...


def validate_profile(p: RiskProfile) -> bool:
    if (
        type(p) is not RiskProfile
        or p.schema_version != "1.0"
        or p.profile_version < 1
        or type(p.risk_level) is not int
        or not 1 <= p.risk_level <= 5
    ):
        return False
    if any(type(v) is not Decimal or not v.is_finite() or v <= 0 for _, v in p.user_hard_limits):
        return False
    # Critical profile limits intentionally have no implicit defaults.
    vals = (
        p.risk_per_trade_pct,
        p.max_open_capital,
        p.spot_budget,
        p.daily_loss_pct,
        p.max_drawdown_pct,
    )
    if any(value is None for value in vals):
        return False
    assert p.risk_per_trade_pct is not None
    assert p.daily_loss_pct is not None
    assert p.max_drawdown_pct is not None
    return (
        p.max_positions is not None
        and type(p.max_positions) is int
        and p.max_positions > 0
        and p.max_consecutive_losses is not None
        and type(p.max_consecutive_losses) is int
        and p.max_consecutive_losses > 0
        and all(type(v) is Decimal and v.is_finite() and v > 0 for v in vals)
        and all(v is not None for v in vals)
        and p.risk_per_trade_pct <= p.daily_loss_pct <= 100
        and p.max_drawdown_pct <= 100
    )


def _decision(
    ok: bool,
    reason: RiskReason,
    p: RiskProfile | None,
    state: BotState,
    amount: Decimal = Decimal(0),
    edge: Decimal | None = None,
) -> RiskDecision:
    return RiskDecision(
        ok, reason, reason.value, p.profile_version if p else None, state, amount, edge
    )


def evaluate_risk(profile: RiskProfile | None, request: RiskRequest) -> RiskDecision:
    """New risk is denied on any missing, stale, unverified, or unsafe input."""
    if profile is None or not validate_profile(profile):
        return _decision(False, RiskReason.MISSING_PROFILE, profile, request.bot_state)
    assert profile.risk_per_trade_pct is not None
    assert profile.max_open_capital is not None
    assert profile.spot_budget is not None
    assert profile.daily_loss_pct is not None
    assert profile.max_drawdown_pct is not None
    assert profile.max_positions is not None
    assert profile.max_consecutive_losses is not None
    if request.settings_version != profile.profile_version:
        return _decision(False, RiskReason.STALE_CONFIGURATION, profile, request.bot_state)
    # A higher level may change the requested baseline only; user hard caps always win.
    if request.bot_state is not BotState.RUNNING:
        code = (
            RiskReason.EMERGENCY_ENGINE_UNAVAILABLE
            if request.bot_state is BotState.EMERGENCY_FLATTEN and not request.closure_engine_tested
            else RiskReason.PROTECTION_UNDEFINED
            if request.bot_state in (BotState.PAUSED, BotState.STOPPED)
            and not request.protection_behavior_configured
            else RiskReason.BOT_NOT_RUNNING
        )
        return _decision(False, code, profile, request.bot_state)
    q = request
    monetary = (
        q.proposed_notional,
        q.reference_price,
        q.current_open_capital,
        q.equity,
        q.day_start_equity,
        q.peak_equity,
    )
    if any(type(x) is not Decimal or not x.is_finite() or x < 0 for x in monetary) or q.equity <= 0:
        return _decision(False, RiskReason.INVALID_COST_OR_STOPS, profile, q.bot_state)
    if q.equity <= q.day_start_equity * (1 - profile.daily_loss_pct / 100):
        return _decision(False, RiskReason.LOSS_LIMIT, profile, q.bot_state)
    if q.equity <= q.peak_equity * (1 - profile.max_drawdown_pct / 100):
        return _decision(False, RiskReason.DRAWDOWN_LIMIT, profile, q.bot_state)
    if q.consecutive_losses >= profile.max_consecutive_losses:
        return _decision(False, RiskReason.LOSS_STREAK, profile, q.bot_state)
    if q.open_positions >= profile.max_positions or q.proposed_notional <= 0:
        return _decision(False, RiskReason.POSITION_LIMIT, profile, q.bot_state)
    if q.current_open_capital + q.proposed_notional > min(
        profile.max_open_capital, profile.spot_budget, *(v for _, v in profile.user_hard_limits)
    ):
        return _decision(False, RiskReason.CAPITAL_LIMIT, profile, q.bot_state)
    if (
        q.stop_loss_price is None
        or q.take_profit_price is None
        or q.stop_loss_price >= q.reference_price
        or q.take_profit_price <= q.reference_price
    ):
        return _decision(False, RiskReason.INVALID_COST_OR_STOPS, profile, q.bot_state)
    assert q.stop_loss_price is not None
    assert q.take_profit_price is not None
    risk = q.proposed_notional * (q.reference_price - q.stop_loss_price) / q.reference_price
    if risk > q.equity * profile.risk_per_trade_pct / 100:
        return _decision(False, RiskReason.TRADE_RISK_LIMIT, profile, q.bot_state)
    costs = (
        q.fee_bps_round_trip,
        q.spread_bps,
        q.slippage_bps,
        q.expected_gross_edge_bps,
        q.min_net_edge_bps,
    )
    if any(type(x) is not Decimal or not x.is_finite() or x < 0 for x in costs):
        return _decision(False, RiskReason.NET_EDGE_UNVERIFIED, profile, q.bot_state)
    assert q.fee_bps_round_trip is not None
    assert q.spread_bps is not None
    assert q.slippage_bps is not None
    assert q.expected_gross_edge_bps is not None
    assert q.min_net_edge_bps is not None
    net = q.expected_gross_edge_bps - q.fee_bps_round_trip - q.spread_bps - q.slippage_bps
    if net <= q.min_net_edge_bps or q.take_profit_price <= q.reference_price:
        return _decision(False, RiskReason.NET_EDGE_UNVERIFIED, profile, q.bot_state, edge=net)
    return _decision(True, RiskReason.ALLOW, profile, q.bot_state, q.proposed_notional, net)


def evaluate_risk_and_audit(
    profile: RiskProfile | None,
    request: RiskRequest,
    *,
    recorder: RiskAuditRecorder,
) -> RiskDecision:
    """Evaluate and synchronously hand every outcome to independent audit storage."""
    decision = evaluate_risk(profile, request)
    recorder(request, decision)
    return decision
