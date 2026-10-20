"""Fail-closed Spot market feed checks on deterministic historical fixtures."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import Candle

D = Decimal


def fixture(open_ms: int, interval: str = "1m") -> Candle:
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    return Candle(
        symbol="BTCUSDT",
        interval=interval,
        open_time_ms=open_ms,
        close_time_ms=open_ms + step - 1,
        open=D("100"),
        high=D("110"),
        low=D("90"),
        close=D("105"),
        volume=D("12.5"),
    )


def test_ready_requires_sufficient_fresh_contiguous_closed_candles() -> None:
    candles = [fixture(t * 60_000) for t in range(50)]
    result = evaluate_closed_spot_feed(
        candles, symbol="BTCUSDT", interval="1m", now_ms=50 * 60_000
    )
    assert result.status is FeedStatus.READY
    assert result.usable_for_paper_signals is True
    assert result.missing_intervals == 0
    assert result.lag_ms == 1


def test_empty_feed_is_not_ready() -> None:
    result = evaluate_closed_spot_feed(
        [], symbol="BTCUSDT", interval="1m", now_ms=180_000
    )
    assert result.status is FeedStatus.EMPTY
    assert result.usable_for_paper_signals is False


def test_lookback_warmup_is_required() -> None:
    result = evaluate_closed_spot_feed(
        [fixture(0), fixture(60_000)],
        symbol="BTCUSDT",
        interval="1m",
        now_ms=120_000,
    )
    assert result.status is FeedStatus.INSUFFICIENT_HISTORY


def test_missing_intervals_block_future_signals() -> None:
    result = evaluate_closed_spot_feed(
        [fixture(0), fixture(120_000)],
        symbol="BTCUSDT",
        interval="1m",
        now_ms=180_000,
        min_history=2,
    )
    assert result.status is FeedStatus.GAPS
    assert result.missing_intervals == 1
    assert result.usable_for_paper_signals is False


def test_stale_market_data_is_not_ready() -> None:
    result = evaluate_closed_spot_feed(
        [fixture(0), fixture(60_000)],
        symbol="BTCUSDT",
        interval="1m",
        now_ms=240_001,
        min_history=2,
    )
    assert result.status is FeedStatus.STALE


@pytest.mark.parametrize(
    "bad",
    [
        replace(fixture(60_000), close_time_ms=120_000),
        replace(fixture(60_000), open_time_ms=65_000),
        replace(fixture(60_000), symbol="ETHUSDT"),
        replace(fixture(60_000), close=D("Infinity")),
        replace(fixture(60_000), volume=D("-1")),
        replace(fixture(60_000), open=D("200")),
    ],
)
def test_invalid_or_open_candle_blocks_signals(bad: Candle) -> None:
    result = evaluate_closed_spot_feed(
        [fixture(0), bad],
        symbol="BTCUSDT",
        interval="1m",
        now_ms=120_000,
        min_history=2,
    )
    assert result.status is FeedStatus.INVALID
    assert result.usable_for_paper_signals is False


def test_duplicates_and_reversed_candles_fail() -> None:
    for data in ([fixture(0), fixture(0)], [fixture(60_000), fixture(0)]):
        result = evaluate_closed_spot_feed(
            data, symbol="BTCUSDT", interval="1m", now_ms=180_000, min_history=2
        )
        assert result.status is FeedStatus.INVALID


@pytest.mark.parametrize("interval", ["1m", "5m", "15m"])
def test_each_supported_interval(interval: str) -> None:
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    candles = [fixture(i * step, interval) for i in range(2)]
    result = evaluate_closed_spot_feed(
        candles, symbol="BTCUSDT", interval=interval, now_ms=2 * step, min_history=2
    )
    assert result.status is FeedStatus.READY


@pytest.mark.parametrize("min_history", [0, 1, 1001, True])
def test_rejects_unsafe_window_size(min_history: int) -> None:
    with pytest.raises(ValueError):
        evaluate_closed_spot_feed(
            [], symbol="BTCUSDT", interval="1m", now_ms=120_000,
            min_history=min_history,
        )


def test_does_not_use_current_unclosed_candle() -> None:
    candles = [fixture(0), fixture(60_000)]
    result = evaluate_closed_spot_feed(
        candles, symbol="BTCUSDT", interval="1m", now_ms=119_999, min_history=2
    )
    assert result.status is FeedStatus.INVALID
