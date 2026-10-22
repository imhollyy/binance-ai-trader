"""Deterministic 3-timeframe PAPER context: negative data/clock tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from binance_ai_trader.ai_constitution import MarketRegime
from binance_ai_trader.ai_market_regime import TimeframeHistory, analyze_spot_timeframes
from binance_ai_trader.market_data import INTERVAL_MS, Candle

D = Decimal
NOW = 1_800_000_000_000


def history(interval: str, *, trend: int = 1, n: int = 50) -> TimeframeHistory:
    step = INTERVAL_MS[interval]
    final_open = ((NOW - 1) // step) * step
    candles: list[Candle] = []
    for idx in range(n):
        open_ms = final_open - (n - idx - 1) * step
        close = D("100") + D(idx * trend) / 10
        candles.append(
            Candle(
                "BTCUSDT",
                interval,
                open_ms,
                open_ms + step - 1,
                close,
                close + D("0.1"),
                close - D("0.1"),
                close,
                D("10"),
            )
        )
    return TimeframeHistory(interval, tuple(candles))


def inputs(*, higher_trend: int = 1) -> tuple[TimeframeHistory, ...]:
    return (
        history("1m"),
        history("5m"),
        history("15m", trend=higher_trend),
    )


def analyze(feeds: tuple[TimeframeHistory, ...], *, as_of: int = NOW):
    return analyze_spot_timeframes(
        feeds,
        symbol="BTCUSDT",
        execution_timeframe="1m",
        context_timeframe="5m",
        higher_timeframe="15m",
        now_ms=as_of,
    )


def test_three_closed_feeds_agree_without_future_data() -> None:
    result = analyze(inputs())
    assert result.data_ok
    assert result.regime is MarketRegime.UPTREND
    assert result.execution_close_at_ms == NOW - 1
    assert result.execution_close == D("104.9")


def test_conflicting_higher_trend_is_not_an_entry_signal() -> None:
    result = analyze(inputs(higher_trend=-1))
    assert not result.data_ok
    assert result.regime is MarketRegime.UNCERTAIN
    assert result.reason_code == "REGIME_UNCERTAIN"


@pytest.mark.parametrize("interval", ["1m", "5m", "15m"])
def test_missing_gap_or_stale_any_timeframe_blocks(interval: str) -> None:
    feeds = list(inputs())
    pos = [x.interval for x in feeds].index(interval)
    gaps = feeds[:]
    gaps[pos] = replace(feeds[pos], candles=feeds[pos].candles[:20] + feeds[pos].candles[21:])
    assert analyze(tuple(gaps)).reason_code == "DATA_QUALITY_FAILURE"
    short = feeds[:]
    short[pos] = replace(feeds[pos], candles=feeds[pos].candles[-10:])
    assert analyze(tuple(short)).reason_code == "DATA_QUALITY_FAILURE"
    stale = feeds[:]
    stale[pos] = replace(
        feeds[pos],
        candles=tuple(
            replace(
                c,
                open_time_ms=c.open_time_ms - INTERVAL_MS[interval] * 4,
                close_time_ms=c.close_time_ms - INTERVAL_MS[interval] * 4,
            )
            for c in feeds[pos].candles
        ),
    )
    assert analyze(tuple(stale)).reason_code == "DATA_QUALITY_FAILURE"


def test_live_unclosed_candle_not_valid_for_analysis() -> None:
    feeds = list(inputs())
    prior = feeds[0].candles
    next_open = NOW
    contaminated = Candle(
        "BTCUSDT",
        "1m",
        next_open,
        next_open + 59_999,
        D("100"),
        D("101"),
        D("99"),
        D("100"),
        D("1"),
    )
    feeds[0] = replace(feeds[0], candles=(*prior, contaminated))
    assert analyze(tuple(feeds)).reason_code == "DATA_QUALITY_FAILURE"


def test_missing_or_duplicate_interval_is_rejected() -> None:
    assert analyze(inputs()[:2]).reason_code == "DATA_QUALITY_FAILURE"
    assert analyze((history("1m"), history("5m"), history("5m"))).reason_code == (
        "DATA_QUALITY_FAILURE"
    )


def test_invalid_timeframe_configuration_cannot_be_guessed() -> None:
    with pytest.raises(ValueError, match="invalid_mtf_configuration"):
        analyze_spot_timeframes(
            inputs(),
            symbol="BTCUSDT",
            execution_timeframe="1m",
            context_timeframe="1m",
            higher_timeframe="15m",
            now_ms=NOW,
        )
    with pytest.raises(ValueError, match="invalid_mtf_configuration"):
        analyze_spot_timeframes(
            inputs(),
            symbol="BTCUSDT",
            execution_timeframe="1m",
            context_timeframe="5m",
            higher_timeframe="4h",
            now_ms=NOW,
        )


def test_future_clock_or_price_inconsistency_is_not_accepted() -> None:
    feeds = list(inputs())
    bad = feeds[0].candles
    feeds[0] = replace(feeds[0], candles=(*bad[:-1], replace(bad[-1], low=D("200"))))
    assert analyze(tuple(feeds)).reason_code == "DATA_QUALITY_FAILURE"
