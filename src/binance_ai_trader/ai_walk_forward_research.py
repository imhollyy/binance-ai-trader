"""BT-023F bounded, provenance-aware offline market research primitives.

Historical candles are fetched only from Binance's public Spot market-data host.
This module never creates historical predictions or claims that candle-close
returns are executable fills. All reports remain research-only/no-trade.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_model_review import (
    ForecastOutcome,
    ObservedOutcome,
    SplitMetrics,
    _baseline_brier,
    _metrics,
)
from binance_ai_trader.market_data import (
    INTERVAL_MS,
    PUBLIC_MARKET_URL,
    Candle,
    MarketDataInvalid,
    PublicSpotMarketClient,
)

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)
MAX_RESEARCH_CANDLES = 20_000
PAGE_SIZE = 1000


class ResearchStatus(StrEnum):
    READY = "READY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_CALIBRATED = "NOT_CALIBRATED"
    RESEARCH_REVIEW_ONLY = "RESEARCH_REVIEW_ONLY"
    INVALID_DATA = "INVALID_DATA"
    OVERLAPPING_LABELS = "OVERLAPPING_LABELS"
    OVERLAPPING_TRADES = "OVERLAPPING_TRADES"


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    symbol: str
    interval: str
    start_open_ms: int
    end_open_ms: int
    retrieved_at_ms: int
    source_url: str
    candle_count: int
    candle_sha256: str
    candles: tuple[Candle, ...]

    @property
    def research_only(self) -> bool:
        return True

    @property
    def trade_authorized(self) -> bool:
        return False


def _canonical_candle(candle: Candle) -> list[str | int]:
    return [
        candle.symbol,
        candle.interval,
        candle.open_time_ms,
        candle.close_time_ms,
        format(candle.open, "f"),
        format(candle.high, "f"),
        format(candle.low, "f"),
        format(candle.close, "f"),
        format(candle.volume, "f"),
    ]


def fetch_historical_spot_dataset(
    client: PublicSpotMarketClient,
    *,
    symbol: str,
    interval: str,
    start_open_ms: int,
    end_open_ms: int,
    max_candles: int = MAX_RESEARCH_CANDLES,
) -> HistoricalDataset:
    """Fetch one complete, bounded, closed-candle range with a canonical digest.

    The injected client provides public-only transport behavior and stops on
    HTTP 418/429. Missing intervals, duplicate records and unfinished ends fail.
    Callers must retain this manifest with their report; no forecast is created.
    """
    period = INTERVAL_MS.get(interval)
    if (
        period is None
        or type(start_open_ms) is not int
        or type(end_open_ms) is not int
        or type(max_candles) is not int
        or start_open_ms < 0
        or start_open_ms % period
        or end_open_ms < start_open_ms
        or end_open_ms % period
        or not 1 <= max_candles <= MAX_RESEARCH_CANDLES
    ):
        raise MarketDataInvalid("invalid_historical_research_window")
    expected_count = (end_open_ms - start_open_ms) // period + 1
    if expected_count > max_candles:
        raise MarketDataInvalid("historical_window_exceeds_bound")
    candles: list[Candle] = []
    cursor = start_open_ms
    while cursor <= end_open_ms:
        page = client.fetch_closed_klines(
            symbol=symbol,
            interval=interval,
            limit=min(PAGE_SIZE, (end_open_ms - cursor) // period + 1),
            start_time_ms=cursor,
        )
        page = [candle for candle in page if candle.open_time_ms <= end_open_ms]
        if not page:
            raise MarketDataInvalid("historical_window_has_missing_candles")
        for candle in page:
            if candle.open_time_ms != cursor:
                raise MarketDataInvalid("historical_window_has_gap_or_duplicate")
            candles.append(candle)
            cursor += period
            if len(candles) > max_candles:
                raise MarketDataInvalid("historical_window_exceeds_bound")
    if len(candles) != expected_count:
        raise MarketDataInvalid("historical_window_has_missing_candles")
    retrieved_at_ms = time.time_ns() // 1_000_000
    if retrieved_at_ms <= end_open_ms + period - 1:
        raise MarketDataInvalid("local_retrieval_clock_precedes_candle_close")
    encoded = json.dumps(
        [_canonical_candle(candle) for candle in candles],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return HistoricalDataset(
        symbol=symbol,
        interval=interval,
        start_open_ms=start_open_ms,
        end_open_ms=end_open_ms,
        retrieved_at_ms=retrieved_at_ms,
        source_url=PUBLIC_MARKET_URL + "/api/v3/klines",
        candle_count=len(candles),
        candle_sha256=hashlib.sha256(encoded).hexdigest(),
        candles=tuple(candles),
    )


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start_ms: int
    train_end_ms: int
    calibration_start_ms: int
    calibration_end_ms: int
    validation_start_ms: int
    validation_end_ms: int
    test_start_ms: int
    test_end_ms: int
    embargo_ms: int


@dataclass(frozen=True, slots=True)
class FoldReport:
    fold_index: int
    status: ResearchStatus
    validation: SplitMetrics | None = None
    test: SplitMetrics | None = None
    train_test_brier_gap: Decimal | None = None
    outcome_counts: tuple[int, int, int] | None = None
    test_baseline_brier: Decimal | None = None
    test_brier_improvement: Decimal | None = None
    model_version: str | None = None
    test_start_ms: int | None = None
    test_end_ms: int | None = None

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def build_expanding_windows(
    *,
    start_ms: int,
    period_ms: int,
    train_periods: int,
    calibration_periods: int,
    validation_periods: int,
    test_periods: int,
    embargo_periods: int,
    folds: int,
) -> tuple[WalkForwardWindow, ...]:
    """Create expanding chronological folds; test outcomes never set parameters."""
    positive = (
        start_ms,
        period_ms,
        train_periods,
        calibration_periods,
        validation_periods,
        test_periods,
        folds,
    )
    if (
        any(type(value) is not int or value <= 0 for value in positive)
        or type(embargo_periods) is not int
        or embargo_periods < 0
        or start_ms % period_ms
    ):
        raise ValueError("walk_forward_bounds_invalid")
    if folds > 20 or min(train_periods, calibration_periods, validation_periods, test_periods) < 1:
        raise ValueError("walk_forward_bounds_invalid")
    cursor = start_ms
    windows: list[WalkForwardWindow] = []
    for _ in range(folds):
        train_start = start_ms
        train_end = cursor + train_periods * period_ms
        cal_start = train_end + embargo_periods * period_ms
        cal_end = cal_start + calibration_periods * period_ms
        val_start = cal_end + embargo_periods * period_ms
        val_end = val_start + validation_periods * period_ms
        test_start = val_end + embargo_periods * period_ms
        test_end = test_start + test_periods * period_ms
        windows.append(
            WalkForwardWindow(
                train_start,
                train_end,
                cal_start,
                cal_end,
                val_start,
                val_end,
                test_start,
                test_end,
                embargo_periods * period_ms,
            )
        )
        cursor = test_end
    return tuple(windows)


def audit_fold(
    *,
    fold_index: int,
    window: WalkForwardWindow,
    train: Sequence[ForecastOutcome],
    calibration: Sequence[ForecastOutcome],
    validation: Sequence[ForecastOutcome],
    test: Sequence[ForecastOutcome],
    minimum_samples: int = 120,
) -> FoldReport:
    """Audit pre-existing forecasts; never synthesizes historical model output."""
    splits = (
        (train, window.train_start_ms, window.train_end_ms),
        (calibration, window.calibration_start_ms, window.calibration_end_ms),
        (validation, window.validation_start_ms, window.validation_end_ms),
        (test, window.test_start_ms, window.test_end_ms),
    )
    seen_ids: set[str] = set()
    model_versions: set[str] = set()
    for records, start, end in splits:
        if len(records) < minimum_samples:
            return FoldReport(fold_index, ResearchStatus.INSUFFICIENT_EVIDENCE)
        previous = start - 1
        previous_label_end = start - 1
        for record in records:
            if not isinstance(record, ForecastOutcome):
                return FoldReport(fold_index, ResearchStatus.INVALID_DATA)
            if (
                not isinstance(record.signal_id, str)
                or not record.signal_id
                or not isinstance(record.model_version, str)
                or not record.model_version
                or record.symbol not in ("BTCUSDT", "ETHUSDT")
                or record.interval not in INTERVAL_MS
                or not isinstance(record.realized_outcome, ObservedOutcome)
                or not isinstance(record.forecast, ProbabilityEstimate)
                or type(record.observed_close_at_ms) is not int
                or record.observed_close_at_ms <= previous
                or type(record.forecast_recorded_at_ms) is not int
                or record.forecast_recorded_at_ms <= record.observed_close_at_ms
                or record.signal_id in seen_ids
            ):
                return FoldReport(fold_index, ResearchStatus.INVALID_DATA)
            probabilities = (record.forecast.win, record.forecast.loss, record.forecast.flat)
            if (
                any(
                    type(value) is not Decimal or not value.is_finite() or not ZERO <= value <= ONE
                    for value in probabilities
                )
                or sum(probabilities, ZERO) != ONE
            ):
                return FoldReport(fold_index, ResearchStatus.INVALID_DATA)
            label_end = record.label_available_at_ms
            if type(label_end) is not int or label_end <= record.observed_close_at_ms:
                return FoldReport(fold_index, ResearchStatus.INVALID_DATA)
            if record.forecast_recorded_at_ms >= label_end:
                return FoldReport(fold_index, ResearchStatus.OVERLAPPING_LABELS)
            if not start <= record.observed_close_at_ms < end or label_end > end:
                return FoldReport(fold_index, ResearchStatus.OVERLAPPING_LABELS)
            if label_end > record.observed_close_at_ms + window.embargo_ms:
                return FoldReport(fold_index, ResearchStatus.OVERLAPPING_LABELS)
            if record.observed_close_at_ms < previous_label_end:
                return FoldReport(fold_index, ResearchStatus.OVERLAPPING_LABELS)
            seen_ids.add(record.signal_id)
            model_versions.add(record.model_version)
            previous = record.observed_close_at_ms
            previous_label_end = label_end
    if len(model_versions) != 1:
        return FoldReport(fold_index, ResearchStatus.INVALID_DATA)
    metrics_train = _metrics(train)
    metrics_cal = _metrics(calibration)
    metrics_val = _metrics(validation)
    metrics_test = _metrics(test)
    if (
        min(metrics_train.outcome_counts) < 10
        or min(metrics_cal.outcome_counts) < 10
        or min(metrics_val.outcome_counts) < 10
        or min(metrics_test.outcome_counts) < 10
    ):
        return FoldReport(
            fold_index,
            ResearchStatus.INSUFFICIENT_EVIDENCE,
            metrics_val,
            metrics_test,
            outcome_counts=metrics_test.outcome_counts,
        )
    # Calibration data describes the reliability of recorded probabilities;
    # no temperature/scaling parameters are learned from the test split.
    gap = metrics_test.multiclass_brier - metrics_train.multiclass_brier
    baseline = _baseline_brier(calibration, test)
    improvement = baseline - metrics_test.multiclass_brier
    status = (
        ResearchStatus.NOT_CALIBRATED
        if metrics_cal.expected_calibration_error > Decimal("0.08")
        or metrics_val.expected_calibration_error > Decimal("0.08")
        or metrics_test.expected_calibration_error > Decimal("0.08")
        or improvement <= 0
        else ResearchStatus.RESEARCH_REVIEW_ONLY
    )
    return FoldReport(
        fold_index,
        status,
        metrics_val,
        metrics_test,
        gap,
        metrics_test.outcome_counts,
        baseline,
        improvement,
        next(iter(model_versions)),
        window.test_start_ms,
        window.test_end_ms,
    )


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    status: ResearchStatus
    fold_count: int
    model_version: str | None
    test_brier_mean: Decimal | None
    test_brier_worst: Decimal | None
    train_to_test_degradation_folds: int
    cost_profitable_folds: int | None
    stress_profitable_fold_counts: tuple[int, ...]

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        return False

    @property
    def trade_authorized(self) -> bool:
        return False


def summarize_walk_forward(
    reports: Sequence[FoldReport],
    *,
    cost_reports: Sequence[CostPerformance] | None = None,
) -> WalkForwardSummary:
    """Summarize at least three untouched fold reports without model promotion."""
    if (
        type(reports) not in (tuple, list)
        or len(reports) < 3
        or any(
            report.status
            not in (ResearchStatus.RESEARCH_REVIEW_ONLY, ResearchStatus.NOT_CALIBRATED)
            or report.test is None
            or report.model_version is None
            or report.test_start_ms is None
            or report.test_end_ms is None
            for report in reports
        )
        or len({report.model_version for report in reports}) != 1
        or len({report.fold_index for report in reports}) != len(reports)
        or any(
            left.test_end_ms is None
            or right.test_start_ms is None
            or left.test_end_ms >= right.test_start_ms
            for left, right in zip(reports, reports[1:], strict=False)
        )
    ):
        return WalkForwardSummary(
            ResearchStatus.INSUFFICIENT_EVIDENCE,
            len(reports) if isinstance(reports, (tuple, list)) else 0,
            None,
            None,
            None,
            0,
            None,
            (),
        )
    briers = [report.test.multiclass_brier for report in reports if report.test is not None]
    degradation = sum(
        report.train_test_brier_gap is not None and report.train_test_brier_gap > 0
        for report in reports
    )
    status = (
        ResearchStatus.RESEARCH_REVIEW_ONLY
        if all(report.status is ResearchStatus.RESEARCH_REVIEW_ONLY for report in reports)
        else ResearchStatus.NOT_CALIBRATED
    )
    profitable: int | None = None
    stress_counts: tuple[int, ...] = ()
    if cost_reports is not None:
        if len(cost_reports) != len(reports) or any(
            result.status is not ResearchStatus.READY for result in cost_reports
        ):
            status = ResearchStatus.INSUFFICIENT_EVIDENCE
        else:
            profitable = sum(result.net_return_bps > 0 for result in cost_reports)
            stress_width = min(len(result.stress_net_bps) for result in cost_reports)
            stress_counts = tuple(
                sum(result.stress_net_bps[index] > 0 for result in cost_reports)
                for index in range(stress_width)
            )
    return WalkForwardSummary(
        status,
        len(reports),
        reports[0].model_version,
        sum(briers, ZERO) / len(briers),
        max(briers),
        degradation,
        profitable,
        stress_counts,
    )


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    buy_fee_bps: Decimal
    sell_fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps_per_side: Decimal


@dataclass(frozen=True, slots=True)
class TradeCostRow:
    signal_id: str
    regime: str
    gross_return_bps: Decimal
    buy_commission_bps: Decimal
    sell_commission_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    total_cost_bps: Decimal
    net_return_bps: Decimal


@dataclass(frozen=True, slots=True)
class CostPerformance:
    status: ResearchStatus
    assumptions: CostAssumptions
    trades: tuple[TradeCostRow, ...] = ()
    gross_return_bps: Decimal = ZERO
    net_return_bps: Decimal = ZERO
    maximum_drawdown: Decimal = ZERO
    wins: int = 0
    losses: int = 0
    stress_net_bps: tuple[Decimal, ...] = ()
    regime_net_bps: tuple[tuple[str, Decimal], ...] = ()

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def cost_inputs_are_verified(self) -> bool:
        return False


def _regime(candles: Sequence[Candle], index: int) -> str:
    history = candles[max(0, index - 20) : index]
    if len(history) < 5:
        return "UNCERTAIN"
    move = history[-1].close / history[0].close - ONE
    return (
        "UPTREND" if move > Decimal("0.01") else "DOWNTREND" if move < Decimal("-0.01") else "RANGE"
    )


def cost_adjusted_long_replay(
    *,
    forecasts: Sequence[ForecastOutcome],
    candles: Sequence[Candle],
    assumptions: CostAssumptions,
    win_probability_threshold: Decimal = Decimal("0.5"),
    stress_multipliers: tuple[Decimal, ...] = (ONE, Decimal("1.5"), Decimal("2")),
) -> CostPerformance:
    """Close-to-close long replay with explicit hypothetical two-sided costs.

    Fees, spread and slippage are assumptions, not account commission evidence,
    order-book observations, or fill data. Overlapping positions fail closed.
    """
    costs = (
        assumptions.buy_fee_bps,
        assumptions.sell_fee_bps,
        assumptions.spread_bps,
        assumptions.slippage_bps_per_side,
    )
    if (
        type(assumptions) is not CostAssumptions
        or type(forecasts) not in (tuple, list)
        or type(candles) not in (tuple, list)
        or type(win_probability_threshold) is not Decimal
        or not win_probability_threshold.is_finite()
        or type(stress_multipliers) is not tuple
        or any(
            type(multiplier) is not Decimal
            or not multiplier.is_finite()
            or not ONE <= multiplier <= Decimal("10")
            for multiplier in stress_multipliers
        )
        or not stress_multipliers
    ):
        return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
    if (
        any(
            type(value) is not Decimal or not value.is_finite() or value < 0 or value > 100
            for value in costs
        )
        or not Decimal(0) < win_probability_threshold <= ONE
    ):
        return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
    if len(candles) < 2:
        return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
    if not isinstance(candles[0], Candle):
        return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
    market = candles[0].symbol
    interval = candles[0].interval
    period = INTERVAL_MS.get(interval)
    if period is None:
        return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
    previous_open: int | None = None
    for candle in candles:
        if (
            not isinstance(candle, Candle)
            or candle.symbol != market
            or candle.interval != interval
            or candle.open_time_ms % period
            or candle.close_time_ms != candle.open_time_ms + period - 1
            or previous_open is not None
            and candle.open_time_ms != previous_open + period
            or any(
                not value.is_finite() or value <= 0
                for value in (candle.open, candle.high, candle.low, candle.close)
            )
            or not candle.low <= min(candle.open, candle.close)
            or not candle.high >= max(candle.open, candle.close)
            or not candle.volume.is_finite()
            or candle.volume < 0
        ):
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        previous_open = candle.open_time_ms
    seen_signals: set[str] = set()
    previous_forecast_at = -1
    for forecast in forecasts:
        if not isinstance(forecast, ForecastOutcome):
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        if (
            not isinstance(forecast.forecast, ProbabilityEstimate)
            or not isinstance(forecast.realized_outcome, ObservedOutcome)
            or not isinstance(forecast.signal_id, str)
            or type(forecast.observed_close_at_ms) is not int
            or type(forecast.label_available_at_ms) is not int
            or type(forecast.forecast_recorded_at_ms) is not int
            or forecast.observed_close_at_ms <= previous_forecast_at
            or forecast.forecast_recorded_at_ms <= forecast.observed_close_at_ms
            or forecast.forecast_recorded_at_ms >= forecast.label_available_at_ms
        ):
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        probabilities = (forecast.forecast.win, forecast.forecast.loss, forecast.forecast.flat)
        if (
            forecast.signal_id in seen_signals
            or forecast.symbol != market
            or forecast.interval != interval
            or any(type(value) is not Decimal or not value.is_finite() for value in probabilities)
            or sum(probabilities, ZERO) != ONE
        ):
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        seen_signals.add(forecast.signal_id)
        previous_forecast_at = forecast.observed_close_at_ms
    by_close = {candle.close_time_ms: (index, candle) for index, candle in enumerate(candles)}
    rows: list[TradeCostRow] = []
    last_exit = -1
    for forecast in forecasts:
        if forecast.forecast.win < win_probability_threshold:
            continue
        source = by_close.get(forecast.observed_close_at_ms)
        target = by_close.get(forecast.label_available_at_ms)
        if source is None or target is None:
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        source_index, entry = source
        target_index, exit_candle = target
        if source_index <= last_exit:
            return CostPerformance(ResearchStatus.OVERLAPPING_TRADES, assumptions)
        if target_index <= source_index or entry.close <= 0 or exit_candle.close <= 0:
            return CostPerformance(ResearchStatus.INVALID_DATA, assumptions)
        gross = (exit_candle.close / entry.close - ONE) * BPS
        buy_fee = assumptions.buy_fee_bps
        sell_fee = assumptions.sell_fee_bps * exit_candle.close / entry.close
        spread_cost = assumptions.spread_bps * (ONE + exit_candle.close / entry.close) / 2
        slippage_cost = assumptions.slippage_bps_per_side * (ONE + exit_candle.close / entry.close)
        total = buy_fee + sell_fee + spread_cost + slippage_cost
        rows.append(
            TradeCostRow(
                forecast.signal_id,
                _regime(candles, source_index),
                gross,
                buy_fee,
                sell_fee,
                spread_cost,
                slippage_cost,
                total,
                gross - total,
            )
        )
        last_exit = target_index
    if not rows:
        return CostPerformance(ResearchStatus.INSUFFICIENT_EVIDENCE, assumptions)
    equity = ONE
    peak = ONE
    drawdown = ZERO
    for row in rows:
        equity *= ONE + row.net_return_bps / BPS
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    gross_total = sum((row.gross_return_bps for row in rows), ZERO)
    net_total = sum((row.net_return_bps for row in rows), ZERO)
    stresses: list[Decimal] = []
    for multiplier in stress_multipliers:
        stresses.append(
            sum((row.gross_return_bps - row.total_cost_bps * multiplier for row in rows), ZERO)
        )
    regime_totals: dict[str, Decimal] = {}
    for row in rows:
        regime_totals[row.regime] = regime_totals.get(row.regime, ZERO) + row.net_return_bps
    return CostPerformance(
        ResearchStatus.READY,
        assumptions,
        tuple(rows),
        gross_total,
        net_total,
        drawdown,
        sum(row.net_return_bps > 0 for row in rows),
        sum(row.net_return_bps <= 0 for row in rows),
        tuple(stresses),
        tuple(sorted(regime_totals.items())),
    )
