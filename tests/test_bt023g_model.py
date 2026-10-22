from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from binance_ai_trader.ai_market_regime import TimeframeHistory
from binance_ai_trader.ai_trainable_model import (
    FEATURES,
    ModelAction,
    ResearchPrediction,
    TrainingRow,
    predict_research,
    train_chronological,
)
from binance_ai_trader.market_data import INTERVAL_MS, Candle


def _histories() -> tuple[TimeframeHistory, ...]:
    result = []
    for interval in ("1m", "5m", "15m"):
        period = INTERVAL_MS[interval]
        start = 27_000_000 - period * 30
        candles = []
        for i in range(30):
            close = Decimal(100 + i) / 1
            candles.append(
                Candle(
                    "BTCUSDT",
                    interval,
                    start + i * period,
                    start + (i + 1) * period - 1,
                    close,
                    close + 1,
                    close - 1,
                    close,
                    Decimal(10 + i),
                )
            )
        result.append(TimeframeHistory(interval, tuple(candles)))
    return tuple(result)


def test_real_trainable_model_is_deterministic_and_manifest_hashes_artifact() -> None:
    rows = tuple(
        TrainingRow(
            i * 100,
            tuple((i % 7) / 7 for _ in FEATURES),
            ("BUY", "HOLD", "SELL")[i % 3],
            i * 100 + 10,
        )
        for i in range(60)
    )
    first = train_chronological(rows, model_version="logreg-1", outcome_horizon_ms=10)
    second = train_chronological(rows, model_version="logreg-1", outcome_horizon_ms=10)
    assert first.artifact_json == second.artifact_json
    assert hashlib.sha256(first.artifact_json.encode()).hexdigest() == first.artifact_sha256
    assert first.train_end_ms < first.test_start_ms
    assert '"algorithm":"multinomial_logistic_regression_full_batch_v1"' in first.artifact_json


def test_future_label_leakage_is_rejected() -> None:
    rows = tuple(
        TrainingRow(
            i * 100,
            tuple(0.0 for _ in FEATURES),
            "HOLD",
            (i + 2) * 100 if i == 47 else i * 100 + 10,
        )
        for i in range(60)
    )
    with pytest.raises(ValueError, match="temporal_leakage"):
        train_chronological(rows, model_version="v1", outcome_horizon_ms=10)


def test_model_fail_closed_and_receipts_callback_receives_uncalibrated_prediction() -> None:
    rows = tuple(
        TrainingRow(
            i * 100,
            tuple((i % 7) / 7 for _ in FEATURES),
            ("BUY", "HOLD", "SELL")[i % 3],
            i * 100 + 10,
        )
        for i in range(60)
    )
    model = train_chronological(rows, model_version="v1", outcome_horizon_ms=10)
    saved: list[ResearchPrediction] = []

    def record(prediction: ResearchPrediction) -> None:
        saved.append(prediction)

    bad = predict_research(
        (),
        symbol="BTCUSDT",
        interval="1m",
        now_ms=9_000_000,
        horizon_ms=60_000,
        artifact_json=model.artifact_json,
        expected_sha256=model.artifact_sha256,
        signal_id="signal_0001",
        recorder=record,
    )
    assert bad.action is ModelAction.NO_TRADE and bad.probability is None
    assert saved == [bad]
    good = predict_research(
        _histories(),
        symbol="BTCUSDT",
        interval="1m",
        now_ms=27_000_000,
        horizon_ms=60_000,
        artifact_json=model.artifact_json,
        expected_sha256=model.artifact_sha256,
        signal_id="signal_0002",
        recorder=record,
    )
    assert not good.probability_verified
    assert good.probability is not None
    changed = predict_research(
        _histories(),
        symbol="BTCUSDT",
        interval="1m",
        now_ms=27_000_000,
        horizon_ms=60_000,
        artifact_json=model.artifact_json,
        expected_sha256="bad",
        signal_id="signal_0003",
        recorder=record,
    )
    assert changed.action is ModelAction.NO_TRADE
