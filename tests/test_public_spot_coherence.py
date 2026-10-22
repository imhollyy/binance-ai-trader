"""BT-021 cross-source Spot reconciliation; synthetic HTTP only, no trading."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from binance_ai_trader.public_spot_coherence import (
    CoherenceStatus,
    SpotCoherenceReview,
    observe_public_spot_coherence_once,
    review_public_spot_coherence,
)
from binance_ai_trader.public_spot_cost_review import PublicSpotCostClient
from binance_ai_trader.public_spot_depth_review import (
    DepthLevel,
    PublicSpotDepthClient,
)
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient

D = Decimal
START = 1_800_000_180_000
NOW = START + 50


def kline(ms: int) -> list[object]:
    return [
        ms, "100", "101", "99", "100", "20", ms + 59_999,
        "0", 1, "0", "0", "0",
    ]


def symbol_rules() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
                "orderTypes": ["MARKET", "LIMIT"],
                "permissionSets": [["SPOT"]],
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1000000",
                        "tickSize": "0.01",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.01",
                        "maxQty": "10",
                        "stepSize": "0.01",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.01",
                        "maxQty": "4",
                        "stepSize": "0.01",
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "10",
                        "applyMinToMarket": True,
                        "maxNotional": "10000",
                        "applyMaxToMarket": True,
                        "avgPriceMins": 0,
                    },
                ],
            }
        ]
    }


def clients(
    *,
    book_bid: str = "99.90",
    book_ask: str = "100.10",
    last_price: str = "100.05",
    depth_bid: str = "99.90",
    depth_ask: str = "100.10",
    ticker_symbol: str = "BTCUSDT",
    depth_status: int = 200,
    delay_ms: int = 5,
    rules: dict[str, object] | None = None,
) -> tuple[
    PublicSpotObservationClient,
    PublicSpotCostClient,
    PublicSpotDepthClient,
    list[str],
    list[int],
]:
    clock = [NOW]
    seen: list[str] = []
    history = [kline(START - (60 - i) * 60_000) for i in range(60)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        path = request.url.path
        seen.append(path)
        clock[0] += delay_ms
        if path == "/api/v3/klines":
            assert request.url.params["interval"] == "1m"
            return httpx.Response(200, json=history)
        if path == "/api/v3/ticker/price":
            return httpx.Response(
                200, json={"symbol": "BTCUSDT", "price": last_price}
            )
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=symbol_rules() if rules is None else rules)
        if path == "/api/v3/ticker/bookTicker":
            return httpx.Response(
                200,
                json={
                    "symbol": ticker_symbol,
                    "bidPrice": book_bid,
                    "askPrice": book_ask,
                    "bidQty": "3",
                    "askQty": "3",
                },
            )
        assert path == "/api/v3/depth"
        assert request.url.params["limit"] == "20"
        return httpx.Response(
            depth_status,
            json={
                "lastUpdateId": 101,
                "bids": [[depth_bid, "3"], ["99.80", "2"]],
                "asks": [[depth_ask, "3"], ["100.20", "2"]],
            },
            headers={"Retry-After": "12"},
        )

    transport = httpx.MockTransport(handler)
    clock_ms = lambda: clock[0]
    return (
        PublicSpotObservationClient(transport=transport, clock_ms=clock_ms),
        PublicSpotCostClient(transport=transport, clock_ms=clock_ms),
        PublicSpotDepthClient(transport=transport, clock_ms=clock_ms),
        seen,
        clock,
    )


def samples() -> tuple[Any, Any, Any, int]:
    candle, book, depth, _, clock = clients()
    a = candle.observe(symbol="BTCUSDT", interval="1m")
    b = book.observe(symbol="BTCUSDT")
    c = depth.observe(symbol="BTCUSDT")
    return a, b, c, clock[0]


def check(
    sample: tuple[Any, Any, Any, int],
    *,
    expected_symbol: str = "BTCUSDT",
    now: int | None = None,
) -> SpotCoherenceReview:
    observation, book, depth, time_ms = sample
    return review_public_spot_coherence(
        observation, book, depth,
        expected_symbol=expected_symbol,
        now_ms=time_ms if now is None else now,
    )


def test_one_shot_collected_only_from_fixed_public_endpoints() -> None:
    candle, book, depth, seen, _ = clients()
    result = observe_public_spot_coherence_once(
        candle, book, depth, symbol="BTCUSDT"
    )
    assert result.status is CoherenceStatus.COHERENT_RESEARCH_ONLY
    assert result.symbol == "BTCUSDT"
    assert result.latest_closed_candle_ms == START - 1
    assert result.max_book_depth_deviation_bps == D(0)
    assert result.no_trade
    assert not result.trade_authorized
    assert seen == [
        "/api/v3/klines",
        "/api/v3/ticker/price",
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/bookTicker",
        "/api/v3/depth",
    ]


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        ({"depth_status": 429}, CoherenceStatus.INPUT_UNAVAILABLE),
        ({"ticker_symbol": "ETHUSDT"}, CoherenceStatus.INPUT_UNAVAILABLE),
        ({"book_ask": "100.30"}, CoherenceStatus.BOOK_DEPTH_DIVERGENCE),
        ({"depth_ask": "100.16"}, CoherenceStatus.BOOK_DEPTH_DIVERGENCE),
        ({"last_price": "101.00"}, CoherenceStatus.TICKER_BOOK_DIVERGENCE),
        ({"delay_ms": 800}, CoherenceStatus.STALE_OR_UNORDERED),
    ],
)
def test_bad_public_sequence_refuses_coherence(
    parameters: dict[str, Any], expected: CoherenceStatus
) -> None:
    candle, book, depth, _, _ = clients(**parameters)
    result = observe_public_spot_coherence_once(
        candle, book, depth, symbol="BTCUSDT"
    )
    assert result.status is expected
    assert result.no_trade
    assert not result.trade_authorized


def test_filter_average_or_unevaluated_filter_fail_closed() -> None:
    original = symbol_rules()
    symbol = original["symbols"]
    assert isinstance(symbol, list)
    element = symbol[0]
    assert isinstance(element, dict)
    rules = element["filters"]
    assert isinstance(rules, list)
    modified = [
        {**item, "avgPriceMins": 5} if item["filterType"] == "NOTIONAL" else item
        for item in rules
    ]
    for filters in (
        modified,
        [*rules, {"filterType": "TRAILING_DELTA", "minTrailing": 3}],
    ):
        raw: dict[str, object] = {"symbols": [{**element, "filters": filters}]}
        candle, book, depth, _, _ = clients(rules=raw)
        result = observe_public_spot_coherence_once(
            candle, book, depth, symbol="BTCUSDT"
        )
        assert result.status is CoherenceStatus.RULES_UNVERIFIED


def test_forged_mismatched_and_stale_dataclasses_block() -> None:
    a, b, c, now = samples()
    original = (a, b, c, now)
    assert check(original).status is CoherenceStatus.COHERENT_RESEARCH_ONLY
    rejected = (
        (replace(a, symbol="ETHUSDT"), b, c, now),
        (a, replace(b, symbol="ETHUSDT"), c, now),
        (a, b, replace(c, symbol="ETHUSDT"), now),
        (a, replace(b, rules=replace(b.rules, symbol="ETHUSDT")), c, now),
    )
    for mismatched in rejected:
        assert check(mismatched).status is CoherenceStatus.SYMBOL_MISMATCH
    for source in (
        replace(a, source="forged"),
        replace(a, exchange_price_event_time_ms=123),  # type: ignore[arg-type]
    ):
        assert check((source, b, c, now)).status is CoherenceStatus.INVALID_INPUT
    for invalid in (
        replace(a, reference_price=D("110")),
        replace(a, closed_candles=a.closed_candles[:25] + a.closed_candles[26:]),
    ):
        assert check((invalid, b, c, now)).status is CoherenceStatus.CLOSED_FEED_INVALID
    assert check(original, now=now + 3001).status is CoherenceStatus.STALE_OR_UNORDERED
    assert check((a, replace(b, requested_at_ms=a.received_at_ms - 1), c, now)).status is (
        CoherenceStatus.STALE_OR_UNORDERED
    )
    assert check((a, b, replace(c, bids=()), now)).status is CoherenceStatus.DEPTH_INVALID
    assert check((a, b, replace(c, bids=(DepthLevel(D(0), D(1)),)), now)).status is (
        CoherenceStatus.DEPTH_INVALID
    )
    assert check(
        (a, b, replace(c, bids=(DepthLevel(D("99.905"), D(1)),)), now)
    ).status is CoherenceStatus.RULES_UNVERIFIED
    assert check(original, expected_symbol="ETHUSDT").status is CoherenceStatus.SYMBOL_MISMATCH


def test_missing_fields_and_untrusted_wrapper_never_contact_network() -> None:
    assert check((None, None, None, NOW)).status is CoherenceStatus.INPUT_UNAVAILABLE
    assert check((None, None, None, NOW), expected_symbol="DOGEUSDT").status is (
        CoherenceStatus.INVALID_INPUT
    )
    assert check((None, None, None, -1)).status is CoherenceStatus.INVALID_INPUT
    assert (
        observe_public_spot_coherence_once(
            object(), object(), object(), symbol="BTCUSDT"  # type: ignore[arg-type]
        ).status
        is CoherenceStatus.INVALID_INPUT
    )
