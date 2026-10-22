"""BT-020 public Spot depth and conservative simulated two-sided fill costs.

No private Binance data, signed orders, PAPER account writes, scheduler, API
route or worker hook. A REST depth snapshot is NOT an executable quote.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import httpx

from binance_ai_trader.market_data import (
    PUBLIC_MARKET_URL,
    SUPPORTED_SYMBOLS,
    MarketDataInvalid,
    MarketDataUnavailable,
    MarketRateLimited,
    _retry_after,
)

MAX_DEPTH_LEVELS = 20
MAX_DEPTH_AGE_MS = 3_000
BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class PublicSpotDepth:
    """Snapshot sequence number is NOT a Binance exchange event time."""

    symbol: str
    update_id: int
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    requested_at_ms: int
    received_at_ms: int
    source: str = "binance_public_spot_depth"
    exchange_event_time_ms: None = None


class DepthReviewStatus(StrEnum):
    INVALID_SOURCE = "INVALID_SOURCE"
    STALE = "STALE"
    INVALID_ASSUMPTIONS = "INVALID_ASSUMPTIONS"
    INSUFFICIENT_VISIBLE_DEPTH = "INSUFFICIENT_VISIBLE_DEPTH"
    DEPTH_IMPACT_LIMIT = "DEPTH_IMPACT_LIMIT"
    ACCOUNT_FEE_UNKNOWN = "ACCOUNT_FEE_UNKNOWN"
    NEGATIVE_NET_EDGE = "NEGATIVE_NET_EDGE"
    SIMULATION_ONLY = "SIMULATION_ONLY"


@dataclass(frozen=True, slots=True)
class DepthCostReview:
    status: DepthReviewStatus
    buy_vwap: Decimal | None = None
    sell_vwap: Decimal | None = None
    roundtrip_cost_bps: Decimal | None = None
    hypothetical_net_edge_bps: Decimal | None = None

    @property
    def trade_authorized(self) -> bool:
        return False


def _positive_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise MarketDataInvalid("invalid_depth_decimal_type")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise MarketDataInvalid("invalid_depth_decimal") from exc
    if not result.is_finite() or result <= 0:
        raise MarketDataInvalid("invalid_depth_positive_quantity_or_price")
    return result


def _parse_side(raw: object, *, is_bid: bool) -> tuple[DepthLevel, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DEPTH_LEVELS:
        raise MarketDataInvalid("invalid_depth_side_length")
    levels: list[DepthLevel] = []
    previous: Decimal | None = None
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2:
            raise MarketDataInvalid("invalid_depth_level_shape")
        price = _positive_decimal(entry[0])
        quantity = _positive_decimal(entry[1])
        if previous is not None and (
            (is_bid and price >= previous) or (not is_bid and price <= previous)
        ):
            raise MarketDataInvalid("duplicate_or_unordered_depth_levels")
        levels.append(DepthLevel(price, quantity))
        previous = price
    return tuple(levels)


def _bounded_decimal(value: object, minimum: Decimal, maximum: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and minimum <= value <= maximum


class PublicSpotDepthClient:
    """One bounded fixed-host PUBLIC GET, no retries, proxies, keys or redirects."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def now_ms(self) -> int:
        current = self._clock_ms()
        if type(current) is not int or current <= 0:
            raise MarketDataInvalid("invalid_depth_clock")
        return current

    def observe(self, *, symbol: str) -> PublicSpotDepth:
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported_public_spot_symbol")
        requested = self.now_ms()
        try:
            with httpx.Client(
                base_url=PUBLIC_MARKET_URL,
                transport=self._transport,
                timeout=httpx.Timeout(3.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                reply = client.get(
                    "/api/v3/depth",
                    params={"symbol": symbol, "limit": MAX_DEPTH_LEVELS},
                )
        except httpx.RequestError as exc:
            raise MarketDataUnavailable("public_depth_network_failure") from exc
        received = self.now_ms()
        if not 0 <= received - requested <= MAX_DEPTH_AGE_MS:
            raise MarketDataInvalid("stale_public_depth_response")
        if reply.status_code in (418, 429):
            raise MarketRateLimited(
                reply.status_code, _retry_after(reply.headers.get("Retry-After"))
            )
        if reply.status_code != 200:
            raise MarketDataUnavailable("public_depth_http_failure")
        try:
            payload: Any = reply.json()
        except ValueError as exc:
            raise MarketDataInvalid("malformed_public_depth_json") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "lastUpdateId",
            "bids",
            "asks",
        }:
            raise MarketDataInvalid("invalid_public_depth_payload")
        update_id = payload["lastUpdateId"]
        if type(update_id) is not int or update_id <= 0:
            raise MarketDataInvalid("invalid_public_depth_update_sequence")
        bids = _parse_side(payload["bids"], is_bid=True)
        asks = _parse_side(payload["asks"], is_bid=False)
        if bids[0].price >= asks[0].price:
            raise MarketDataInvalid("crossed_public_depth_book")
        return PublicSpotDepth(
            symbol, update_id, bids, asks, requested, received
        )


def _fill_vwap(levels: tuple[DepthLevel, ...], amount: Decimal) -> Decimal | None:
    remaining = amount
    notional = Decimal(0)
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            return notional / amount
    return None


def review_hypothetical_spot_depth(
    sample: PublicSpotDepth,
    *,
    quantity: Decimal,
    hypothetical_taker_fee_bps: Decimal | None,
    adverse_slippage_bps: Decimal,
    projected_gross_edge_bps: Decimal,
    minimum_net_edge_bps: Decimal,
    maximum_depth_impact_bps: Decimal,
    now_ms: int,
) -> DepthCostReview:
    """Pricing stress test only; even positive net results never permit orders."""
    if (
        not isinstance(sample, PublicSpotDepth)
        or sample.source != "binance_public_spot_depth"
        or sample.symbol not in SUPPORTED_SYMBOLS
        or sample.exchange_event_time_ms is not None
        or type(sample.update_id) is not int
        or sample.update_id <= 0
        or type(sample.requested_at_ms) is not int
        or type(sample.received_at_ms) is not int
        or not isinstance(sample.bids, tuple)
        or not isinstance(sample.asks, tuple)
        or not sample.bids
        or not sample.asks
        or type(now_ms) is not int
        or now_ms <= 0
    ):
        return DepthCostReview(DepthReviewStatus.INVALID_SOURCE)
    # Revalidate even in-memory caller-mutated snapshots, not merely HTTP parses.
    try:
        previous_bid: Decimal | None = None
        previous_ask: Decimal | None = None
        for row in sample.bids:
            if (
                not isinstance(row, DepthLevel)
                or not _bounded_decimal(row.price, Decimal("1e-12"), Decimal("1e20"))
                or not _bounded_decimal(row.quantity, Decimal("1e-12"), Decimal("1e20"))
                or (previous_bid is not None and row.price >= previous_bid)
            ):
                return DepthCostReview(DepthReviewStatus.INVALID_SOURCE)
            previous_bid = row.price
        for row in sample.asks:
            if (
                not isinstance(row, DepthLevel)
                or not _bounded_decimal(row.price, Decimal("1e-12"), Decimal("1e20"))
                or not _bounded_decimal(row.quantity, Decimal("1e-12"), Decimal("1e20"))
                or (previous_ask is not None and row.price <= previous_ask)
            ):
                return DepthCostReview(DepthReviewStatus.INVALID_SOURCE)
            previous_ask = row.price
        if sample.bids[0].price >= sample.asks[0].price:
            return DepthCostReview(DepthReviewStatus.INVALID_SOURCE)
    except (TypeError, ArithmeticError):
        return DepthCostReview(DepthReviewStatus.INVALID_SOURCE)
    if (
        sample.received_at_ms < sample.requested_at_ms
        or sample.received_at_ms - sample.requested_at_ms > MAX_DEPTH_AGE_MS
        or now_ms < sample.received_at_ms
        or now_ms - sample.received_at_ms > MAX_DEPTH_AGE_MS
    ):
        return DepthCostReview(DepthReviewStatus.STALE)
    if (
        not _bounded_decimal(quantity, Decimal("1e-12"), Decimal("1e20"))
        or not _bounded_decimal(adverse_slippage_bps, Decimal(0), Decimal(100))
        or not _bounded_decimal(projected_gross_edge_bps, Decimal(0), BPS)
        or not _bounded_decimal(minimum_net_edge_bps, Decimal(0), BPS)
        or not _bounded_decimal(maximum_depth_impact_bps, Decimal(0), Decimal(100))
    ):
        return DepthCostReview(DepthReviewStatus.INVALID_ASSUMPTIONS)
    buy = _fill_vwap(sample.asks, quantity)
    sell = _fill_vwap(sample.bids, quantity)
    if buy is None or sell is None:
        return DepthCostReview(DepthReviewStatus.INSUFFICIENT_VISIBLE_DEPTH)
    bid_impact = BPS * (Decimal(1) - sell / sample.bids[0].price)
    ask_impact = BPS * (buy / sample.asks[0].price - Decimal(1))
    if max(bid_impact, ask_impact) > maximum_depth_impact_bps:
        return DepthCostReview(DepthReviewStatus.DEPTH_IMPACT_LIMIT, buy, sell)
    if hypothetical_taker_fee_bps is None:
        return DepthCostReview(DepthReviewStatus.ACCOUNT_FEE_UNKNOWN, buy, sell)
    if not _bounded_decimal(hypothetical_taker_fee_bps, Decimal("0.000001"), Decimal(100)):
        return DepthCostReview(DepthReviewStatus.INVALID_ASSUMPTIONS, buy, sell)
    fee = hypothetical_taker_fee_bps / BPS
    slippage = adverse_slippage_bps / BPS
    hypothetical_buy = buy * (Decimal(1) + slippage) * (Decimal(1) + fee)
    hypothetical_sell = sell * (Decimal(1) - slippage) * (Decimal(1) - fee)
    costs = BPS * (Decimal(1) - hypothetical_sell / hypothetical_buy)
    net = projected_gross_edge_bps - costs
    return DepthCostReview(
        DepthReviewStatus.SIMULATION_ONLY
        if net > minimum_net_edge_bps
        else DepthReviewStatus.NEGATIVE_NET_EDGE,
        buy,
        sell,
        costs,
        net,
    )
