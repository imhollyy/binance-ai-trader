"""BT-020 bounded public-depth and hypothetical volume-weighted fill tests."""

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
from binance_ai_trader.public_spot_depth_review import (
    DepthCostReview,
    DepthLevel,
    DepthReviewStatus,
    PublicSpotDepth,
    PublicSpotDepthClient,
    review_hypothetical_spot_depth,
)

D = Decimal
NOW = 1_800_001_000_000
QUANTITY = Decimal("1")
FEE = Decimal("10")
SLIPPAGE = Decimal("5")
EDGE = Decimal("80")
MIN_EDGE = Decimal("5")
IMPACT_LIMIT = Decimal("30")


def valid_book() -> dict[str, object]:
    return {
        "lastUpdateId": 10001,
        "bids": [["99.90", "2"], ["99.80", "2"], ["99.70", "2"]],
        "asks": [["100.10", "2"], ["100.20", "2"], ["100.30", "2"]],
    }


def observer(
    *,
    book: object = None,
    status: int = 200,
    delay_ms: int = 0,
) -> tuple[PublicSpotDepthClient, list[str], list[int]]:
    paths: list[str] = []
    clock = [NOW]

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["limit"] == "20"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        clock[0] += delay_ms
        return httpx.Response(
            status,
            json=valid_book() if book is None else book,
            headers={"Retry-After": "12"},
        )

    return (
        PublicSpotDepthClient(transport=httpx.MockTransport(handle), clock_ms=lambda: clock[0]),
        paths,
        clock,
    )


def review(
    sample: PublicSpotDepth,
    *,
    quantity: Decimal = QUANTITY,
    fee: Decimal | None = FEE,
    slippage: Decimal = SLIPPAGE,
    edge: Decimal = EDGE,
    required: Decimal = MIN_EDGE,
    impact: Decimal = IMPACT_LIMIT,
    now: int = NOW,
) -> DepthCostReview:
    return review_hypothetical_spot_depth(
        sample,
        expected_symbol="BTCUSDT",
        quantity=quantity,
        hypothetical_taker_fee_bps=fee,
        adverse_slippage_bps=slippage,
        projected_gross_edge_bps=edge,
        minimum_net_edge_bps=required,
        maximum_depth_impact_bps=impact,
        now_ms=now,
    )


def test_one_level_vs_multi_level_weighted_costs_never_authorize_trades() -> None:
    client, paths, clock = observer()
    sample = client.observe(symbol="BTCUSDT")
    assert paths == ["/api/v3/depth"]
    assert sample.update_id == 10001
    assert sample.received_at_ms == clock[0]
    assert sample.exchange_event_time_ms is None
    assert sample.bids[0].price == D("99.90")
    assert sample.asks[0].price == D("100.10")
    first = review(sample)
    assert first.status is DepthReviewStatus.SIMULATION_ONLY
    assert not first.trade_authorized
    assert first.buy_vwap == D("100.10")
    assert first.sell_vwap == D("99.90")
    assert first.roundtrip_cost_bps is not None
    assert D("45") < first.roundtrip_cost_bps < D("55")
    assert first.hypothetical_net_edge_bps is not None
    assert first.hypothetical_net_edge_bps > MIN_EDGE
    larger = review(sample, quantity=D("3"))
    assert larger.status is DepthReviewStatus.SIMULATION_ONLY
    assert larger.buy_vwap == D("100.10") * D("2") / D("3") + D("100.20") / D("3")
    assert larger.sell_vwap == D("99.90") * D("2") / D("3") + D("99.80") / D("3")
    assert larger.roundtrip_cost_bps is not None
    assert larger.roundtrip_cost_bps > first.roundtrip_cost_bps
    assert not larger.trade_authorized


