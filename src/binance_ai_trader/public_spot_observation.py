"""Verified PUBLIC-only Spot closed-feed + locally observed last-price sample.

The ticker/price response has NO exchange event timestamp. Timestamp fields
below are CLIENT receive times, not Binance trade time or an executable quote.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import (
    INTERVAL_MS,
    PUBLIC_MARKET_URL,
    SUPPORTED_SYMBOLS,
    Candle,
    MarketDataInvalid,
    MarketDataUnavailable,
    MarketRateLimited,
    PublicSpotMarketClient,
    _retry_after,
)

MAX_HTTP_OBSERVATION_MS = 3_000
MAX_SPOT_SAMPLE_AGE_MS = 3_000
DEFAULT_MAX_DEVIATION_PCT = Decimal("5")


@dataclass(frozen=True, slots=True)
class PublicSpotObservation:
    """Measured local public data sample. No independent Binance tick timestamp."""

    symbol: str
    interval: str
    closed_candles: tuple[Candle, ...]
    reference_price: Decimal
    requested_at_ms: int
    received_at_ms: int
    source: str = "binance_public_spot_ticker_price"
    exchange_price_event_time_ms: None = None

    @property
    def signal_close_time_ms(self) -> int:
        return self.closed_candles[-1].close_time_ms


class PublicSpotObservationClient:
    """Bounded synchronous Spot REST queries: GET klines then GET ticker/price.

    No caller-provided 'HEALTHY' shortcut; both responses MUST succeed.
    No private endpoint, API key, environment proxies, redirects or retries.
    A MockTransport and fake clock are supported exclusively for offline tests.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._candles = PublicSpotMarketClient(transport=transport, clock_ms=self._clock_ms)

    def now_ms(self) -> int:
        clock = self._clock_ms()
        if type(clock) is not int or clock <= 0:
            raise MarketDataInvalid("Invalid local UTC observation clock")
        return clock

    def observe(
        self, *, symbol: str, interval: str, limit: int = 100
    ) -> PublicSpotObservation:
        if symbol not in SUPPORTED_SYMBOLS or interval not in INTERVAL_MS:
            raise ValueError("Unsupported public Spot market")
        if type(limit) is not int or not 50 <= limit <= 1000:
            raise ValueError("Invalid bounded market history size")
        # Order matters: signals can only use fully closed historical candles
        # fetched BEFORE the independently sampled last-price ticker.
        history = tuple(
            self._candles.fetch_closed_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        )
        requested = self.now_ms()
        quality = evaluate_closed_spot_feed(
            history, symbol=symbol, interval=interval,
            now_ms=requested, min_history=50,
        )
        step = INTERVAL_MS[interval]
        if quality.status is not FeedStatus.READY:
            raise MarketDataInvalid("public_feed_" + quality.status.value)
        if requested - history[-1].close_time_ms > step:
            raise MarketDataInvalid("public_feed_stale_for_action")
        try:
            with httpx.Client(
                base_url=PUBLIC_MARKET_URL,
                transport=self._transport,
                timeout=httpx.Timeout(3.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                reply = client.get("/api/v3/ticker/price", params={"symbol": symbol})
        except httpx.RequestError as exc:
            raise MarketDataUnavailable("Public Spot ticker network failure") from exc
        received = self.now_ms()
        if not 0 <= received - requested <= MAX_HTTP_OBSERVATION_MS:
            raise MarketDataInvalid("ticker_observation_timeout_or_clock_skew")
        if (
            received <= history[-1].close_time_ms
            or received - history[-1].close_time_ms > step
        ):
            raise MarketDataInvalid("ticker_sample_crossed_candle_boundary")
        if reply.status_code in (418, 429):
            raise MarketRateLimited(reply.status_code, _retry_after(reply.headers.get("Retry-After")))
        if reply.status_code != 200:
            raise MarketDataUnavailable("Unexpected public ticker HTTP response")
        try:
            body: Any = reply.json()
        except ValueError as exc:
            raise MarketDataInvalid("Malformed public ticker JSON") from exc
        if (
            not isinstance(body, dict)
            or set(body) != {"symbol", "price"}
            or body["symbol"] != symbol
            or not isinstance(body["price"], str)
        ):
            raise MarketDataInvalid("Wrong public ticker payload/symbol")
        try:
            price = Decimal(body["price"])
        except InvalidOperation as exc:
            raise MarketDataInvalid("Non-numeric public ticker price") from exc
        if not price.is_finite() or price <= 0:
            raise MarketDataInvalid("Non-positive or nonfinite public ticker price")
        return PublicSpotObservation(
            symbol=symbol,
            interval=interval,
            closed_candles=history,
            reference_price=price,
            requested_at_ms=requested,
            received_at_ms=received,
        )


def validate_public_observation(
    sample: PublicSpotObservation,
    *,
    now_ms: int,
    max_deviation_pct: Decimal = DEFAULT_MAX_DEVIATION_PCT,
) -> None:
    """Validate a quote and its historical candle context again before PAPER use."""

    if (
        not isinstance(sample, PublicSpotObservation)
        or sample.symbol not in SUPPORTED_SYMBOLS
        or sample.interval not in INTERVAL_MS
        or sample.source != "binance_public_spot_ticker_price"
        or sample.exchange_price_event_time_ms is not None
        or type(now_ms) is not int
        or type(sample.requested_at_ms) is not int
        or type(sample.received_at_ms) is not int
        or not isinstance(sample.reference_price, Decimal)
        or not sample.reference_price.is_finite()
        or sample.reference_price <= 0
        or not isinstance(max_deviation_pct, Decimal)
        or not max_deviation_pct.is_finite()
        or not Decimal("0") < max_deviation_pct <= Decimal("10")
    ):
        raise MarketDataInvalid("invalid_quote_observation")
    if (
        sample.received_at_ms < sample.requested_at_ms
        or sample.received_at_ms - sample.requested_at_ms > MAX_HTTP_OBSERVATION_MS
        or now_ms < sample.received_at_ms
        or now_ms - sample.received_at_ms > MAX_SPOT_SAMPLE_AGE_MS
    ):
        raise MarketDataInvalid("stale_or_future_spot_quote")
    quality = evaluate_closed_spot_feed(
        sample.closed_candles, symbol=sample.symbol, interval=sample.interval,
        now_ms=now_ms, min_history=50,
    )
    if quality.status is not FeedStatus.READY:
        raise MarketDataInvalid("invalid_quote_history_" + quality.status.value)
    step = INTERVAL_MS[sample.interval]
    last = sample.closed_candles[-1]
    if (
        not last.close_time_ms < sample.requested_at_ms
        or sample.received_at_ms - last.close_time_ms > step
        or now_ms - last.close_time_ms > step
    ):
        raise MarketDataInvalid("quote_not_after_latest_closed_candle")
    change = abs(sample.reference_price - last.close) * Decimal("100") / last.close
    if change > max_deviation_pct:
        raise MarketDataInvalid("reference_price_deviation_limit")
