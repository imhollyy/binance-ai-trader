"""BT-023: offline time-separated probability scoring with NO execution rights."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

import pytest

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_artifact_manifest import (
    OfflineManifestStatus,
    create_offline_research_manifest,
    verify_offline_research_manifest,
)
from binance_ai_trader.ai_offline_model_review import (
    ForecastOutcome,
    HoldoutProtocol,
    ObservedOutcome,
    ValidationStatus,
    review_offline_walk_forward,
)

D = Decimal
ORIGIN = 1_800_000_000_000
HORIZON = 60_000
VAL_START = ORIGIN + HORIZON
VAL_END = VAL_START + 121 * HORIZON
TEST_START = VAL_END + HORIZON
TEST_END = TEST_START + 121 * HORIZON
EVALUATED = TEST_END + HORIZON


def protocol() -> HoldoutProtocol:
    return HoldoutProtocol(
        "research_model_v1",
        "BTCUSDT",
        "1m",
        ORIGIN,
        VAL_START,
        VAL_END,
        TEST_START,
        TEST_END,
        HORIZON,
        EVALUATED,
    )


def rows(prefix: str, start: int, *, calibrated: bool = True) -> tuple[ForecastOutcome, ...]:
    records: list[ForecastOutcome] = []
    for i in range(120):
        group = i // 60
        within = i % 60
        if group == 0:
            outcome = (
                ObservedOutcome.WIN
                if within < 42
                else ObservedOutcome.LOSS
                if within < 51
                else ObservedOutcome.FLAT
            )
        else:
            outcome = (
                ObservedOutcome.LOSS
                if within < 42
                else ObservedOutcome.WIN
                if within < 51
                else ObservedOutcome.FLAT
            )
        if calibrated:
            probabilities = (
                ProbabilityEstimate(win=D(".70"), loss=D(".15"), flat=D(".15"))
                if group == 0
                else ProbabilityEstimate(win=D(".15"), loss=D(".70"), flat=D(".15"))
            )
        else:
            probabilities = ProbabilityEstimate(win=D(".425"), loss=D(".425"), flat=D(".15"))
        instant = start + i * HORIZON
        records.append(
            ForecastOutcome(
                f"{prefix}_{i:04d}",
                "research_model_v1",
                "BTCUSDT",
                "1m",
                instant,
                instant + HORIZON,
                probabilities,
                outcome,
            )
        )
    return tuple(records)


def dataset() -> tuple[HoldoutProtocol, tuple[ForecastOutcome, ...], tuple[ForecastOutcome, ...]]:
    return protocol(), rows("validation", VAL_START), rows("holdout", TEST_START)


def test_research_metrics_show_brier_gain_but_never_promote_model() -> None:
    spec, validation, test = dataset()
    report = review_offline_walk_forward(spec, validation, test)
    assert report.status is ValidationStatus.UNVERIFIED_OFFLINE_RESEARCH_ONLY
    assert report.model_version == spec.model_version
    assert report.validation is not None
    assert report.test is not None
    assert report.validation.samples == 120
    assert report.test.samples == 120
    assert report.test.outcome_counts == (51, 51, 18)
    assert report.test.expected_calibration_error == D("0")
    assert report.test_baseline_brier is not None
    assert report.test_brier_improvement is not None
    assert report.test_brier_improvement > D(".10")
    assert report.test.multiclass_brier < report.test_baseline_brier
    assert not report.trade_authorized
    assert not report.qualifies_as_calibrated_model
    assert report.no_trade


@pytest.mark.parametrize(
    "mutation",
    [
        {"symbol": "XRPUSDT"},
        {"model_version": ""},
        {"interval": "1h"},
        {"outcome_horizon_ms": 0},
        {"outcome_horizon_ms": 86_400_001},
        {"validation_start_ms": ORIGIN + 1},
        {"validation_end_ms": VAL_START},
        {"test_start_ms": VAL_END},
        {"test_end_ms": TEST_START},
        {"evaluated_at_ms": TEST_END},
    ],
)
def test_invalid_or_overlapping_declared_splits_rejected(mutation: dict[str, object]) -> None:
    spec, val, test = dataset()
    result = review_offline_walk_forward(replace(spec, **cast(Any, mutation)), val, test)
    assert result.status is ValidationStatus.INVALID_PROTOCOL
    assert not result.trade_authorized


def test_duplicate_and_reordered_holdout_signals_are_temporal_leakage() -> None:
    spec, val, test = dataset()
    repeated = replace(test[0], signal_id=val[0].signal_id)
    assert (
        review_offline_walk_forward(spec, val, (repeated, *test[1:])).status
        is ValidationStatus.TEMPORAL_LEAKAGE
    )
    assert (
        review_offline_walk_forward(spec, val, (test[1], test[0], *test[2:])).status
        is ValidationStatus.TEMPORAL_LEAKAGE
    )
    assert (
        review_offline_walk_forward(
            spec, val, (replace(test[0], observed_close_at_ms=VAL_END), *test[1:])
        ).status
        is ValidationStatus.TEMPORAL_LEAKAGE
    )


def test_immature_labels_future_leakage_and_malformed_entries_fail_closed() -> None:
    spec, val, test = dataset()
    assert (
        review_offline_walk_forward(
            spec, val, (replace(test[0], label_available_at_ms=EVALUATED + HORIZON), *test[1:])
        ).status
        is ValidationStatus.TEMPORAL_LEAKAGE
    )
    assert (
        review_offline_walk_forward(
            spec, (replace(val[0], label_available_at_ms=VAL_START), *val[1:]), test
        ).status
        is ValidationStatus.TEMPORAL_LEAKAGE
    )
    assert (
        review_offline_walk_forward(
            spec, val, (replace(test[0], model_version="unseen_model"), *test[1:])
        ).status
        is ValidationStatus.INVALID_OBSERVATION
    )
    assert (
        review_offline_walk_forward(
            spec, val, (replace(test[0], symbol="ETHUSDT"), *test[1:])
        ).status
        is ValidationStatus.INVALID_OBSERVATION
    )
    forged = ProbabilityEstimate.model_construct(win=D("1.2"), loss=D("-.2"), flat=D(0))
    assert (
        review_offline_walk_forward(
            spec, val, (replace(test[0], forecast=forged), *test[1:])
        ).status
        is ValidationStatus.INVALID_OBSERVATION
    )
    assert review_offline_walk_forward(spec, val, test[:50]).status is (
        ValidationStatus.INSUFFICIENT_HOLDOUT
    )


def test_calibration_rejection_and_climatology_comparison() -> None:
    spec, val, test = dataset()
    uniform = rows("baseline", TEST_START, calibrated=False)
    report = review_offline_walk_forward(spec, val, uniform)
    assert report.status is ValidationStatus.NO_PREDICTIVE_GAIN
    assert report.test_brier_improvement is not None
    assert report.test_brier_improvement < D(".01")

    overconfident = tuple(
        replace(
            sample,
            forecast=(
                ProbabilityEstimate(win=D(".99"), loss=D(".005"), flat=D(".005"))
                if index < 60
                else ProbabilityEstimate(win=D(".005"), loss=D(".99"), flat=D(".005"))
            ),
        )
        for index, sample in enumerate(test)
    )
    report = review_offline_walk_forward(spec, val, overconfident)
    assert report.status is ValidationStatus.WEAK_CALIBRATION
    assert report.test is not None
    assert report.test.expected_calibration_error > D(".08")


def test_no_future_matured_outcome_cannot_inflate_sample_size() -> None:
    spec, val, test = dataset()
    sparse = tuple(replace(item, realized_outcome=ObservedOutcome.WIN) for item in val)
    report = review_offline_walk_forward(spec, sparse, test)
    assert report.status is ValidationStatus.INSUFFICIENT_HOLDOUT
    assert not report.qualifies_as_calibrated_model


def test_untyped_collections_and_boolean_timestamp_rejected() -> None:
    spec, val, test = dataset()
    assert (
        review_offline_walk_forward(spec, val, test).status
        is ValidationStatus.UNVERIFIED_OFFLINE_RESEARCH_ONLY
    )
    assert (
        review_offline_walk_forward(replace(spec, test_start_ms=True), val, test).status
        is ValidationStatus.INVALID_PROTOCOL
    )
    assert (
        review_offline_walk_forward(
            spec,
            val,
            [*test[:1], None, *test[2:]],  # type: ignore[list-item]
        ).status
        is ValidationStatus.INVALID_OBSERVATION
    )


def test_offline_manifest_same_records_have_stable_digest_but_no_trading_rights() -> None:
    spec, val, test = dataset()
    manifest = create_offline_research_manifest(
        spec, val, test, model_artifact=b"research-model-bytes", training_data=b"train-inputs"
    )
    repeat = create_offline_research_manifest(
        spec, list(val), list(test),
        model_artifact=b"research-model-bytes",
        training_data=b"train-inputs",
    )
    assert repeat == manifest
    assert manifest.schema_version == 1
    assert len(manifest.manifest_sha256) == 64
    review = verify_offline_research_manifest(
        manifest,
        spec,
        val,
        test,
        model_artifact=b"research-model-bytes",
        training_data=b"train-inputs",
        detached_reference_sha256=manifest.manifest_sha256,
    )
    assert review.status is OfflineManifestStatus.SELF_REPORTED_RESEARCH_MATCH_ONLY
    assert review.model_review_status is ValidationStatus.UNVERIFIED_OFFLINE_RESEARCH_ONLY
    assert review.actual_manifest_sha256 == manifest.manifest_sha256
    assert review.no_trade
    assert not review.trade_authorized
    assert not review.qualifies_as_calibrated_model


def test_offline_manifest_requires_detached_reference_and_rejects_replacement() -> None:
    spec, val, test = dataset()
    source = b"synthetic-saved-model"
    train = b"synthetic-training"
    manifest = create_offline_research_manifest(
        spec, val, test, model_artifact=source, training_data=train
    )

    def check(
        current_test: tuple[ForecastOutcome, ...] = test,
        *,
        model: bytes = source,
        training: bytes = train,
        reference: str | None = manifest.manifest_sha256,
    ) -> OfflineManifestStatus:
        return verify_offline_research_manifest(
            manifest, spec, val, current_test,
            model_artifact=model,
            training_data=training,
            detached_reference_sha256=reference,
        ).status

    assert check(reference=None) is OfflineManifestStatus.DETACHED_REFERENCE_MISSING
    assert check(reference="0" * 64) is OfflineManifestStatus.DETACHED_REFERENCE_MISMATCH
    assert check(reference=manifest.manifest_sha256.upper()) is (
        OfflineManifestStatus.DETACHED_REFERENCE_MISMATCH
    )
    assert check(model=source + b"change") is OfflineManifestStatus.UNTRUSTED_MANIFEST_CHANGED
    assert check(training=train + b"change") is OfflineManifestStatus.UNTRUSTED_MANIFEST_CHANGED
    changed_label = replace(test[0], realized_outcome=ObservedOutcome.LOSS)
    assert check(current_test=(changed_label, *test[1:])) is (
        OfflineManifestStatus.UNTRUSTED_MANIFEST_CHANGED
    )
    assert check(current_test=test[:-1]) is OfflineManifestStatus.HOLDOUT_REJECTED


def test_offline_manifest_rejects_corrupt_model_and_self_claimed_provenance() -> None:
    spec, val, test = dataset()
    manifest = create_offline_research_manifest(
        spec, val, test, model_artifact=b"x", training_data=b"y"
    )
    for bad in (b"", b"x" * 10_000_001):
        with pytest.raises(ValueError, match="unverified_or_invalid"):
            create_offline_research_manifest(
                spec, val, test, model_artifact=bad, training_data=b"y"
            )
    altered = replace(manifest, source="alleged_exchange_signed")
    verdict = verify_offline_research_manifest(
        altered, spec, val, test,
        model_artifact=b"x",
        training_data=b"y",
        detached_reference_sha256=manifest.manifest_sha256,
    )
    assert verdict.status is OfflineManifestStatus.UNTRUSTED_MANIFEST_CHANGED
    assert not verdict.trade_authorized
    weak = tuple(replace(row, realized_outcome=ObservedOutcome.WIN) for row in val)
    with pytest.raises(ValueError, match="unverified_or_invalid"):
        create_offline_research_manifest(
            spec, weak, test, model_artifact=b"x", training_data=b"y"
        )
