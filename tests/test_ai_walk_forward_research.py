"""Synthetic-only checks for BT-023F math and provenance guards."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_model_review import ForecastOutcome, ObservedOutcome
from binance_ai_trader.ai_walk_forward_research import (
    CostAssumptions,
    ResearchStatus,
    audit_fold,
    build_expanding_windows,
    cost_adjusted_long_replay,
    fetch_historical_spot_dataset,
    summarize_walk_forward,
)
from binance_ai_trader.market_data import Candle, MarketDataInvalid, PublicSpotMarketClient

D = Decimal
MINUTE = 60_000
ORIGIN = 1_700_000_000_000 - (1_700_000_000_000 % MINUTE)


def raw_kline(open_ms: int, close: str = "101") -> list[object]:
    return [
        open_ms,
        "100",
        "102",
        "99",
        close,
        "3",
        open_ms + MINUTE - 1,
        "300",
        10,
        "1",
        "100",
        "0",
    ]


def test_historical_fetch_has_closed_contiguous_hash_and_source_provenance() -> None:
    start = ORIGIN
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "data-api.binance.vision"
        assert request.url.path == "/api/v3/klines"
        return httpx.Response(200, json=[raw_kline(start), raw_kline(start + MINUTE)])

    client = PublicSpotMarketClient(
        transport=httpx.MockTransport(respond), clock_ms=lambda: start + 2 * MINUTE + 5
    )
    data = fetch_historical_spot_dataset(
        client,
        symbol="BTCUSDT",
        interval="1m",
        start_open_ms=start,
        end_open_ms=start + MINUTE,
    )
    assert data.candle_count == 2
    assert data.source_url.endswith("/api/v3/klines")
    assert len(data.candle_sha256) == 64
    assert data.research_only and not data.trade_authorized
    assert calls == 1


def test_downloaded_binance_research_artifacts_match_stored_sha256_manifest() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "research_artifacts" / "bt023f_binance_spot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["symbol"] == "BTCUSDT"
    assert manifest["interval"] == "5m"
    assert len(manifest["windows"]) == 4
    for window in manifest["windows"]:
        raw_path = root / window["artifact_path"]
        raw_bytes = raw_path.read_bytes()
        rows = json.loads(raw_bytes)
        assert hashlib.sha256(raw_bytes).hexdigest() == window["artifact_file_sha256"]
        canonical = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        assert hashlib.sha256(canonical).hexdigest() == window["candle_sha256"]
        assert len(rows) == window["candle_count"] == 288
        assert all(rows[index][2] == rows[index - 1][2] + 300_000 for index in range(1, len(rows)))
        assert window["historical_ai_forecasts_present"] is False


def test_historical_fetch_rejects_gap_open_end_and_oversized_ranges() -> None:
    start = ORIGIN
    missing = PublicSpotMarketClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[raw_kline(start + MINUTE)])
        ),
        clock_ms=lambda: start + 3 * MINUTE,
    )
    with pytest.raises(MarketDataInvalid, match="gap_or_duplicate"):
        fetch_historical_spot_dataset(
            missing,
            symbol="BTCUSDT",
            interval="1m",
            start_open_ms=start,
            end_open_ms=start + MINUTE,
        )
    empty = PublicSpotMarketClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
        clock_ms=lambda: start + 3 * MINUTE,
    )
    with pytest.raises(MarketDataInvalid, match="historical_window_has_missing"):
        fetch_historical_spot_dataset(
            empty,
            symbol="BTCUSDT",
            interval="1m",
            start_open_ms=start + 2 * MINUTE,
            end_open_ms=start + 2 * MINUTE,
        )
    with pytest.raises(MarketDataInvalid, match="exceeds_bound"):
        fetch_historical_spot_dataset(
            missing,
            symbol="BTCUSDT",
            interval="1m",
            start_open_ms=start,
            end_open_ms=start + 20_000 * MINUTE,
        )


def test_expanding_walk_forward_windows_are_chronological_with_embargo() -> None:
    windows = build_expanding_windows(
        start_ms=ORIGIN,
        period_ms=MINUTE,
        train_periods=100,
        calibration_periods=20,
        validation_periods=20,
        test_periods=20,
        embargo_periods=2,
        folds=3,
    )
    assert len(windows) == 3
    assert windows[0].test_end_ms < windows[1].test_start_ms
    assert windows[0].train_end_ms < windows[1].train_end_ms
    with pytest.raises(ValueError):
        build_expanding_windows(
            start_ms=1,
            period_ms=MINUTE,
            train_periods=1,
            calibration_periods=1,
            validation_periods=1,
            test_periods=1,
            embargo_periods=0,
            folds=21,
        )


def scored_rows(prefix: str, start: int) -> tuple[ForecastOutcome, ...]:
    labels = (ObservedOutcome.WIN, ObservedOutcome.LOSS, ObservedOutcome.FLAT) * 40
    return tuple(
        ForecastOutcome(
            f"{prefix}_{index:03d}",
            "baseline_v1",
            "BTCUSDT",
            "1m",
            start + index * 2 * MINUTE,
            start + index * 2 * MINUTE + MINUTE,
            ProbabilityEstimate(win=D(".34"), loss=D(".33"), flat=D(".33")),
            label,
            start + index * 2 * MINUTE + 1,
        )
        for index, label in enumerate(labels)
    )


def test_fold_reports_class_counts_brier_logloss_baseline_and_no_calibration_claim() -> None:
    windows = build_expanding_windows(
        start_ms=ORIGIN,
        period_ms=MINUTE,
        train_periods=500,
        calibration_periods=300,
        validation_periods=300,
        test_periods=300,
        embargo_periods=1,
        folds=1,
    )
    window = windows[0]

    # Each split is spread across its window and uses non-overlapping 1m labels.
    def place(prefix: str, begin: int) -> tuple[ForecastOutcome, ...]:
        return tuple(
            ForecastOutcome(
                f"{prefix}_{i:03d}",
                "baseline_v1",
                "BTCUSDT",
                "1m",
                begin + i * 2 * MINUTE,
                begin + i * 2 * MINUTE + MINUTE,
                ProbabilityEstimate(win=D(".34"), loss=D(".33"), flat=D(".33")),
                (ObservedOutcome.WIN, ObservedOutcome.LOSS, ObservedOutcome.FLAT)[i % 3],
                begin + i * 2 * MINUTE + 1,
            )
            for i in range(120)
        )

    report = audit_fold(
        fold_index=0,
        window=window,
        train=place("train", window.train_start_ms),
        calibration=place("cal", window.calibration_start_ms),
        validation=place("val", window.validation_start_ms),
        test=place("test", window.test_start_ms),
    )
    assert report.status is ResearchStatus.NOT_CALIBRATED
    assert report.validation is not None and report.test is not None
    assert report.validation.samples == 120
    assert sum(bin_.samples for bin_ in report.test.reliability_bins) == 120
    assert report.test.multiclass_log_loss > 0
    assert report.test_baseline_brier is not None
    assert report.model_version == "baseline_v1"
    assert not report.qualifies_as_calibrated_model and report.no_trade
    assert summarize_walk_forward((report, report)).status is ResearchStatus.INSUFFICIENT_EVIDENCE
    folds = tuple(
        replace(
            report,
            fold_index=index,
            test_start_ms=window.test_start_ms + index * 1_000 * MINUTE,
            test_end_ms=window.test_end_ms + index * 1_000 * MINUTE,
        )
        for index in range(3)
    )
    summary = summarize_walk_forward(folds)
    assert summary.fold_count == 3
    assert summary.status is ResearchStatus.NOT_CALIBRATED
    assert not summary.qualifies_as_calibrated_model and not summary.trade_authorized


def test_fold_rejects_overlapping_labels_and_mixed_model_versions() -> None:
    window = build_expanding_windows(
        start_ms=ORIGIN,
        period_ms=MINUTE,
        train_periods=500,
        calibration_periods=300,
        validation_periods=300,
        test_periods=300,
        embargo_periods=1,
        folds=1,
    )[0]
    assert window.embargo_ms == MINUTE

    def place(prefix: str, begin: int) -> tuple[ForecastOutcome, ...]:
        return tuple(
            ForecastOutcome(
                f"{prefix}_{i:03d}",
                "baseline_v1",
                "BTCUSDT",
                "1m",
                begin + i * 2 * MINUTE,
                begin + i * 2 * MINUTE + MINUTE,
                ProbabilityEstimate(win=D(".34"), loss=D(".33"), flat=D(".33")),
                (ObservedOutcome.WIN, ObservedOutcome.LOSS, ObservedOutcome.FLAT)[i % 3],
                forecast_recorded_at_ms=begin + i * 2 * MINUTE + 1,
            )
            for i in range(120)
        )

    calibration = place("cal", window.calibration_start_ms)
    validation = place("val", window.validation_start_ms)
    test = place("test", window.test_start_ms)
    overlap = audit_fold(
        fold_index=0,
        window=window,
        train=place("train", window.train_start_ms),
        calibration=(
            replace(
                calibration[0],
                label_available_at_ms=calibration[0].observed_close_at_ms + 2 * MINUTE,
            ),
            *calibration[1:],
        ),
        validation=validation,
        test=test,
    )
    assert overlap.status is ResearchStatus.OVERLAPPING_LABELS
    mixed = audit_fold(
        fold_index=0,
        window=window,
        train=place("train", window.train_start_ms),
        calibration=calibration,
        validation=(replace(validation[0], model_version="other_model"), *validation[1:]),
        test=test,
    )
    assert mixed.status is ResearchStatus.INVALID_DATA
    hindsight = audit_fold(
        fold_index=0,
        window=window,
        train=place("train", window.train_start_ms),
        calibration=calibration,
        validation=validation,
        test=(
            replace(
                test[0],
                forecast_recorded_at_ms=test[0].label_available_at_ms,
            ),
            *test[1:],
        ),
    )
    assert hindsight.status is ResearchStatus.OVERLAPPING_LABELS


def test_cost_replay_reports_separate_costs_net_drawdown_and_stress() -> None:
    closes = (D("100"), D("110"), D("100"), D("90"))
    candles = tuple(
        Candle(
            "BTCUSDT",
            "1m",
            index * MINUTE,
            (index + 1) * MINUTE - 1,
            close,
            close,
            close,
            close,
            D("1"),
        )
        for index, close in enumerate(closes)
    )
    forecasts = (
        ForecastOutcome(
            "trade_one",
            "baseline_v1",
            "BTCUSDT",
            "1m",
            candles[0].close_time_ms,
            candles[1].close_time_ms,
            ProbabilityEstimate(win=D(".9"), loss=D(".05"), flat=D(".05")),
            ObservedOutcome.WIN,
            candles[0].close_time_ms + 1,
        ),
        ForecastOutcome(
            "trade_two",
            "baseline_v1",
            "BTCUSDT",
            "1m",
            candles[2].close_time_ms,
            candles[3].close_time_ms,
            ProbabilityEstimate(win=D(".9"), loss=D(".05"), flat=D(".05")),
            ObservedOutcome.LOSS,
            candles[2].close_time_ms + 1,
        ),
    )
    report = cost_adjusted_long_replay(
        forecasts=forecasts,
        candles=candles,
        assumptions=CostAssumptions(D("10"), D("10"), D("8"), D("3")),
    )
    assert report.status is ResearchStatus.READY
    assert len(report.trades) == 2
    assert report.trades[0].gross_return_bps == D("1000")
    assert report.trades[0].buy_commission_bps == D("10")
    assert report.trades[0].sell_commission_bps == D("11")
    assert report.trades[0].net_return_bps < report.trades[0].gross_return_bps
    assert report.maximum_drawdown > 0
    assert report.stress_net_bps[2] < report.stress_net_bps[0]
    assert not report.cost_inputs_are_verified
    assert report.regime_net_bps
