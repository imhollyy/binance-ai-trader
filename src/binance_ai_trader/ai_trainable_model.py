"""BT-023G deterministic, trainable, research-only OHLCV model.

The model has no database, exchange, account, or order dependencies. The
caller supplies a recorder separately; all reported probabilities remain
uncalibrated until prospectively evaluated.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from binance_ai_trader.ai_constitution import MarketRegime, ProbabilityEstimate
from binance_ai_trader.ai_market_regime import TimeframeHistory, analyze_spot_timeframes
from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle

SCHEMA_VERSION = "1.0"
FEATURES = tuple(
    f"{tf}_{feature}"
    for tf in ("1m", "5m", "15m")
    for feature in ("trend", "volume", "volatility", "momentum", "regime")
)
CLASSES = ("SELL", "HOLD", "BUY")


class ModelAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class TrainingRow:
    close_at_ms: int
    features: tuple[float, ...]
    label: str
    outcome_available_at_ms: int


@dataclass(frozen=True, slots=True)
class TrainResult:
    model_version: str
    artifact_json: str
    artifact_sha256: str
    manifest_json: str
    train_end_ms: int
    test_start_ms: int
    train_rows: int
    test_rows: int


@dataclass(frozen=True, slots=True)
class ResearchPrediction:
    action: ModelAction
    signal_id: str
    model_version: str
    schema_version: str
    symbol: str
    interval: str
    observed_close_at_ms: int | None
    outcome_due_at_ms: int | None
    probability: ProbabilityEstimate | None
    probability_verified: bool
    reason_code: str
    artifact_sha256: str | None = None
    feature_sha256: str | None = None


class ReceiptRecorder(Protocol):
    def __call__(self, prediction: ResearchPrediction) -> None: ...


def _valid_history(history: TimeframeHistory, symbol: str, interval: str, now_ms: int) -> bool:
    rows = history.candles
    period = INTERVAL_MS[interval]
    if len(rows) < 25:
        return False
    previous = None
    for candle in rows:
        if (
            not isinstance(candle, Candle)
            or candle.symbol != symbol
            or candle.interval != interval
            or candle.close_time_ms != candle.open_time_ms + period - 1
            or candle.close_time_ms >= now_ms
            or (previous is not None and candle.open_time_ms != previous + period)
            or candle.close <= 0
            or candle.volume < 0
        ):
            return False
        previous = candle.open_time_ms
    return True


def extract_features(
    histories: tuple[TimeframeHistory, ...], *, symbol: str, now_ms: int
) -> tuple[float, ...]:
    """Closed-candle-only, fixed-schema trend/volume/volatility/momentum/regime vector."""
    if type(histories) is not tuple or len(histories) != 3 or symbol not in SUPPORTED_SYMBOLS:
        raise ValueError("feature_schema_mismatch")
    by_interval = {h.interval: h for h in histories}
    if set(by_interval) != {"1m", "5m", "15m"}:
        raise ValueError("missing_timeframe")
    assessment = analyze_spot_timeframes(
        histories,
        symbol=symbol,
        execution_timeframe="1m",
        context_timeframe="5m",
        higher_timeframe="15m",
        now_ms=now_ms,
    )
    if not assessment.data_ok or assessment.regime is MarketRegime.UNCERTAIN:
        raise ValueError("uncertain_market_regime")
    regime_value = {
        MarketRegime.STRONG_UPTREND: 1.0,
        MarketRegime.UPTREND: 0.75,
        MarketRegime.BREAKOUT: 0.5,
        MarketRegime.RANGE: 0.0,
        MarketRegime.HIGH_VOLATILITY: 0.0,
        MarketRegime.LOW_VOLATILITY: 0.0,
        MarketRegime.DOWNTREND: -0.75,
        MarketRegime.STRONG_DOWNTREND: -1.0,
        MarketRegime.BREAKDOWN: -0.5,
        MarketRegime.SQUEEZE: 0.0,
        MarketRegime.CHAOTIC: 0.0,
    }[assessment.regime]
    values: list[float] = []
    for interval in ("1m", "5m", "15m"):
        candles = by_interval[interval].candles
        if not _valid_history(by_interval[interval], symbol, interval, now_ms):
            raise ValueError("stale_or_incomplete_closed_candles")
        returns = [
            float(candles[i].close / candles[i - 1].close - 1) for i in range(1, len(candles))
        ]
        mean = sum(returns[-20:]) / 20
        volatility = math.sqrt(sum((x - mean) ** 2 for x in returns[-20:]) / 20)
        base_volume = sum(float(c.volume) for c in candles[-20:-10]) / 10
        recent_volume = sum(float(c.volume) for c in candles[-10:]) / 10
        # Values are bounded for stable deterministic optimization.
        trend = max(-1.0, min(1.0, float(candles[-1].close / candles[-20].close - 1) * 20))
        momentum = max(-1.0, min(1.0, sum(returns[-5:]) * 20))
        volume = max(-1.0, min(1.0, (recent_volume / base_volume - 1) if base_volume > 0 else 0))
        vol = max(0.0, min(1.0, volatility * 100))
        values.extend((trend, volume, vol, momentum, regime_value))
    if len(values) != len(FEATURES) or any(not math.isfinite(v) for v in values):
        raise ValueError("invalid_feature_vector")
    return tuple(values)


def _softmax(scores: list[float]) -> list[float]:
    top = max(scores)
    exps = [math.exp(x - top) for x in scores]
    total = sum(exps)
    return [x / total for x in exps]


def train_chronological(
    rows: tuple[TrainingRow, ...],
    *,
    model_version: str,
    test_fraction: float = 0.2,
    outcome_horizon_ms: int = 900_000,
) -> TrainResult:
    """Fit fixed-seed-free full-batch multinomial logistic regression; purge label overlap."""
    if (
        type(rows) is not tuple
        or len(rows) < 30
        or not model_version
        or not 0.1 <= test_fraction <= 0.4
        or outcome_horizon_ms <= 0
    ):
        raise ValueError("insufficient_or_invalid_training_data")
    if any(
        type(r) is not TrainingRow
        or len(r.features) != len(FEATURES)
        or r.label not in CLASSES
        or any(not math.isfinite(x) for x in r.features)
        for r in rows
    ):
        raise ValueError("invalid_training_row")
    if any(a.close_at_ms >= b.close_at_ms for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("training_rows_not_chronological")
    split = int(len(rows) * (1 - test_fraction))
    while split > 1 and rows[split - 1].outcome_available_at_ms > rows[split].close_at_ms:
        split -= 1
    train, test = rows[:split], rows[split:]
    if len(train) < 20 or not test:
        raise ValueError("temporal_leakage_or_embargo_failure")
    # Normalize from train only; fit logistic weights on training partition.
    means = [sum(r.features[j] for r in train) / len(train) for j in range(len(FEATURES))]
    scales = [
        max(math.sqrt(sum((r.features[j] - means[j]) ** 2 for r in train) / len(train)), 1e-9)
        for j in range(len(FEATURES))
    ]
    weights = [[0.0] * (len(FEATURES) + 1) for _ in CLASSES]
    for _ in range(800):
        grads = [[0.0] * (len(FEATURES) + 1) for _ in CLASSES]
        for row in train:
            x = [1.0] + [(v - means[j]) / scales[j] for j, v in enumerate(row.features)]
            probs = _softmax([sum(wi * xi for wi, xi in zip(w, x, strict=True)) for w in weights])
            target = CLASSES.index(row.label)
            for c in range(3):
                for j, xi in enumerate(x):
                    grads[c][j] += (probs[c] - int(c == target)) * xi / len(train)
        rate = 0.08
        for c in range(3):
            for j in range(len(weights[c])):
                weights[c][j] -= rate * (grads[c][j] + (0.001 * weights[c][j] if j else 0))
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "model_version": model_version,
        "algorithm": "multinomial_logistic_regression_full_batch_v1",
        "classes": CLASSES,
        "features": FEATURES,
        "means": means,
        "scales": scales,
        "weights": weights,
        "calibration": "UNVERIFIED",
    }
    artifact_json = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(artifact_json.encode()).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "model_version": model_version,
        "artifact_sha256": digest,
        "row_count": len(train),
        "available_rows": len(rows),
        "holdout_rows": len(test),
        "data_sha256": hashlib.sha256(
            json.dumps(
                [[r.close_at_ms, r.outcome_available_at_ms, r.features, r.label] for r in train],
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "train_end_ms": train[-1].close_at_ms,
        "test_start_ms": test[0].close_at_ms,
        "train_rows": len(train),
        "test_rows": len(test),
        "feature_schema": FEATURES,
        "split": "chronological_with_label_embargo",
    }
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return TrainResult(
        model_version,
        artifact_json,
        digest,
        manifest_json,
        train[-1].close_at_ms,
        test[0].close_at_ms,
        len(train),
        len(test),
    )


def predict_research(
    histories: tuple[TimeframeHistory, ...],
    *,
    symbol: str,
    interval: str,
    now_ms: int,
    horizon_ms: int,
    artifact_json: str,
    expected_sha256: str,
    signal_id: str,
    recorder: ReceiptRecorder,
) -> ResearchPrediction:
    """Strict model adapter. It only emits a research prediction and delegates its receipt."""
    try:
        if hashlib.sha256(artifact_json.encode()).hexdigest() != expected_sha256:
            raise ValueError("artifact_integrity_failure")
        model = json.loads(artifact_json)
        if (
            model.get("schema_version") != SCHEMA_VERSION
            or tuple(model.get("features", ())) != FEATURES
        ):
            raise ValueError("artifact_schema_mismatch")
        if interval not in ("1m", "5m", "15m") or horizon_ms < INTERVAL_MS[interval]:
            raise ValueError("invalid_horizon")
        x = extract_features(histories, symbol=symbol, now_ms=now_ms)
        feature_digest = hashlib.sha256(
            json.dumps(x, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if (
            not isinstance(model.get("model_version"), str)
            or len(model["means"]) != len(FEATURES)
            or len(model["scales"]) != len(FEATURES)
            or len(model["weights"]) != len(CLASSES)
            or any(len(w) != len(FEATURES) + 1 for w in model["weights"])
            or any(not math.isfinite(float(v)) for v in model["means"] + model["scales"])
            or any(float(v) <= 0 for v in model["scales"])
        ):
            raise ValueError("artifact_shape_mismatch")
        normalized = [1.0] + [
            (v - m) / s for v, m, s in zip(x, model["means"], model["scales"], strict=True)
        ]
        probs = _softmax(
            [sum(a * b for a, b in zip(w, normalized, strict=True)) for w in model["weights"]]
        )
        from decimal import Decimal

        d = [Decimal(str(p)) for p in probs]
        d[2] = Decimal(1) - d[0] - d[1]
        probability = ProbabilityEstimate(win=d[2], loss=d[0], flat=d[1])
        action = (
            ModelAction.BUY
            if probs[2] > max(probs[0], probs[1])
            else (ModelAction.SELL if probs[0] > probs[1] else ModelAction.HOLD)
        )
        close_at = {h.interval: h.candles[-1].close_time_ms for h in histories}[interval]
        prediction = ResearchPrediction(
            action,
            signal_id,
            model["model_version"],
            SCHEMA_VERSION,
            symbol,
            interval,
            close_at,
            close_at + horizon_ms,
            probability,
            False,
            "UNCALIBRATED_RESEARCH_ONLY",
            expected_sha256,
            feature_digest,
        )
    except Exception:
        prediction = ResearchPrediction(
            ModelAction.NO_TRADE,
            signal_id,
            "unknown",
            SCHEMA_VERSION,
            symbol,
            interval,
            None,
            None,
            None,
            False,
            "MODEL_OR_DATA_FAILURE",
        )
    recorder(prediction)
    return prediction
