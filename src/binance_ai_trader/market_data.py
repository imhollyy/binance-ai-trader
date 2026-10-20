"""Fail-closed client for PUBLIC Binance Global Spot market candles only.

This module has NO authenticated client, account, order or trading endpoints.
Official public-data host: https://data-api.binance.vision
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

PUBLIC_MARKET_URL = "https://data-api.binance.vision"
SUPPORTED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


class MarketDataError(Exception):
    """Exchange data is not safe to use for analysis."""


class MarketDataUnavailable(MarketDataError):
    """The public endpoint is unavailable or returned an unexpected response."""


class MarketRateLimited(MarketDataError):
    """Caller must stop polling until the exchange has lifted its limit."""

    def __init__(self, status_code: int, retry_after_seconds: int | None) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Binance public API HTTP {status_code}; polling paused")


class MarketDataInvalid(MarketDataError):
    """Exchange-supplied row fails strict validation."""


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def open_time_utc(self) -> int:
        """UTC Unix milliseconds; timezone-independent."""

        return self.open_time_ms


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise MarketDataInvalid(f"Invalid {name} type")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise MarketDataInvalid(f"Invalid {name} number") from exc
    if not number.is_finite():
        raise MarketDataInvalid(f"Non-finite {name}")
    return number


def _parse_candle(row: object, symbol: str, interval: str) -> Candle:
    if not isinstance(row, list) or len(row) != 12:
        raise MarketDataInvalid("Kline must have exactly 12 fields")
    millis = INTERVAL_MS[interval]
    open_ms, close_ms = row[0], row[6]
    if type(open_ms) is not int or type(close_ms) is not int:
        raise MarketDataInvalid("Kline timestamps must be integers")
    if open_ms < 0 or open_ms % millis != 0 or close_ms != open_ms + millis - 1:
        raise MarketDataInvalid("Kline timestamps are inconsistent")

    op = _decimal(row[1], "open")
    hi = _decimal(row[2], "high")
    lo = _decimal(row[3], "low")
    cl = _decimal(row[4], "close")
    vol = _decimal(row[5], "volume")
    if min(op, hi, lo, cl) <= 0 or vol < 0:
        raise MarketDataInvalid("Prices must be positive, volume nonnegative")
    if lo > hi or not (lo <= op <= hi) or not (lo <= cl <= hi):
        raise MarketDataInvalid("OHLC prices violate high/low bounds")
    return Candle(symbol, interval, open_ms, close_ms, op, hi, lo, cl, vol)


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class PublicSpotMarketClient:
    """Synchronous, bounded, read-only public REST adapter.

    No auth headers, arbitrary URLs, trading endpoints, automatic retries,
    redirects or environment proxy settings.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def fetch_closed_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time_ms: int | None = None,
    ) -> list[Candle]:
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("Unsupported public Spot symbol")
        if interval not in INTERVAL_MS:
            raise ValueError("Unsupported candle interval")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if start_time_ms is not None and (
            type(start_time_ms) is not int or start_time_ms < 0
        ):
            raise ValueError("start_time_ms must be a nonnegative Unix millisecond integer")

        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        try:
            with httpx.Client(
                base_url=PUBLIC_MARKET_URL,
                transport=self._transport,
                timeout=httpx.Timeout(8.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get("/api/v3/klines", params=params)
        except httpx.RequestError as exc:
            raise MarketDataUnavailable("Public Spot market network request failed") from exc

        if response.status_code in (418, 429):
            raise MarketRateLimited(
                response.status_code, _retry_after(response.headers.get("Retry-After"))
            )
        if response.status_code != 200:
            raise MarketDataUnavailable(f"Unexpected public Spot HTTP {response.status_code}")
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise MarketDataInvalid("Malformed Binance kline JSON") from exc
        if not isinstance(payload, list) or len(payload) > limit:
            raise MarketDataInvalid("Unexpected kline array shape or length")

        current_ms = self._clock_ms()
        if type(current_ms) is not int or current_ms <= 0:
            raise MarketDataInvalid("Current time unavailable")
        candles: list[Candle] = []
        previous_open_time: int | None = None
        for entry in payload:
            candle = _parse_candle(entry, symbol, interval)
            if previous_open_time is not None and candle.open_time_ms <= previous_open_time:
                raise MarketDataInvalid("Duplicate or unordered candles")
            previous_open_time = candle.open_time_ms
            # The most recent kline is not trustworthy until the interval has closed.
            if candle.close_time_ms < current_ms:
                candles.append(candle)
        return candles
