"""BT-012 constitution v1: an untrusted AI proposal and read-only risk gate.

AI output is a proposal, NEVER an order. No HTTP, database, exchange
credentials, wallet access, or trading side effects.
This is not wired to the existing default-OFF PAPER worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from binance_ai_trader.paper_risk import (
    PaperRiskRequest,
    PaperRiskState,
    PaperRiskStatus,
    evaluate_paper_risk,
)
from binance_ai_trader.risk_schemas import RiskSettings

HUNDRED = Decimal("100")
ZERO = Decimal("0")
SPOT_SUPPORTED = frozenset({"BTCUSDT", "ETHUSDT"})


class BotState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


class ConstitutionAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class MarketRegime(StrEnum):
    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    RANGE = "RANGE"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    SQUEEZE = "SQUEEZE"
    CHAOTIC = "CHAOTIC"
    UNCERTAIN = "UNCERTAIN"


class ProbabilityEstimate(BaseModel):
    """A calibrated probability estimate, not market truth."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    win: Decimal = Field(ge=0, le=1)
    loss: Decimal = Field(ge=0, le=1)
    flat: Decimal = Field(ge=0, le=1)

    @field_validator("win", "loss", "flat", mode="before")
    @classmethod
    def reject_float_probabilities(cls, value: object) -> object:
        if type(value) is float or type(value) is bool:
            raise ValueError("decimal_string_required")
        return value

    @model_validator(mode="after")
    def exact_probability_sum(self) -> Self:
        if self.win + self.loss + self.flat != Decimal("1"):
            raise ValueError("probability_distribution_not_normalized")
        return self


