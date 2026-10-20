"""Offline tests of the Binance PUBLIC market-data client (no API key)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from binance_ai_trader.market_data import (
    MarketDataInvalid,
    MarketDataUnavailable,
    MarketRateLimited,
    PublicSpotMarketClient,
)

NOW_MS = 1_800_000_180_000


def kline(open_ms: int, *, close: str = "106.25") -> list[object]:
    return [
        open_ms, "100", "110", "90", close, "12.5000", open_ms + 59_999,
        "0", 1, "0", "0", "0",
    ]


def create_client(
    payload: object, *, status: int = 200, headers: dict[str, str] | None = None
) -> PublicSpotMarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert request.url.path == "/api/v3/klines"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        return httpx.Response(status, json=payload, headers=headers)

    return PublicSpotMarketClient(
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW_MS,
    )


def test_parses_closed_candle_with_decimal_precision() -> None:
    client = create_client([kline(1_799_999_940_000)])
    candles = client.fetch_closed_klines(symbol="BTCUSDT", interval="1m", limit=10)
    assert len(candles) == 1
    assert candles[0].close == Decimal("106.25")
    assert candles[0].volume == Decimal("12.5000")
    assert candles[0].open_time_ms == 1_799_999_940_000


def test_excludes_unfinished_last_candle() -> None:
    client = create_client([
        kline(1_799_999_940_000), kline(1_800_000_180_000)
    ])
    candles = client.fetch_closed_klines(symbol="ETHUSDT", interval="1m")
    assert len(candles) == 1


@pytest.mark.parametrize("symbol", ["BTCUSD", "../ETHUSDT", "DOGEUSDT", ""])
def test_rejects_unapproved_symbols(symbol: str) -> None:
    with pytest.raises(ValueError, match="symbol"):
        create_client([]).fetch_closed_klines(symbol=symbol, interval="1m")


@pytest.mark.parametrize("interval", ["1s", "1h", "../1m", ""])
def test_rejects_unapproved_intervals(interval: str) -> None:
    with pytest.raises(ValueError, match="interval"):
        create_client([]).fetch_closed_klines(symbol="BTCUSDT", interval=interval)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_rejects_bad_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        create_client([]).fetch_closed_klines(
            symbol="BTCUSDT", interval="1m", limit=limit  # type: ignore[arg-type]
        )


def test_rejects_invalid_ohlc() -> None:
    bad = kline(1_799_999_940_000, close="120")
    with pytest.raises(MarketDataInvalid, match="OHLC"):
        create_client([bad]).fetch_closed_klines(symbol="BTCUSDT", interval="1m")


def test_rejects_nonfinite_candle() -> None:
    bad = kline(1_799_999_940_000, close="NaN")
    with pytest.raises(MarketDataInvalid, match="Non-finite"):
        create_client([bad]).fetch_closed_klines(symbol="BTCUSDT", interval="1m")


def test_rejects_duplicates_and_nonascending_rows() -> None:
    rows = [kline(1_799_999_940_000), kline(1_799_999_940_000)]
    with pytest.raises(MarketDataInvalid, match="Duplicate"):
        create_client(rows).fetch_closed_klines(symbol="BTCUSDT", interval="1m")


def test_rate_limit_429_is_not_retried() -> None:
    count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(429, json={}, headers={"Retry-After": "45"})

    client = PublicSpotMarketClient(
        transport=httpx.MockTransport(handler), clock_ms=lambda: NOW_MS
    )
    with pytest.raises(MarketRateLimited) as caught:
        client.fetch_closed_klines(symbol="BTCUSDT", interval="1m")
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 45
    assert count == 1


def test_ip_ban_418_exposes_cooldown() -> None:
    with pytest.raises(MarketRateLimited) as caught:
        create_client({}, status=418, headers={"Retry-After": "120"}).fetch_closed_klines(
            symbol="BTCUSDT", interval="1m"
        )
    assert caught.value.retry_after_seconds == 120


@pytest.mark.parametrize("response", [123, {"code": -1121}, "not a list"])
def test_invalid_response_fails_closed(response: object) -> None:
    with pytest.raises(MarketDataInvalid):
        create_client(response).fetch_closed_klines(symbol="BTCUSDT", interval="1m")


def test_5xx_fails_closed() -> None:
    with pytest.raises(MarketDataUnavailable):
        create_client({}, status=503).fetch_closed_klines(symbol="BTCUSDT", interval="1m")


def test_transport_network_failure_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = PublicSpotMarketClient(transport=httpx.MockTransport(handler))
    with pytest.raises(MarketDataUnavailable):
        client.fetch_closed_klines(symbol="ETHUSDT", interval="5m")
