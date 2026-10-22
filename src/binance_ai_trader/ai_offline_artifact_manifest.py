"""BT-023B: reproducible OFFLINE model/data/forecast digest snapshots.

SHA-256 detects unintended changes only against an independently retained
reference digest. An attacker able to replace the reference can still forge
the entire record. No trusted publisher signature, model registry, Binance
labels, validated training run or trading authorization is implied.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.ai_offline_model_review import (
    ForecastOutcome,
    HoldoutProtocol,
    OfflineModelValidation,
    ValidationStatus,
    review_offline_walk_forward,
)

MAX_RECORDS = 10_000
MAX_ARTIFACT_BYTES = 10_000_000
SOURCE = "self_reported_offline_research_v1"
PREFIX = b"tradepilot-ai-research-v1\n"
GOOD_RESEARCH = frozenset(
    {
        ValidationStatus.UNVERIFIED_OFFLINE_RESEARCH_ONLY,
        ValidationStatus.WEAK_CALIBRATION,
        ValidationStatus.NO_PREDICTIVE_GAIN,
    }
)


class OfflineManifestStatus(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    HOLDOUT_REJECTED = "HOLDOUT_REJECTED"
    UNTRUSTED_MANIFEST_CHANGED = "UNTRUSTED_MANIFEST_CHANGED"
    DETACHED_REFERENCE_MISSING = "DETACHED_REFERENCE_MISSING"
    DETACHED_REFERENCE_MISMATCH = "DETACHED_REFERENCE_MISMATCH"
    SELF_REPORTED_RESEARCH_MATCH_ONLY = "SELF_REPORTED_RESEARCH_MATCH_ONLY"


@dataclass(frozen=True, slots=True)
class OfflineResearchManifest:
    schema_version: int
    source: str
    model_version: str
    model_artifact_sha256: str
    training_data_sha256: str
    validation_forecasts_sha256: str
    test_forecasts_sha256: str
    protocol_sha256: str
    validation_count: int
    test_count: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class OfflineManifestReview:
    status: OfflineManifestStatus
    model_review_status: ValidationStatus | None = None
    actual_manifest_sha256: str | None = None

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        return False

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decimal(value: Decimal) -> str:
    # Canonicalize equivalent Decimal spellings, not rounded float formatting.
    return format(value.normalize(), "f")


def _canonical(payload: object) -> bytes:
    return PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _protocol_dict(p: HoldoutProtocol) -> dict[str, str | int]:
    return {
        "model_version": p.model_version,
        "symbol": p.symbol,
        "interval": p.interval,
        "train_end_ms": p.train_end_ms,
        "validation_start_ms": p.validation_start_ms,
        "validation_end_ms": p.validation_end_ms,
        "test_start_ms": p.test_start_ms,
        "test_end_ms": p.test_end_ms,
        "outcome_horizon_ms": p.outcome_horizon_ms,
        "evaluated_at_ms": p.evaluated_at_ms,
    }


def _forecast_dict(f: ForecastOutcome) -> dict[str, object]:
    return {
        "signal_id": f.signal_id,
        "model_version": f.model_version,
        "symbol": f.symbol,
        "interval": f.interval,
        "observed_close_at_ms": f.observed_close_at_ms,
        "label_available_at_ms": f.label_available_at_ms,
        "forecast": {
            "win": _decimal(f.forecast.win),
            "loss": _decimal(f.forecast.loss),
            "flat": _decimal(f.forecast.flat),
        },
        "realized_outcome": f.realized_outcome.value,
    }


def _safe_inputs(
    protocol: HoldoutProtocol,
    validation: Sequence[ForecastOutcome],
    test: Sequence[ForecastOutcome],
    model_artifact: bytes,
    training_data: bytes,
) -> tuple[OfflineModelValidation, bytes] | None:
    if (
        type(model_artifact) is not bytes
        or type(training_data) is not bytes
        or not 1 <= len(model_artifact) <= MAX_ARTIFACT_BYTES
        or not 1 <= len(training_data) <= MAX_ARTIFACT_BYTES
        or type(validation) not in (tuple, list)
        or type(test) not in (tuple, list)
        or len(validation) > MAX_RECORDS
        or len(test) > MAX_RECORDS
    ):
        return None
    result = review_offline_walk_forward(protocol, validation, test)
    if result.status not in GOOD_RESEARCH:
        return None
    try:
        payload = {
            "schema_version": 1,
            "source": SOURCE,
            "model_version": protocol.model_version,
            "model_artifact_sha256": _sha(model_artifact),
            "training_data_sha256": _sha(training_data),
            "validation_forecasts_sha256": _sha(
                _canonical([_forecast_dict(r) for r in validation])
            ),
            "test_forecasts_sha256": _sha(_canonical([_forecast_dict(r) for r in test])),
            "protocol_sha256": _sha(_canonical(_protocol_dict(protocol))),
            "validation_count": len(validation),
            "test_count": len(test),
        }
        return result, _canonical(payload)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def create_offline_research_manifest(
    protocol: HoldoutProtocol,
    validation: Sequence[ForecastOutcome],
    test: Sequence[ForecastOutcome],
    *,
    model_artifact: bytes,
    training_data: bytes,
) -> OfflineResearchManifest:
    """Returns a research fingerprint; NOT a trusted signed model attestation."""
    prepared = _safe_inputs(protocol, validation, test, model_artifact, training_data)
    if prepared is None:
        raise ValueError("unverified_or_invalid_offline_manifest_inputs")
    _, body = prepared
    payload = json.loads(body[len(PREFIX) :])
    return OfflineResearchManifest(
        schema_version=1,
        source=SOURCE,
        model_version=protocol.model_version,
        model_artifact_sha256=payload["model_artifact_sha256"],
        training_data_sha256=payload["training_data_sha256"],
        validation_forecasts_sha256=payload["validation_forecasts_sha256"],
        test_forecasts_sha256=payload["test_forecasts_sha256"],
        protocol_sha256=payload["protocol_sha256"],
        validation_count=payload["validation_count"],
        test_count=payload["test_count"],
        manifest_sha256=_sha(body),
    )


def verify_offline_research_manifest(
    manifest: OfflineResearchManifest,
    protocol: HoldoutProtocol,
    validation: Sequence[ForecastOutcome],
    test: Sequence[ForecastOutcome],
    *,
    model_artifact: bytes,
    training_data: bytes,
    detached_reference_sha256: str | None,
) -> OfflineManifestReview:
    """Check reproducibility and a separately retained checksum; never release.

    The detached reference is supplied by the caller and is not authenticated.
    No checksum can prove a particular exchange label or model training run.
    """
    if type(manifest) is not OfflineResearchManifest:
        return OfflineManifestReview(OfflineManifestStatus.INVALID_INPUT)
    prepared = _safe_inputs(protocol, validation, test, model_artifact, training_data)
    if prepared is None:
        return OfflineManifestReview(OfflineManifestStatus.HOLDOUT_REJECTED)
    model_report, body = prepared
    digest = _sha(body)
    computed = create_offline_research_manifest(
        protocol, validation, test, model_artifact=model_artifact, training_data=training_data
    )
    if manifest != computed:
        return OfflineManifestReview(
            OfflineManifestStatus.UNTRUSTED_MANIFEST_CHANGED,
            model_report.status,
            digest,
        )
    if detached_reference_sha256 is None:
        return OfflineManifestReview(
            OfflineManifestStatus.DETACHED_REFERENCE_MISSING, model_report.status, digest
        )
    if (
        type(detached_reference_sha256) is not str
        or len(detached_reference_sha256) != 64
        or not all(ch in "0123456789abcdef" for ch in detached_reference_sha256)
        or detached_reference_sha256 != digest
    ):
        return OfflineManifestReview(
            OfflineManifestStatus.DETACHED_REFERENCE_MISMATCH, model_report.status, digest
        )
    return OfflineManifestReview(
        OfflineManifestStatus.SELF_REPORTED_RESEARCH_MATCH_ONLY, model_report.status, digest
    )
