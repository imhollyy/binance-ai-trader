"""BT-023: OFFLINE walk-forward probability calibration and leakage audit.

This is an independent review of SELF-REPORTED historical labels and model
probabilities. It neither trains an AI model nor verifies exchange truth,
provenance, holdout immutability or a model binary. It NEVER amends the
constitution's calibrated_model_versions, starts a worker, or authorizes orders.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.market_data import SUPPORTED_SYMBOLS

ZERO = Decimal(0)
ONE = Decimal(1)
THREE = ("WIN", "LOSS", "FLAT")
MIN_SPLIT_OBSERVATIONS = 120
MIN_PER_OUTCOME = 10
MAX_HORIZON_MS = 86_400_000
MAX_ECE = Decimal("0.08")
MIN_BRIER_IMPROVEMENT = Decimal("0.01")


class ObservedOutcome(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    FLAT = "FLAT"


class ValidationStatus(StrEnum):
    INVALID_PROTOCOL = "INVALID_PROTOCOL"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    TEMPORAL_LEAKAGE = "TEMPORAL_LEAKAGE"
    INSUFFICIENT_HOLDOUT = "INSUFFICIENT_HOLDOUT"
    WEAK_CALIBRATION = "WEAK_CALIBRATION"
    NO_PREDICTIVE_GAIN = "NO_PREDICTIVE_GAIN"
    UNVERIFIED_OFFLINE_RESEARCH_ONLY = "UNVERIFIED_OFFLINE_RESEARCH_ONLY"


@dataclass(frozen=True, slots=True)
class HoldoutProtocol:
    """Declared timing; its purported training cutoff is NOT independently proven."""

    model_version: str
    symbol: str
    interval: str
    train_end_ms: int
    validation_start_ms: int
    validation_end_ms: int
    test_start_ms: int
    test_end_ms: int
    outcome_horizon_ms: int
    evaluated_at_ms: int


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    signal_id: str
    model_version: str
    symbol: str
    interval: str
    observed_close_at_ms: int
    label_available_at_ms: int
    forecast: ProbabilityEstimate
    realized_outcome: ObservedOutcome


@dataclass(frozen=True, slots=True)
class SplitMetrics:
    samples: int
    multiclass_brier: Decimal
    multiclass_log_loss: Decimal
    expected_calibration_error: Decimal
    outcome_counts: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class OfflineModelValidation:
    status: ValidationStatus
    model_version: str | None = None
    validation: SplitMetrics | None = None
    test: SplitMetrics | None = None
    test_baseline_brier: Decimal | None = None
    test_brier_improvement: Decimal | None = None

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        """Self-reported/offline data can never promote a model to trusted."""
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _denied(status: ValidationStatus, model_version: str | None = None) -> OfflineModelValidation:
    return OfflineModelValidation(status=status, model_version=model_version)


def _valid_protocol(p: HoldoutProtocol) -> bool:
    if (
        not isinstance(p.model_version, str)
        or not 1 <= len(p.model_version) <= 64
        or not all(c.isascii() and (c.isalnum() or c in "_.-") for c in p.model_version)
        or type(p.symbol) is not str
        or p.symbol not in SUPPORTED_SYMBOLS
        or type(p.interval) is not str
        or p.interval not in ("1m", "5m", "15m")
    ):
        return False
    fields = (
        p.train_end_ms,
        p.validation_start_ms,
        p.validation_end_ms,
        p.test_start_ms,
        p.test_end_ms,
        p.outcome_horizon_ms,
        p.evaluated_at_ms,
    )
    if any(type(value) is not int or value <= 0 for value in fields):
        return False
    h = p.outcome_horizon_ms
    return (
        h <= MAX_HORIZON_MS
        and p.train_end_ms + h <= p.validation_start_ms
        and p.validation_start_ms < p.validation_end_ms
        and p.validation_end_ms + h <= p.test_start_ms
        and p.test_start_ms < p.test_end_ms
        and p.test_end_ms + h <= p.evaluated_at_ms
    )


def _check_split(
    rows: Sequence[ForecastOutcome],
    *,
    p: HoldoutProtocol,
    start_ms: int,
    end_ms: int,
    seen_ids: set[str],
) -> ValidationStatus | None:
    if len(rows) < MIN_SPLIT_OBSERVATIONS:
        return ValidationStatus.INSUFFICIENT_HOLDOUT
    previous_at = start_ms - 1
    for item in rows:
        if not isinstance(item, ForecastOutcome):
            return ValidationStatus.INVALID_OBSERVATION
        if (
            not isinstance(item.signal_id, str)
            or not 8 <= len(item.signal_id) <= 128
            or not item.signal_id.isascii()
            or not all(ch.isalnum() or ch in "_-" for ch in item.signal_id)
            or item.model_version != p.model_version
            or item.symbol != p.symbol
            or item.interval != p.interval
            or not isinstance(item.forecast, ProbabilityEstimate)
            or not isinstance(item.realized_outcome, ObservedOutcome)
            or type(item.observed_close_at_ms) is not int
            or type(item.label_available_at_ms) is not int
        ):
            return ValidationStatus.INVALID_OBSERVATION
        values = (item.forecast.win, item.forecast.loss, item.forecast.flat)
        # Pydantic's model_construct can bypass field validation. Recheck all
        # probability claims before scoring even synthetic records.
        if (
            any(
                type(value) is not Decimal or not value.is_finite() or not ZERO <= value <= ONE
                for value in values
            )
            or sum(values, ZERO) != ONE
        ):
            return ValidationStatus.INVALID_OBSERVATION
        # Closed candle, forward outcome maturity and split embargo. No
        # backfilled labels or duplicate observations may leak across splits.
        if (
            item.signal_id in seen_ids
            or not start_ms <= item.observed_close_at_ms < end_ms
            or item.observed_close_at_ms <= previous_at
            or item.observed_close_at_ms + p.outcome_horizon_ms != item.label_available_at_ms
            or item.label_available_at_ms > p.evaluated_at_ms
            or item.label_available_at_ms > end_ms
        ):
            return ValidationStatus.TEMPORAL_LEAKAGE
        seen_ids.add(item.signal_id)
        previous_at = item.observed_close_at_ms
    return None


def _event_vector(outcome: ObservedOutcome) -> tuple[Decimal, Decimal, Decimal]:
    return (
        ONE if outcome is ObservedOutcome.WIN else ZERO,
        ONE if outcome is ObservedOutcome.LOSS else ZERO,
        ONE if outcome is ObservedOutcome.FLAT else ZERO,
    )


def _forecast_vector(item: ForecastOutcome) -> tuple[Decimal, Decimal, Decimal]:
    return item.forecast.win, item.forecast.loss, item.forecast.flat


def _metrics(items: Sequence[ForecastOutcome]) -> SplitMetrics:
    count = len(items)
    brier = ZERO
    log_loss = ZERO
    bins: dict[int, list[tuple[Decimal, int]]] = {}
    labels = [0, 0, 0]
    for row in items:
        probabilities = _forecast_vector(row)
        truth = _event_vector(row.realized_outcome)
        brier += sum(((x - y) ** 2 for x, y in zip(probabilities, truth, strict=True)), ZERO)
        log_loss -= sum(
            (y * max(x, Decimal("1e-30")).ln() for x, y in zip(probabilities, truth, strict=True)),
            ZERO,
        )
        position = max(range(3), key=probabilities.__getitem__)
        confidence = probabilities[position]
        correct = int(truth[position] == ONE)
        # Top-label calibration buckets. This does not certify per-class
        # calibration and is never used as a production release threshold.
        bucket = min(int(confidence * 10), 9)
        bins.setdefault(bucket, []).append((confidence, correct))
        labels[THREE.index(row.realized_outcome.value)] += 1
    ece = ZERO
    for group in bins.values():
        mean_conf = sum((entry[0] for entry in group), ZERO) / len(group)
        accuracy = Decimal(sum(entry[1] for entry in group)) / len(group)
        ece += Decimal(len(group)) / count * abs(mean_conf - accuracy)
    return SplitMetrics(
        count, brier / count, log_loss / count, ece, (labels[0], labels[1], labels[2])
    )


def _baseline_brier(
    validation: Sequence[ForecastOutcome], test: Sequence[ForecastOutcome]
) -> Decimal:
    n = Decimal(len(validation))
    rates = tuple(
        Decimal(sum(row.realized_outcome is outcome for row in validation)) / n
        for outcome in ObservedOutcome
    )
    return sum(
        (
            sum(
                (
                    (rate - expected) ** 2
                    for rate, expected in zip(
                        rates, _event_vector(row.realized_outcome), strict=True
                    )
                ),
                ZERO,
            )
            for row in test
        ),
        ZERO,
    ) / len(test)


def review_offline_walk_forward(
    protocol: HoldoutProtocol,
    validation: Sequence[ForecastOutcome],
    test: Sequence[ForecastOutcome],
) -> OfflineModelValidation:
    """Score frozen sequential holdouts; never infer trusted calibration.

    A caller can fabricate probabilities, labels or claimed train_end_ms.
    The test split is NOT used to tune thresholds; fixed constants are
    exploratory review thresholds and NOT user risk limits.
    """
    if not isinstance(protocol, HoldoutProtocol) or not _valid_protocol(protocol):
        return _denied(ValidationStatus.INVALID_PROTOCOL)
    if not isinstance(validation, (tuple, list)) or not isinstance(test, (tuple, list)):
        return _denied(ValidationStatus.INVALID_OBSERVATION, protocol.model_version)
    seen: set[str] = set()
    for rows, start_ms, end_ms in (
        (validation, protocol.validation_start_ms, protocol.validation_end_ms),
        (test, protocol.test_start_ms, protocol.test_end_ms),
    ):
        blocked = _check_split(rows, p=protocol, start_ms=start_ms, end_ms=end_ms, seen_ids=seen)
        if blocked is not None:
            return _denied(blocked, protocol.model_version)
    metrics_val, metrics_test = _metrics(validation), _metrics(test)
    if (
        min(metrics_val.outcome_counts) < MIN_PER_OUTCOME
        or min(metrics_test.outcome_counts) < MIN_PER_OUTCOME
    ):
        return OfflineModelValidation(
            ValidationStatus.INSUFFICIENT_HOLDOUT, protocol.model_version, metrics_val, metrics_test
        )
    baseline = _baseline_brier(validation, test)
    improvement = baseline - metrics_test.multiclass_brier
    if (
        metrics_val.expected_calibration_error > MAX_ECE
        or metrics_test.expected_calibration_error > MAX_ECE
    ):
        status = ValidationStatus.WEAK_CALIBRATION
    elif (
        improvement < MIN_BRIER_IMPROVEMENT
        or metrics_test.multiclass_brier > metrics_val.multiclass_brier + Decimal("0.05")
    ):
        status = ValidationStatus.NO_PREDICTIVE_GAIN
    else:
        status = ValidationStatus.UNVERIFIED_OFFLINE_RESEARCH_ONLY
    return OfflineModelValidation(
        status, protocol.model_version, metrics_val, metrics_test, baseline, improvement
    )