def test_unknown_account_fee_and_negative_net_edge_fail_closed() -> None:
    sample = observer()[0].observe(symbol="BTCUSDT")
    missing = review(sample, fee=None)
    assert missing.status is DepthReviewStatus.ACCOUNT_FEE_UNKNOWN
    assert not missing.trade_authorized
    negative = review(sample, edge=D("20"))
    assert negative.status is DepthReviewStatus.NEGATIVE_NET_EDGE
    assert negative.hypothetical_net_edge_bps is not None
    assert negative.hypothetical_net_edge_bps < 0
    assert review(sample, fee=D("0")).status is DepthReviewStatus.INVALID_ASSUMPTIONS
    assert review(sample, slippage=D("101")).status is DepthReviewStatus.INVALID_ASSUMPTIONS
    assert review(sample, edge=D("NaN")).status is DepthReviewStatus.INVALID_ASSUMPTIONS


def test_insufficient_depth_and_price_impact_rejection() -> None:
    sample = observer()[0].observe(symbol="BTCUSDT")
    assert review(sample, quantity=D("7")).status is DepthReviewStatus.INSUFFICIENT_VISIBLE_DEPTH
    assert review(sample, quantity=D("3"), impact=D("2")).status is (
        DepthReviewStatus.DEPTH_IMPACT_LIMIT
    )
    assert review(sample, quantity=D("0")).status is DepthReviewStatus.INVALID_ASSUMPTIONS


@pytest.mark.parametrize("status", [418, 429])
def test_exchange_rate_limits_are_never_retried(status: int) -> None:
    client, paths, _ = observer(status=status)
    with pytest.raises(MarketRateLimited) as caught:
        client.observe(symbol="BTCUSDT")
    assert caught.value.status_code == status
    assert caught.value.retry_after_seconds == 12
    assert paths == ["/api/v3/depth"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"lastUpdateId": 0, "bids": [["99", "1"]], "asks": [["100", "1"]]},
        {"lastUpdateId": True, "bids": [["99", "1"]], "asks": [["100", "1"]]},
        {"lastUpdateId": 1, "bids": [], "asks": [["100", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "0"]], "asks": [["100", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "NaN"]], "asks": [["100", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", 1]], "asks": [["100", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "1"], ["100", "1"]], "asks": [["101", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "1"], ["99", "1"]], "asks": [["101", "1"]]},
        {"lastUpdateId": 1, "bids": [["100", "1"]], "asks": [["99", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "1"]], "asks": [["100", "1"], ["99", "1"]]},
        {"lastUpdateId": 1, "bids": [["99", "1"]], "asks": [["100", "1"]], "T": 42},
        [],
    ],
)
def test_malformed_order_books_rejected_before_simulation(payload: object) -> None:
    with pytest.raises(MarketDataInvalid):
        observer(book=payload)[0].observe(symbol="BTCUSDT")


def test_clock_source_and_modified_snapshot_cannot_authorize() -> None:
    client, paths, _ = observer(delay_ms=4000)
    with pytest.raises(MarketDataInvalid, match="stale"):
        client.observe(symbol="BTCUSDT")
    safe, paths, _ = observer()
    with pytest.raises(ValueError):
        safe.observe(symbol="XRPUSDT")
    assert paths == []
    sample = safe.observe(symbol="BTCUSDT")
    assert review(sample, now=NOW + 3001).status is DepthReviewStatus.STALE
    invalid_samples = (
        replace(sample, source="unverified_depth"),
        replace(sample, symbol="ETHUSDT"),
        replace(sample, bids=(DepthLevel(D("0"), D("5")),)),
        replace(sample, asks=(DepthLevel(D("99"), D("5")),)),
        replace(sample, update_id=0),
    )
    for untrusted in invalid_samples:
        assert review(untrusted).status is DepthReviewStatus.INVALID_SOURCE


def test_bad_http_and_timeout_fail_closed() -> None:
    with pytest.raises(MarketDataUnavailable):
        observer(status=503)[0].observe(symbol="BTCUSDT")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no depth", request=request)

    client = PublicSpotDepthClient(transport=httpx.MockTransport(down), clock_ms=lambda: NOW)
    with pytest.raises(MarketDataUnavailable):
        client.observe(symbol="BTCUSDT")