class AiSpotProposal(BaseModel):
    """Strict, untrusted Spot PAPER JSON with no editable user limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["1.0"]
    signal_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    model_version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    market: Literal["SPOT"]
    mode: Literal["PAPER"]
    symbol: str = Field(min_length=5, max_length=20, pattern=r"^[A-Z0-9]+$")
    execution_timeframe: Literal["1m", "5m", "15m"]
    observed_close_at_ms: int = Field(ge=1)
    created_at_ms: int = Field(ge=1)
    valid_until_ms: int = Field(ge=1)
    decision: ConstitutionAction
    market_regime: MarketRegime
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=12)
    # Uncalibrated estimates must be absent; 0 is not the same as unknown.
    probability: ProbabilityEstimate | None = None
    expected_win_bps: Decimal | None = Field(default=None, ge=0)
    expected_loss_bps: Decimal | None = Field(default=None, ge=0)
    suggested_notional_usdt: Decimal = Field(default=ZERO, ge=0)
    stop_price: Decimal | None = Field(default=None, gt=0)

    @field_validator(
        "expected_win_bps",
        "expected_loss_bps",
        "suggested_notional_usdt",
        "stop_price",
        mode="before",
    )
    @classmethod
    def no_float_money_or_cost(cls, value: object) -> object:
        if type(value) is float or type(value) is bool:
            raise ValueError("decimal_string_required")
        return value

    @field_validator("reason_codes")
    @classmethod
    def bounded_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not code
            or len(code) > 64
            or not code.isascii()
            or not all(ch.isupper() or ch.isdigit() or ch == "_" for ch in code)
            for code in values
        ):
            raise ValueError("invalid_reason_code")
        return values

    @model_validator(mode="after")
    def time_and_estimate_shape(self) -> Self:
        if self.created_at_ms < self.observed_close_at_ms:
            raise ValueError("decision_before_closed_candle")
        if not self.created_at_ms < self.valid_until_ms <= self.created_at_ms + 900_000:
            raise ValueError("decision_expiration_invalid")
        if (self.expected_win_bps is None) != (self.expected_loss_bps is None):
            raise ValueError("incomplete_expected_return")
        return self


@dataclass(frozen=True, slots=True)
class TrustedSpotEvidence:
    """Trusted only when server's verified ingestion populates this record.

    AI text must never create this context. This class by itself is NOT proof
    of source authenticity.
    """

    symbol: str
    interval: str
    last_closed_at_ms: int
    received_at_ms: int
    reference_price: Decimal
    taker_fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    critical_data_ok: bool
    exchange_constraints_ok: bool
    source: str = "verified_public_binance_spot"


@dataclass(frozen=True, slots=True)
class IndependentRiskPolicy:
    """Server-owned user settings. Never parsed from an AI result.

    Thresholds have NO defaults: the owner must configure them separately.
    An eligible proposal is still not permission to place an order.
    """

    settings: RiskSettings
    account_state: PaperRiskState
    bot_state: BotState
    settings_version: int
    account_settings_version: int
    calibrated_model_versions: frozenset[str]
    min_net_edge_bps: Decimal
    min_edge_to_cost_ratio: Decimal
    max_market_age_ms: int


@dataclass(frozen=True, slots=True)
class ConstitutionVerdict:
    signal_id: str
    decision: ConstitutionAction
    reason_code: str
    allowed_notional_usdt: Decimal = ZERO
    expected_net_edge_bps: Decimal | None = None
    round_trip_cost_bps: Decimal | None = None

    @property
    def proposal_eligible(self) -> bool:
        """Eligibility for later independent execution preflight, not an order."""
        return self.reason_code == "PROPOSAL_ELIGIBLE" and self.decision is ConstitutionAction.BUY


def _no_trade(proposal: AiSpotProposal, reason: str) -> ConstitutionVerdict:
    return ConstitutionVerdict(
        signal_id=proposal.signal_id,
        decision=ConstitutionAction.NO_TRADE,
        reason_code=reason,
    )


def _positive_finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def evaluate_constitution_spot_proposal(
    proposal: AiSpotProposal,
    policy: IndependentRiskPolicy,
    evidence: TrustedSpotEvidence | None,
    *,
    now_ms: int,
) -> ConstitutionVerdict:
    """Independent fail-closed gate. No mutation, exchange, scheduler or order.

    Priority: hard limits -> bot -> exchange -> portfolio -> data ->
    cost -> signal -> expected EV. Only SPOT PAPER is implemented.
    """
    if not isinstance(proposal, AiSpotProposal) or not isinstance(policy, IndependentRiskPolicy):
        raise TypeError("typed_independent_gate_arguments_required")
    settings = policy.settings
    if (
        not isinstance(settings, RiskSettings)
        or settings.mode != "paper"
        or settings.market_type != "spot"
        or proposal.market != "SPOT"
        or proposal.mode != "PAPER"
        or proposal.symbol not in SPOT_SUPPORTED
        or proposal.symbol not in settings.allowed_symbols
        or policy.settings_version < 1
        or policy.account_settings_version != policy.settings_version
        or not settings.stop_loss_required
        or settings.max_leverage != 1
        or settings.spot_capital_usdt <= 0
    ):
        return _no_trade(proposal, "USER_HARD_LIMIT")
    if (
        policy.bot_state is not BotState.RUNNING
        or not settings.bot_enabled
        or settings.manual_approval
    ):
        return _no_trade(proposal, "BOT_NOT_RUNNING")
    if evidence is None or not evidence.exchange_constraints_ok:
        return _no_trade(proposal, "EXCHANGE_CONSTRAINT")
    state = policy.account_state
    if (
        not isinstance(state, PaperRiskState)
        or not _positive_finite(state.equity_usdt)
        or not _positive_finite(state.day_start_equity_usdt)
        or not _positive_finite(state.peak_equity_usdt)
        or not isinstance(state.reserved_capital_usdt, Decimal)
        or not state.reserved_capital_usdt.is_finite()
        or state.reserved_capital_usdt < 0
        or type(state.open_positions) is not int
        or state.open_positions < 0
        or state.equity_usdt
        <= state.day_start_equity_usdt * (1 - Decimal(str(settings.daily_loss_limit_pct)) / HUNDRED)
        or state.equity_usdt
        <= state.peak_equity_usdt * (1 - Decimal(str(settings.max_drawdown_pct)) / HUNDRED)
    ):
        return _no_trade(proposal, "PORTFOLIO_RISK")
    if (
        type(now_ms) is not int
        or now_ms <= 0
        or type(evidence.critical_data_ok) is not bool
        or not evidence.critical_data_ok
        or evidence.source != "verified_public_binance_spot"
        or evidence.symbol != proposal.symbol
        or evidence.interval != proposal.execution_timeframe
        or evidence.last_closed_at_ms != proposal.observed_close_at_ms
        or not (
            evidence.last_closed_at_ms
            <= evidence.received_at_ms
            <= proposal.created_at_ms
            <= now_ms
        )
        or now_ms >= proposal.valid_until_ms
        or type(policy.max_market_age_ms) is not int
        or policy.max_market_age_ms <= 0
        or now_ms - evidence.received_at_ms > policy.max_market_age_ms
        or now_ms - evidence.last_closed_at_ms > policy.max_market_age_ms
        or not _positive_finite(evidence.reference_price)
    ):
        return _no_trade(proposal, "DATA_QUALITY_FAILURE")
    if (
        not all(
            isinstance(amount, Decimal) and amount.is_finite() and amount >= 0
            for amount in (evidence.taker_fee_bps, evidence.spread_bps, evidence.slippage_bps)
        )
        or evidence.taker_fee_bps <= 0
        or evidence.slippage_bps <= 0
    ):
        return _no_trade(proposal, "UNVERIFIED_TRANSACTION_COST")
    round_trip_cost = 2 * evidence.taker_fee_bps + evidence.spread_bps + 2 * evidence.slippage_bps
    if proposal.decision is ConstitutionAction.NO_TRADE:
        return _no_trade(proposal, "MODEL_NO_TRADE")
    if proposal.decision is ConstitutionAction.HOLD:
        return ConstitutionVerdict(proposal.signal_id, ConstitutionAction.HOLD, "MODEL_HOLD")
    # Exit, stop behaviour, PAUSED/EMERGENCY_FLATTEN need an independently
    # validated position/exit engine; do not claim that a stop executes.
    if proposal.decision is ConstitutionAction.SELL:
        return _no_trade(proposal, "EXIT_ENGINE_NOT_INTEGRATED")
    if state.open_positions != 0:
        return _no_trade(proposal, "PORTFOLIO_EXPOSURE")
    if proposal.suggested_notional_usdt <= 0 or proposal.stop_price is None:
        return _no_trade(proposal, "INVALID_ENTRY_PROPOSAL")
    if proposal.stop_price >= evidence.reference_price:
        return _no_trade(proposal, "INVALID_STOP_LOSS")
    stop_distance_pct = (
        (evidence.reference_price - proposal.stop_price) * HUNDRED / evidence.reference_price
    )
    risk = evaluate_paper_risk(
        settings,
        PaperRiskRequest(proposal.suggested_notional_usdt, stop_distance_pct),
        state,
    )
    if risk.status is not PaperRiskStatus.ELIGIBLE:
        return _no_trade(proposal, "PORTFOLIO_RISK")
    if (
        proposal.probability is None
        or proposal.expected_win_bps is None
        or proposal.expected_loss_bps is None
        or proposal.model_version not in policy.calibrated_model_versions
    ):
        return _no_trade(proposal, "MODEL_NOT_CALIBRATED")
    if (
        not isinstance(policy.min_net_edge_bps, Decimal)
        or not isinstance(policy.min_edge_to_cost_ratio, Decimal)
        or not policy.min_net_edge_bps.is_finite()
        or not policy.min_edge_to_cost_ratio.is_finite()
        or policy.min_net_edge_bps < 0
        or policy.min_edge_to_cost_ratio <= 1
    ):
        return _no_trade(proposal, "COST_POLICY_UNAVAILABLE")
    expected_gross = (
        proposal.probability.win * proposal.expected_win_bps
        - proposal.probability.loss * proposal.expected_loss_bps
    )
    expected_net = expected_gross - round_trip_cost
    if (
        expected_net <= policy.min_net_edge_bps
        or expected_gross / round_trip_cost < policy.min_edge_to_cost_ratio
    ):
        return _no_trade(proposal, "INSUFFICIENT_EXPECTED_NET_EDGE")
    return ConstitutionVerdict(
        signal_id=proposal.signal_id,
        decision=ConstitutionAction.BUY,
        reason_code="PROPOSAL_ELIGIBLE",
        allowed_notional_usdt=proposal.suggested_notional_usdt,
        expected_net_edge_bps=expected_net,
        round_trip_cost_bps=round_trip_cost,
    )
