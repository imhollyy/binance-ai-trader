"""BT-025 preparation chain. Computes paper suitability only; never creates orders."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_trainable_model import ModelAction, ResearchPrediction
from binance_ai_trader.risk_guard import RiskDecision, RiskProfile, RiskRequest, evaluate_risk

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SIGNAL = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_MODEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _valid_probability(value: object) -> bool:
    if value is None:
        return True
    if type(value) is not ProbabilityEstimate:
        return False
    values = (value.win, value.loss, value.flat)
    return all(type(x) is Decimal and x.is_finite() and 0 <= x <= 1 for x in values) and sum(
        values, Decimal(0)
    ) == Decimal(1)


@dataclass(frozen=True, slots=True)
class ShadowEvidence:
    symbol: str
    interval: str
    observed_close_at_ms: int
    received_at_ms: int
    valid_until_ms: int
    reference_price: Decimal
    fee_bps_round_trip: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    data_sha256: str
    cost_sha256: str
    data_verified: bool
    costs_verified: bool


@dataclass(frozen=True, slots=True)
class ShadowPreflightResult:
    status: str
    reason_code: str
    signal_id: str
    risk_decision: RiskDecision | None
    paper_suitability: bool
    order_intent_created: bool = False
    data_sha256: str | None = None
    cost_sha256: str | None = None


def evaluate_shadow_preflight(
    prediction: ResearchPrediction,
    profile: RiskProfile | None,
    request: RiskRequest,
    evidence: ShadowEvidence | None,
    *,
    now_ms: int,
    max_data_age_ms: int,
    audit: Callable[[ResearchPrediction, ShadowPreflightResult], None],
) -> ShadowPreflightResult:
    """Validate typed model output, fresh provenance and costs, then independent RiskGuard."""
    status, reason, verdict = "BLOCKED", "INVALID_MODEL_SCHEMA", None
    if (
        type(prediction) is not ResearchPrediction
        or prediction.schema_version != "1.0"
        or not isinstance(prediction.action, ModelAction)
        or type(request) is not RiskRequest
        or type(prediction.signal_id) is not str
        or _SIGNAL.fullmatch(prediction.signal_id) is None
        or type(prediction.model_version) is not str
        or _MODEL.fullmatch(prediction.model_version) is None
        or prediction.symbol not in ("BTCUSDT", "ETHUSDT")
        or prediction.interval not in ("1m", "5m", "15m")
        or type(prediction.probability_verified) is not bool
        or (
            prediction.artifact_sha256 is not None
            and _HASH.fullmatch(prediction.artifact_sha256) is None
        )
        or (
            prediction.feature_sha256 is not None
            and _HASH.fullmatch(prediction.feature_sha256) is None
        )
        or not _valid_probability(prediction.probability)
        or (prediction.probability_verified and prediction.probability is None)
    ):
        result = ShadowPreflightResult(status, reason, "invalid_signal", None, False)
        audit(prediction, result)
        return result
    if prediction.action in (ModelAction.HOLD, ModelAction.NO_TRADE):
        result = ShadowPreflightResult(status, "MODEL_NO_ENTRY", prediction.signal_id, None, False)
        audit(prediction, result)
        return result
    if prediction.action is ModelAction.SELL:
        result = ShadowPreflightResult(
            status, "SPOT_EXIT_ENGINE_UNAVAILABLE", prediction.signal_id, None, False
        )
        audit(prediction, result)
        return result
    if prediction.action not in (ModelAction.BUY, ModelAction.SELL):
        result = ShadowPreflightResult(
            status, "INVALID_MODEL_ACTION", prediction.signal_id, None, False
        )
        audit(prediction, result)
        return result
    if (
        evidence is None
        or type(evidence) is not ShadowEvidence
        or not evidence.data_verified
        or not evidence.costs_verified
        or type(now_ms) is not int
        or type(max_data_age_ms) is not int
        or type(evidence.observed_close_at_ms) is not int
        or type(evidence.received_at_ms) is not int
        or type(evidence.valid_until_ms) is not int
        or max_data_age_ms <= 0
        or evidence.symbol != prediction.symbol
        or evidence.interval != prediction.interval
        or evidence.observed_close_at_ms != prediction.observed_close_at_ms
        or prediction.observed_close_at_ms is None
        or prediction.outcome_due_at_ms is None
        or not prediction.observed_close_at_ms <= now_ms < prediction.outcome_due_at_ms
        or evidence.reference_price != request.reference_price
        or not evidence.received_at_ms <= now_ms < evidence.valid_until_ms
        or now_ms - evidence.received_at_ms > max_data_age_ms
        or _HASH.fullmatch(evidence.data_sha256) is None
        or _HASH.fullmatch(evidence.cost_sha256) is None
        or type(evidence.reference_price) is not Decimal
        or not evidence.reference_price.is_finite()
        or evidence.reference_price <= 0
        or any(
            type(x) is not Decimal or not x.is_finite() or x < 0
            for x in (evidence.fee_bps_round_trip, evidence.spread_bps, evidence.slippage_bps)
        )
    ):
        result = ShadowPreflightResult(
            status, "DATA_OR_COST_EVIDENCE_INVALID", prediction.signal_id, None, False
        )
        audit(prediction, result)
        return result
    risk_request = replace(
        request,
        fee_bps_round_trip=evidence.fee_bps_round_trip,
        spread_bps=evidence.spread_bps,
        slippage_bps=evidence.slippage_bps,
    )
    verdict = evaluate_risk(profile, risk_request)
    if not prediction.probability_verified:
        reason = "MODEL_CALIBRATION_UNVERIFIED"
    elif not verdict.allowed:
        reason = verdict.audit_code
    else:
        status, reason = "PAPER_SUITABILITY_ONLY", "RISK_GUARD_ALLOWED_REVIEW"
    result = ShadowPreflightResult(
        status,
        reason,
        prediction.signal_id,
        verdict,
        status == "PAPER_SUITABILITY_ONLY",
        False,
        evidence.data_sha256,
        evidence.cost_sha256,
    )
    audit(prediction, result)
    return result
