"""Public Binance Spot quote observations use only measured LOCAL receive times."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from binance_ai_trader.market_data import (
    MarketDataInvalid,
    MarketDataUnavailable,
    MarketRateLimited,
)
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservationClient,
    validate_public_observation,
)

START = 1_800_000_180_000
NOW = START + 50


def kline(ms: int, close: str = "100") -> list[object]:
    return [
        ms,
        "100",
        "110",
        "90",
        close,
        "20",
        ms + 59_999,
        "0",
        1,
        "0",
        "0",
        "0",
    ]


def client(
    *,
    status: int = 200,
    payload: object = None,
    malformed_kline: bool = False,
    clock_overrun: int = 0,
) -> tuple[PublicSpotObservationClient, list[str]]:
    seen: list[str] = []
    clock = [NOW]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert request.url.path in ("/api/v3/klines", "/api/v3/ticker/price")
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        if request.url.path == "/api/v3/klines":
            start = START - 60 * 60_000
            rows = [kline(start + i * 60_000) for i in range(60)]
            if malformed_kline:
                rows[25][6] = int(rows[25][6]) + 60_000
            return httpx.Response(200, json=rows)
        clock[0] += clock_overrun
        data = payload if payload is not None else {"symbol": "BTCUSDT", "price": "100.5"}
        return httpx.Response(status, json=data, headers={"Retry-After": "99"})

    return (
        PublicSpotObservationClient(
            transport=httpx.MockTransport(handler),
            clock_ms=lambda: clock[0],
        ),
        seen,
    )


def test_verified_quote_after_closed_prefix_with_no_exchange_timestamp() -> None:
    observer, paths = client()
    observed = observer.observe(symbol="BTCUSDT", interval="1m")
    assert paths == ["/api/v3/klines", "/api/v3/ticker/price"]
    assert observed.symbol == "BTCUSDT"
    assert observed.reference_price == Decimal("100.5")
    assert observed.received_at_ms >= observed.requested_at_ms > observed.signal_close_time_ms
    assert observed.exchange_price_event_time_ms is None
    validate_public_observation(observed, now_ms=observer.now_ms())
    assert len(observed.closed_candles) == 60


@pytest.mark.parametrize("status", [418, 429])
def test_quote_exchange_restrictions_fail_without_retry(status: int) -> None:
    observer, paths = client(status=status)
    with pytest.raises(MarketRateLimited) as exc:
        observer.observe(symbol="BTCUSDT", interval="1m")
    assert exc.value.status_code == status
    assert exc.value.retry_after_seconds == 99
    assert paths == ["/api/v3/klines", "/api/v3/ticker/price"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbol": "ETHUSDT", "price": "100"},
        {"symbol": "BTCUSDT", "price": "0"},
        {"symbol": "BTCUSDT", "price": "-1"},
        {"symbol": "BTCUSDT", "price": "NaN"},
        {"symbol": "BTCUSDT", "price": "Infinity"},
        {"symbol": "BTCUSDT", "price": "abc"},
        {"symbol": "BTCUSDT", "price": 100},
        [{"symbol": "BTCUSDT", "price": "100"}],
        {"symbol": "BTCUSDT", "price": "100", "timestamp": 123},
    ],
)
def test_quote_invalid_or_spoofed_payload_blocks(payload: object) -> None:
    observer, _ = client(payload=payload)
    with pytest.raises(MarketDataInvalid):
        observer.observe(symbol="BTCUSDT", interval="1m")


def test_future_stale_and_manipulated_quotes_block() -> None:
    observer, _ = client()
    sample = observer.observe(symbol="BTCUSDT", interval="1m")
    for bogus in (
        replace(sample, reference_price=Decimal("110")),
        replace(sample, received_at_ms=sample.received_at_ms + 100),
        replace(sample, requested_at_ms=sample.signal_close_time_ms),
        replace(sample, source="arbitrary_client_claim"),
    ):
        with pytest.raises(MarketDataInvalid):
            validate_public_observation(bogus, now_ms=observer.now_ms())
    with pytest.raises(MarketDataInvalid):
        validate_public_observation(sample, now_ms=observer.now_ms() + 4000)


def test_network_timeout_invalid_feed_delayed_response_or_bad_http() -> None:
    observer, _ = client(malformed_kline=True)
    with pytest.raises(MarketDataInvalid):
        observer.observe(symbol="BTCUSDT", interval="1m")
    observer, _ = client(clock_overrun=4_000)
    with pytest.raises(MarketDataInvalid, match="timeout"):
        observer.observe(symbol="BTCUSDT", interval="1m")
    observer, _ = client(status=503)
    with pytest.raises(MarketDataUnavailable):
        observer.observe(symbol="BTCUSDT", interval="1m")

    def fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/klines":
            return httpx.Response(200, json=[kline(START - (60 - i) * 60_000) for i in range(60)])
        raise httpx.ReadTimeout("simulated", request=request)

    observer = PublicSpotObservationClient(
        transport=httpx.MockTransport(fail), clock_ms=lambda: NOW
    )
    with pytest.raises(MarketDataUnavailable):
        observer.observe(symbol="BTCUSDT", interval="1m")


def test_public_history_bounds_and_wrong_symbol_are_rejected_before_io() -> None:
    observer, paths = client()
    for symbol, interval, limit in (
        ("XRPUSDT", "1m", 100),
        ("BTCUSDT", "1s", 100),
        ("BTCUSDT", "1m", 49),
        ("BTCUSDT", "1m", 1001),
    ):
        with pytest.raises(ValueError):
            observer.observe(symbol=symbol, interval=interval, limit=limit)
    assert paths == []
