"""BT-019 synthetic PUBLIC Spot quote/rules and cost scenarios, never orders."""

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
from binance_ai_trader.public_spot_cost_review import (
    CostReview,
    CostReviewStatus,
    PublicBookCostSample,
    PublicSpotCostClient,
    review_hypothetical_spot_cost,
)

D = Decimal
CLOCK = 1_800_000_000_000


def exchange_info() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
                "orderTypes": ["MARKET", "LIMIT"],
                "permissionSets": [["SPOT", "MARGIN"]],
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
                        "maxNotional": "1000",
                        "applyMaxToMarket": True,
                        "avgPriceMins": 0,
                    },
                ],
            }
        ]
    }


def observer(
    *,
    info: object = None,
    book: object = None,
    info_status: int = 200,
    book_status: int = 200,
    delay: int = 0,
) -> tuple[PublicSpotCostClient, list[str], list[int]]:
    paths: list[str] = []
    clock = [CLOCK]

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.host == "data-api.binance.vision"
        assert request.method == "GET"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                info_status,
                json=exchange_info() if info is None else info,
                headers={"Retry-After": "15"},
            )
        assert request.url.path == "/api/v3/ticker/bookTicker"
        clock[0] += delay
        return httpx.Response(
            book_status,
            json=(
                {
                    "symbol": "BTCUSDT",
                    "bidPrice": "100.00",
                    "askPrice": "100.10",
                    "bidQty": "5",
                    "askQty": "5",
                }
                if book is None
                else book
            ),
            headers={"Retry-After": "15"},
        )

    return (
        PublicSpotCostClient(transport=httpx.MockTransport(handler), clock_ms=lambda: clock[0]),
        paths,
        clock,
    )


def review(
    sample: PublicBookCostSample,
    *,
    size: Decimal = D("0.2"),
    edge: Decimal = D("50"),
    fee: Decimal | None = D("10"),
    slip: Decimal = D("5"),
    minimum: Decimal = D("5"),
    now: int = CLOCK,
) -> CostReview:
    return review_hypothetical_spot_cost(
        sample,
        quantity=size,
        projected_gross_edge_bps=edge,
        hypothetical_taker_fee_bps=fee,
        stress_slippage_bps=slip,
        required_net_edge_bps=minimum,
        now_ms=now,
    )


def test_public_book_and_rules_are_read_only_and_unknown_fee_blocks() -> None:
    client, paths, clock = observer()
    sample = client.observe(symbol="BTCUSDT")
    assert paths == ["/api/v3/exchangeInfo", "/api/v3/ticker/bookTicker"]
    assert sample.bid == D("100")
    assert sample.ask == D("100.10")
    assert sample.exchange_quote_time_ms is None
    assert sample.received_at_ms == clock[0]
    assert sample.spread_bps > 9
    assert review(sample, fee=None).status is CostReviewStatus.ACCOUNT_FEE_UNKNOWN
    positive = review(sample)
    assert positive.status is CostReviewStatus.SIMULATION_ONLY
    assert not positive.trade_authorized
    assert positive.modeled_roundtrip_cost_bps is not None
    assert D("35") < positive.modeled_roundtrip_cost_bps < D("45")
    assert positive.modeled_net_edge_bps is not None
    assert positive.modeled_net_edge_bps > 5
    assert review(sample, edge=D("30")).status is CostReviewStatus.NEGATIVE_NET_EDGE


def test_market_size_grid_liquidity_and_assumptions_fail_closed() -> None:
    sample = observer()[0].observe(symbol="BTCUSDT")
    for size in (D("0.001"), D("0.201"), D("5"), D("0.02")):
        assert review(sample, size=size).status is CostReviewStatus.MARKET_SIZE_INVALID
    assert review(sample, size=D("4")).status is CostReviewStatus.SIMULATION_ONLY
    assert review(sample, fee=D("0")).status is CostReviewStatus.INVALID_STRESS
    assert review(sample, fee=D("NaN")).status is CostReviewStatus.INVALID_STRESS
    assert review(sample, slip=D("101")).status is CostReviewStatus.INVALID_STRESS
    assert review(sample, minimum=D("20")).status is CostReviewStatus.NEGATIVE_NET_EDGE
    limited, _, _ = observer(
        book={
            "symbol": "BTCUSDT",
            "bidPrice": "100.00",
            "askPrice": "100.10",
            "bidQty": "0.1",
            "askQty": "4",
        }
    )
    assert (
        review(limited.observe(symbol="BTCUSDT")).status
        is CostReviewStatus.TOP_BOOK_LIQUIDITY
    )


