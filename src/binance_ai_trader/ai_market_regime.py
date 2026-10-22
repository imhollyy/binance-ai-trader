"""Closed-candle, multi-timeframe market-regime context for PAPER analysis only.

This deterministic classifier is descriptive, NOT an AI prediction or trading
authorization. It consumes pre-collected public Spot candles; no HTTP/DB/orders.
It never fills gaps, guesses unavailable order flow, or substitutes a chart image.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from binance_ai_trader.ai_constitution import MarketRegime
from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle

MIN_CANDLES = 30
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class TimeframeHistory:
    interval: str
    candles: tuple[Candle, ...]


@dataclass(frozen=True, slots=True)
class MultiTimeframeAssessment:
    symbol: str
    execution_timeframe: str
    context_timeframe: str
    higher_timeframe: str
    regime: MarketRegime
    reason_code: str
    execution_close_at_ms: int | None
    execution_close: Decimal | None

    @property
    def data_ok(self) -> bool:
        return self.reason_code == "MTF_READY"


def _direction(candles: tuple[Candle, ...]) -> int:
    """Two adjacent closed ten-bar blocks; no future or open-candle access."""
    recent = candles[-10:]
    prior = candles[-20:-10]
    recent_mean = sum((c.close for c in recent), Decimal(0)) / 10
    prior_mean = sum((c.close for c in prior), Decimal(0)) / 10
    if recent_mean > prior_mean and recent[-1].close > prior[-1].close:
        return 1
    if recent_mean < prior_mean and recent[-1].close < prior[-1].close:
        return -1
    return 0


def _volatility_spike(candles: tuple[Candle, ...]) -> bool:
    """Relative closed-bar range expansion, not a calibrated risk threshold."""
    previous = candles[-20:-10]
    recent = candles[-10:]
    old = sum(((c.high - c.low) / c.close for c in previous), Decimal(0)) / 10
    latest = sum(((c.high - c.low) / c.close for c in recent), Decimal(0)) / 10
    return old > 0 and latest > old * 3


def _blocked(
    symbol: str, execution: str, context: str, higher: str, reason: str
) -> MultiTimeframeAssessment:
    return MultiTimeframeAssessment(
        symbol, execution, context, higher, MarketRegime.UNCERTAIN, reason, None, None
    )


def analyze_spot_timeframes(
    histories: Sequence[TimeframeHistory],
    *,
    symbol: str,
    execution_timeframe: str,
    context_timeframe: str,
    higher_timeframe: str,
    now_ms: int,
) -> MultiTimeframeAssessment:
    """Require three DISTINCT caller-selected, ascending, fully closed feeds.

    Only Binance public Spot intervals currently supported by this repository
    are accepted. Never silently infer an absent higher timeframe. Unknown,
    stale, inconsistent or insufficient data produce a non-tradable result.
    """
    timeframes = (execution_timeframe, context_timeframe, higher_timeframe)
    if (
        symbol not in SUPPORTED_SYMBOLS
        or any(tf not in INTERVAL_MS for tf in timeframes)
        or len(set(timeframes)) != 3
        or list(map(INTERVAL_MS.__getitem__, timeframes))
        != sorted(map(INTERVAL_MS.__getitem__, timeframes))
        or type(now_ms) is not int
        or now_ms <= 0
    ):
        raise ValueError("invalid_mtf_configuration")
    if len(histories) != 3 or any(not isinstance(h, TimeframeHistory) for h in histories):
        return _blocked(symbol, *timeframes, "DATA_QUALITY_FAILURE")
    by_interval = {h.interval: h.candles for h in histories}
    if len(by_interval) != 3 or set(by_interval) != set(timeframes):
        return _blocked(symbol, *timeframes, "DATA_QUALITY_FAILURE")
    directions: list[int] = []
    spike = False
    for timeframe in timeframes:
        candles = by_interval[timeframe]
        if not isinstance(candles, tuple):
            return _blocked(symbol, *timeframes, "DATA_QUALITY_FAILURE")
        try:
            check = evaluate_closed_spot_feed(
                candles,
                symbol=symbol,
                interval=timeframe,
                now_ms=now_ms,
                min_history=MIN_CANDLES,
            )
        except (ValueError, TypeError, AttributeError):
            return _blocked(symbol, *timeframes, "DATA_QUALITY_FAILURE")
        if check.status is not FeedStatus.READY:
            return _blocked(symbol, *timeframes, "DATA_QUALITY_FAILURE")
        directions.append(_direction(candles))
        spike = spike or _volatility_spike(candles)
    execution = by_interval[execution_timeframe][-1]
    if spike:
        regime = MarketRegime.HIGH_VOLATILITY
    elif directions == [1, 1, 1]:
        higher_candles = by_interval[higher_timeframe]
        vol_prev = sum((c.volume for c in higher_candles[-20:-10]), Decimal(0))
        vol_now = sum((c.volume for c in higher_candles[-10:]), Decimal(0))
        regime = (
            MarketRegime.STRONG_UPTREND
            if vol_prev > 0 and vol_now > vol_prev * Decimal("1.5")
            else MarketRegime.UPTREND
        )
    elif directions == [-1, -1, -1]:
        regime = MarketRegime.DOWNTREND
    elif directions == [0, 0, 0]:
        regime = MarketRegime.RANGE
    else:
        regime = MarketRegime.UNCERTAIN
    return MultiTimeframeAssessment(
        symbol,
        execution_timeframe,
        context_timeframe,
        higher_timeframe,
        regime,
        "MTF_READY" if regime is not MarketRegime.UNCERTAIN else "REGIME_UNCERTAIN",
        execution.close_time_ms,
        execution.close,
    )
