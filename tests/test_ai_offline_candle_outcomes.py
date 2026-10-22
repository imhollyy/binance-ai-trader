"""BT-023C offline, synthetic candle-based labels and self-reported hash replay."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_candle_outcomes import (
    LabelStatus,
    OfflineCandleLabelLedger,
    RecordedForecast,
    build_offline_candle_label_ledger,
    verify_offline_candle_label_ledger,
)
from binance_ai_trader.ai_offline_model_review import ObservedOutcome
from binance_ai_trader.market_data import Candle

D = Decimal
START = 1_800_000_000_000
STEP = 60_000
COST = Decimal("20")
BAND = Decimal("25")
PRICES = ("100", "100", "101", "101", "99", "100", "103", "105")


def candles() -> tuple[Candle, ...]:
    return tuple(
        Candle(
            "BTCUSDT",
            "1m",
            START + i * STEP,
            START + (i + 1) * STEP - 1,
            D(price),
            D(price) + D("2"),
            D(price) - D("2"),
            D(price),
            D("10"),
        )
        for i, price in enumerate(PRICES)
    )


def forecasts() -> tuple[RecordedForecast, ...]:
    p = ProbabilityEstimate(win=D(".50"), loss=D(".30"), flat=D(".20"))
    return tuple(
        RecordedForecast(
            f"forecast_{i:04d}",
            "offline_model_v1",
            "BTCUSDT",
            "1m",
            START + (i + 1) * STEP - 1,
            START + (i + 1) * STEP,
            p,
        )
        for i in (1, 2, 3)
    )


def label(
    *,
    data: tuple[Candle, ...] | None = None,
    rows: tuple[RecordedForecast, ...] | None = None,
    cost: Decimal = COST,
    band: Decimal = BAND,
    period: int = STEP,
    evaluation: int = START + len(PRICES) * STEP,
    symbol: str = "BTCUSDT",
) -> OfflineCandleLabelLedger:
    return build_offline_candle_label_ledger(
        candles() if data is None else data,
        forecasts() if rows is None else rows,
        symbol=symbol,
        interval="1m",
        model_version="offline_model_v1",
        outcome_horizon_ms=period,
        hypothetical_roundtrip_cost_bps=cost,
        flat_band_bps=band,
        evaluated_at_ms=evaluation,
    )


def replay(
    ledger: OfflineCandleLabelLedger,
    *,
    data: tuple[Candle, ...] | None = None,
    rows: tuple[RecordedForecast, ...] | None = None,
    reference: str | None = None,
    cost: Decimal = COST,
) -> OfflineCandleLabelLedger:
    return verify_offline_candle_label_ledger(
        ledger,
        candles() if data is None else data,
        forecasts() if rows is None else rows,
        symbol="BTCUSDT",
        interval="1m",
        model_version="offline_model_v1",
        outcome_horizon_ms=STEP,
        hypothetical_roundtrip_cost_bps=cost,
        flat_band_bps=BAND,
        evaluated_at_ms=START + len(PRICES) * STEP,
        detached_head_sha256=reference,
    )


def test_reconstruct_forward_candle_labels_after_costs_and_hash_link() -> None:
    original = label()
    assert original.status is LabelStatus.UNANCHORED_RESEARCH_ONLY
    assert [e.forecast_outcome.realized_outcome for e in original.entries] == [
        ObservedOutcome.WIN,
        ObservedOutcome.FLAT,
        ObservedOutcome.LOSS,
    ]
    assert original.entries[0].hypothetical_net_return_bps == D("80")
    assert original.entries[1].hypothetical_net_return_bps == D("-20")
    assert original.entries[2].hypothetical_net_return_bps < D("-200")
    assert all(
        entry.forecast_outcome.label_available_at_ms > entry.recorded_at_ms
        for entry in original.entries
    )
    assert original.entries[0].previous_hash == "0" * 64
    assert original.entries[1].previous_hash == original.entries[0].entry_hash
    assert original.entries[-1].entry_hash == original.head_sha256
    assert len(original.candle_sha256 or "") == 64
    assert label() == original
    assert original.no_trade and not original.trade_authorized
    assert not original.qualifies_as_calibrated_model


def test_detached_reference_replay_detects_changes_but_cannot_attest_real_market() -> None:
    baseline = label()
    verified = replay(baseline, reference=baseline.head_sha256)
    assert verified.status is LabelStatus.SELF_REPORTED_REPLAY_ONLY
    assert verified.entries == baseline.entries
    assert verified.no_trade and not verified.qualifies_as_calibrated_model
    assert replay(baseline).status is LabelStatus.UNANCHORED_RESEARCH_ONLY
    assert replay(baseline, reference="f" * 64).status is (LabelStatus.DETACHED_REFERENCE_MISMATCH)
    assert replay(baseline, cost=D("30")).status is LabelStatus.ALTERED_RESEARCH_CHAIN
    tampered = replace(baseline.entries[1], entry_hash="f" * 64)
    assert (
        replay(
            replace(baseline, entries=(baseline.entries[0], tampered, baseline.entries[2])),
            reference=baseline.head_sha256,
        ).status
        is LabelStatus.ALTERED_RESEARCH_CHAIN
    )
    rows = forecasts()
    changed = replace(rows[0], recorded_at_ms=rows[0].recorded_at_ms + 1)
    assert replay(baseline, rows=(changed, *rows[1:])).status is (
        LabelStatus.ALTERED_RESEARCH_CHAIN
    )
    data = list(candles())
    data[2] = replace(data[2], close=D("100.5"))
    assert replay(baseline, data=tuple(data)).status is LabelStatus.ALTERED_RESEARCH_CHAIN


def test_missing_future_candle_and_gaps_are_rejected() -> None:
    rows = forecasts()
    last = replace(
        rows[0],
        signal_id="forecast_9999",
        observed_close_at_ms=candles()[-1].close_time_ms,
        recorded_at_ms=candles()[-1].close_time_ms + 1,
    )
    assert label(rows=(last,)).status is LabelStatus.MISSING_FUTURE_CANDLE
    data = candles()
    assert label(data=(*data[:3], *data[4:])).status is LabelStatus.INVALID_CANDLES
    assert label(evaluation=data[-1].close_time_ms).status is (LabelStatus.FUTURE_INFORMATION_RISK)


def test_forged_timing_order_duplicates_and_probabilities_fail_closed() -> None:
    rows = forecasts()
    assert label(rows=(rows[1], rows[0])).status is LabelStatus.FUTURE_INFORMATION_RISK
    assert label(rows=(rows[0], rows[0])).status is LabelStatus.INVALID_FORECAST
    backfilled = replace(rows[0], recorded_at_ms=rows[0].recorded_at_ms + STEP)
    assert label(rows=(backfilled,)).status is LabelStatus.FUTURE_INFORMATION_RISK
    early = replace(rows[0], recorded_at_ms=rows[0].observed_close_at_ms)
    assert label(rows=(early,)).status is LabelStatus.FUTURE_INFORMATION_RISK
    wrong = replace(rows[0], symbol="ETHUSDT")
    assert label(rows=(wrong,)).status is LabelStatus.INVALID_FORECAST
    forged = ProbabilityEstimate.model_construct(win=D("2"), loss=D("-1"), flat=D("0"))
    assert label(rows=(replace(rows[0], forecast=forged),)).status is (LabelStatus.INVALID_FORECAST)


def test_bad_candle_ohlc_cost_and_horizon_are_rejected() -> None:
    data = list(candles())
    data[0] = replace(data[0], high=D("99"))
    assert label(data=tuple(data)).status is LabelStatus.INVALID_CANDLES
    data = list(candles())
    data[0] = replace(data[0], symbol="ETHUSDT")
    assert label(data=tuple(data)).status is LabelStatus.INVALID_CANDLES
    assert label(cost=D("-1")).status is LabelStatus.INVALID_INPUT
    assert label(cost=D("1001")).status is LabelStatus.INVALID_INPUT
    assert label(cost=D("NaN")).status is LabelStatus.INVALID_INPUT
    assert label(band=D("201")).status is LabelStatus.INVALID_INPUT
    assert label(period=90_000).status is LabelStatus.INVALID_INPUT
    assert label(period=0).status is LabelStatus.INVALID_INPUT
    assert label(symbol="XRPUSDT").status is LabelStatus.INVALID_INPUT