@pytest.mark.parametrize("which", ["info", "book"])
@pytest.mark.parametrize("status", [418, 429])
def test_public_rate_limit_respected_without_retry(which: str, status: int) -> None:
    client, paths, _ = (
        observer(info_status=status) if which == "info" else observer(book_status=status)
    )
    with pytest.raises(MarketRateLimited) as caught:
        client.observe(symbol="BTCUSDT")
    assert caught.value.status_code == status
    assert caught.value.retry_after_seconds == 15
    assert paths == (
        ["/api/v3/exchangeInfo"]
        if which == "info"
        else ["/api/v3/exchangeInfo", "/api/v3/ticker/bookTicker"]
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "ETHUSDT", "bidPrice": "100", "askPrice": "101", "bidQty": "2", "askQty": "2"},
        {"symbol": "BTCUSDT", "bidPrice": "101", "askPrice": "100", "bidQty": "2", "askQty": "2"},
        {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "NaN", "bidQty": "2", "askQty": "2"},
        {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "101", "bidQty": 2, "askQty": "2"},
        {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "101", "bidQty": "0", "askQty": "2"},
        {
            "symbol": "BTCUSDT",
            "bidPrice": "100.001",
            "askPrice": "101",
            "bidQty": "2",
            "askQty": "2",
        },
        {
            "symbol": "BTCUSDT",
            "bidPrice": "100",
            "askPrice": "101",
            "bidQty": "2",
            "askQty": "2",
            "time": 99,
        },
        [],
    ],
)
def test_corrupted_book_ticker_is_rejected(bad: object) -> None:
    with pytest.raises(MarketDataInvalid):
        observer(book=bad)[0].observe(symbol="BTCUSDT")


def test_exchange_permissions_and_unverified_filters_block() -> None:
    data = exchange_info()
    symbols = data["symbols"]
    assert isinstance(symbols, list)
    base = symbols[0]
    assert isinstance(base, dict)
    for patch in (
        {"status": "HALT"},
        {"isSpotTradingAllowed": False},
        {"permissionSets": [["MARGIN"]]},
        {"permissionSets": [["SPOT"], ["TRD_GRP_01"]]},
        {"orderTypes": ["LIMIT"]},
        {"filters": []},
    ):
        with pytest.raises(MarketDataInvalid):
            observer(info={"symbols": [{**base, **patch}]})[0].observe(symbol="BTCUSDT")
    filters = base["filters"]
    assert isinstance(filters, list)
    unknown = {
        "symbols": [
            {**base, "filters": [*filters, {"filterType": "T_PLUS_SELL", "lag": 1}]}
        ]
    }
    sample = observer(info=unknown)[0].observe(symbol="BTCUSDT")
    assert review(sample).status is CostReviewStatus.UNEVALUATED_FILTER
    average_rule = [
        {**item, "avgPriceMins": 5} if item["filterType"] == "NOTIONAL" else item
        for item in filters
    ]
    sample = observer(info={"symbols": [{**base, "filters": average_rule}]})[0].observe(
        symbol="BTCUSDT"
    )
    assert review(sample).status is CostReviewStatus.UNEVALUATED_FILTER


def test_stale_source_forgery_network_failure_and_invalid_symbol_block() -> None:
    delayed, _, _ = observer(delay=4000)
    with pytest.raises(MarketDataInvalid, match="stale"):
        delayed.observe(symbol="BTCUSDT")
    client, paths, _ = observer()
    with pytest.raises(ValueError):
        client.observe(symbol="XRPUSDT")
    assert paths == []
    sample = client.observe(symbol="BTCUSDT")
    assert review(sample, now=CLOCK + 3001).status is CostReviewStatus.STALE
    assert (
        review(replace(sample, source="untrusted_model_data")).status
        is CostReviewStatus.INVALID_SOURCE
    )
    assert (
        review(replace(sample, rules=replace(sample.rules, symbol="ETHUSDT"))).status
        is CostReviewStatus.INVALID_SOURCE
    )
    with pytest.raises(MarketDataUnavailable):
        observer(book_status=503)[0].observe(symbol="BTCUSDT")

    def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock_timeout", request=request)

    offline = PublicSpotCostClient(transport=httpx.MockTransport(failed), clock_ms=lambda: CLOCK)
    with pytest.raises(MarketDataUnavailable):
        offline.observe(symbol="BTCUSDT")
