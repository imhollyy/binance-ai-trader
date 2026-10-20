"""Fail-closed market history quality gate for future PAPER signal evaluators.

No exchange, background worker, account, order or external I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle


class FeedStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE = "stale"
    GAPS = "gaps"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FeedQuality:
    status: FeedStatus
    checked_candles: int
    latest_close_time_ms: int | None
    lag_ms: int | None
    missing_intervals: int = 0

    @property
    def usable_for_paper_signals(self) -> bool:
        return self.status is FeedStatus.READY


def evaluate_closed_spot_feed(
    candles: Sequence[Candle],
    *,
    symbol: str,
    interval: str,
    now_ms: int,
    min_history: int = 50,
    max_staleness_intervals: int = 2,
) -> FeedQuality:
    """Accept strictly consecutive, fully closed public Spot candles ONLY.

    The gate prevents missing, stale, invalid or still-open candles from
    being treated as a reliable input to a subsequent paper strategy.
    It does not download data, produce any signal or send any orders.
    """

    if symbol not in SUPPORTED_SYMBOLS or interval not in INTERVAL_MS:
        raise ValueError("Unsupported Spot symbol or interval")
    if (
        type(now_ms) is not int
        or now_ms <= 0
        or type(min_history) is not int
        or not 2 <= min_history <= 1000
        or type(max_staleness_intervals) is not int
        or not 1 <= max_staleness_intervals <= 10
    ):
        raise ValueError("Invalid quality gate bounds or clock")
    if len(candles) > 1000:
        raise ValueError("Quality check must be bounded to 1000 candles")
    if not candles:
        return FeedQuality(FeedStatus.EMPTY, 0, None, None)
    step = INTERVAL_MS[interval]
    latest: int | None = None
    missing = 0
    for candle in candles:
        if (
            candle.symbol != symbol
            or candle.interval != interval
            or type(candle.open_time_ms) is not int
            or type(candle.close_time_ms) is not int
            or candle.open_time_ms < 0
            or candle.open_time_ms % step != 0
            or candle.close_time_ms != candle.open_time_ms + step - 1
            or candle.close_time_ms >= now_ms
        ):
            return FeedQuality(FeedStatus.INVALID, len(candles), latest, None)
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if (
            any(not isinstance(v, Decimal) or not v.is_finite() for v in values)
            or min(candle.open, candle.high, candle.low, candle.close) <= 0
            or candle.volume < 0
            or candle.low > candle.high
            or not candle.low <= candle.open <= candle.high
            or not candle.low <= candle.close <= candle.high
        ):
            return FeedQuality(FeedStatus.INVALID, len(candles), latest, None)
        if latest is not None:
            previous_open = latest - step + 1
            difference = candle.open_time_ms - previous_open
            if difference <= 0 or difference % step:
                return FeedQuality(FeedStatus.INVALID, len(candles), latest, None)
            missing += difference // step - 1
        latest = candle.close_time_ms

    assert latest is not None
    lag_ms = now_ms - latest
    if missing:
        return FeedQuality(FeedStatus.GAPS, len(candles), latest, lag_ms, missing)
    if lag_ms > step * max_staleness_intervals:
        return FeedQuality(FeedStatus.STALE, len(candles), latest, lag_ms)
    if len(candles) < min_history:
        return FeedQuality(FeedStatus.INSUFFICIENT_HISTORY, len(candles), latest, lag_ms)
    return FeedQuality(FeedStatus.READY, len(candles), latest, lag_ms)
