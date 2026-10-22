"""BT-021: conservative read-only cross-source Spot observation coherence.

Three individually validated Binance PUBLIC REST observations do not form an
atomic exchange snapshot. Client receive times are LOCAL, not exchange times.
This module never authorizes PAPER or LIVE trading, workers or orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.market_data import SUPPORTED_SYMBOLS, MarketDataError
from binance_ai_trader.public_spot_cost_review import (
    PublicBookCostSample,
    PublicSpotCostClient,
    SymbolCostRules,
)
from binance_ai_trader.public_spot_depth_review import (
    DepthLevel,
    DepthReviewStatus,
    PublicSpotDepth,
    PublicSpotDepthClient,
    review_hypothetical_spot_depth,
)
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservation,
    PublicSpotObservationClient,
    validate_public_observation,
)

MAX_COLLECTION_SPAN_MS = 3_000
MAX_LOCAL_AGE_MS = 3_000
MAX_BOOK_DEPTH_DEVIATION_BPS = Decimal("5")
MAX_TICKER_BOOK_DEVIATION_BPS = Decimal("30")
BPS = Decimal("10000")
ZERO = Decimal(0)


class CoherenceStatus(StrEnum):
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    INVALID_INPUT = "INVALID_INPUT"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    STALE_OR_UNORDERED = "STALE_OR_UNORDERED"
    CLOSED_FEED_INVALID = "CLOSED_FEED_INVALID"
    RULES_UNVERIFIED = "RULES_UNVERIFIED"
    DEPTH_INVALID = "DEPTH_INVALID"
    BOOK_DEPTH_DIVERGENCE = "BOOK_DEPTH_DIVERGENCE"
    TICKER_BOOK_DIVERGENCE = "TICKER_BOOK_DIVERGENCE"
    COHERENT_RESEARCH_ONLY = "COHERENT_RESEARCH_ONLY"


@dataclass(frozen=True, slots=True)
class SpotCoherenceReview:
    """Only a quality assessment; deliberately no trade-permission field."""

    status: CoherenceStatus
    symbol: str | None = None
    latest_closed_candle_ms: int | None = None
    locally_received_at_ms: int | None = None
    max_book_depth_deviation_bps: Decimal | None = None

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _valid_decimal(value: object, *, zero_allowed: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (value >= ZERO if zero_allowed else value > ZERO)
    )


def _difference_bps(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) * BPS / b


def review_public_spot_coherence(
    observation: PublicSpotObservation | None,
    book: PublicBookCostSample | None,
    depth: PublicSpotDepth | None,
    *,
    expected_symbol: str,
    now_ms: int,
) -> SpotCoherenceReview:
    """Fail closed on source discrepancy, but never claim synchronized fills.

    An independently supplied dataclass does NOT prove public REST provenance.
    Use `observe_public_spot_coherence_once` for server-owned REST acquisition.
    """
    if expected_symbol not in SUPPORTED_SYMBOLS or type(now_ms) is not int or now_ms <= 0:
        return SpotCoherenceReview(CoherenceStatus.INVALID_INPUT)
    if observation is None or book is None or depth is None:
        return SpotCoherenceReview(CoherenceStatus.INPUT_UNAVAILABLE)
    if (
        type(observation) is not PublicSpotObservation
        or type(book) is not PublicBookCostSample
        or type(depth) is not PublicSpotDepth
        or any(x.symbol != expected_symbol for x in (observation, book, depth))
        or type(book.rules) is not SymbolCostRules
        or book.rules.symbol != expected_symbol
    ):
        return SpotCoherenceReview(CoherenceStatus.SYMBOL_MISMATCH)
    if (
        observation.source != "binance_public_spot_ticker_price"
        or book.source != "binance_public_spot_book_ticker"
        or depth.source != "binance_public_spot_depth"
        or observation.exchange_price_event_time_ms is not None
        or book.exchange_quote_time_ms is not None
        or depth.exchange_event_time_ms is not None
    ):
        return SpotCoherenceReview(CoherenceStatus.INVALID_INPUT)
    stamps = (
        observation.requested_at_ms,
        observation.received_at_ms,
        book.requested_at_ms,
        book.received_at_ms,
        depth.requested_at_ms,
        depth.received_at_ms,
    )
    if (
        any(type(x) is not int or x <= 0 for x in stamps)
        or tuple(sorted(stamps)) != stamps
        or stamps[-1] - stamps[0] > MAX_COLLECTION_SPAN_MS
        or now_ms < stamps[-1]
        or now_ms - stamps[-1] > MAX_LOCAL_AGE_MS
    ):
        return SpotCoherenceReview(CoherenceStatus.STALE_OR_UNORDERED, expected_symbol)
    try:
        validate_public_observation(observation, now_ms=now_ms)
    except (MarketDataError, ValueError, TypeError, AttributeError):
        return SpotCoherenceReview(CoherenceStatus.CLOSED_FEED_INVALID, expected_symbol)
    rules = book.rules
    if (
        not _valid_decimal(rules.tick)
        or not _valid_decimal(rules.minimum_price, zero_allowed=True)
        or not _valid_decimal(rules.maximum_price, zero_allowed=True)
        or rules.needs_unavailable_average
        or rules.other_filters
    ):
        return SpotCoherenceReview(CoherenceStatus.RULES_UNVERIFIED, expected_symbol)
    if (
        not all(
            _valid_decimal(p) for p in (book.bid, book.ask, book.bid_quantity, book.ask_quantity)
        )
        or book.bid >= book.ask
        or book.bid % rules.tick != 0
        or book.ask % rules.tick != 0
        or (rules.minimum_price > 0 and book.bid < rules.minimum_price)
        or (rules.maximum_price > 0 and book.ask > rules.maximum_price)
    ):
        return SpotCoherenceReview(CoherenceStatus.INVALID_INPUT, expected_symbol)
    if (
        not isinstance(depth.bids, tuple)
        or not isinstance(depth.asks, tuple)
        or not depth.bids
        or not depth.asks
        or type(depth.bids[0]) is not DepthLevel
        or type(depth.asks[0]) is not DepthLevel
    ):
        return SpotCoherenceReview(CoherenceStatus.DEPTH_INVALID, expected_symbol)
    # Reuse BT-020's independent depth structural, freshness and symbol gates.
    # No account-specific fee is present, so valid depth MUST reach that gate.
    depth_result = review_hypothetical_spot_depth(
        depth,
        expected_symbol=expected_symbol,
        quantity=min(depth.bids[0].quantity, depth.asks[0].quantity),
        hypothetical_taker_fee_bps=None,
        adverse_slippage_bps=ZERO,
        projected_gross_edge_bps=ZERO,
        minimum_net_edge_bps=ZERO,
        maximum_depth_impact_bps=Decimal(100),
        now_ms=now_ms,
    )
    if depth_result.status is not DepthReviewStatus.ACCOUNT_FEE_UNKNOWN:
        return SpotCoherenceReview(CoherenceStatus.DEPTH_INVALID, expected_symbol)
    for level in (*depth.bids, *depth.asks):
        if (
            level.price % rules.tick != 0
            or (rules.minimum_price > 0 and level.price < rules.minimum_price)
            or (rules.maximum_price > 0 and level.price > rules.maximum_price)
        ):
            return SpotCoherenceReview(CoherenceStatus.RULES_UNVERIFIED, expected_symbol)
    divergence = max(
        _difference_bps(book.bid, depth.bids[0].price),
        _difference_bps(book.ask, depth.asks[0].price),
    )
    if divergence > MAX_BOOK_DEPTH_DEVIATION_BPS:
        return SpotCoherenceReview(
            CoherenceStatus.BOOK_DEPTH_DIVERGENCE,
            expected_symbol,
            max_book_depth_deviation_bps=divergence,
        )
    reference = observation.reference_price
    if reference < book.bid * (
        Decimal(1) - MAX_TICKER_BOOK_DEVIATION_BPS / BPS
    ) or reference > book.ask * (Decimal(1) + MAX_TICKER_BOOK_DEVIATION_BPS / BPS):
        return SpotCoherenceReview(CoherenceStatus.TICKER_BOOK_DIVERGENCE, expected_symbol)
    return SpotCoherenceReview(
        CoherenceStatus.COHERENT_RESEARCH_ONLY,
        expected_symbol,
        observation.signal_close_time_ms,
        stamps[-1],
        divergence,
    )


def observe_public_spot_coherence_once(
    candle_client: PublicSpotObservationClient,
    quote_client: PublicSpotCostClient,
    depth_client: PublicSpotDepthClient,
    *,
    symbol: str,
) -> SpotCoherenceReview:
    """One explicit bounded sequence of fixed public clients (not an exchange snapshot)."""
    if (
        type(candle_client) is not PublicSpotObservationClient
        or type(quote_client) is not PublicSpotCostClient
        or type(depth_client) is not PublicSpotDepthClient
        or symbol not in SUPPORTED_SYMBOLS
    ):
        return SpotCoherenceReview(CoherenceStatus.INVALID_INPUT)
    try:
        observation = candle_client.observe(symbol=symbol, interval="1m", limit=100)
        book = quote_client.observe(symbol=symbol)
        depth = depth_client.observe(symbol=symbol)
        return review_public_spot_coherence(
            observation, book, depth, expected_symbol=symbol, now_ms=depth_client.now_ms()
        )
    except (MarketDataError, ValueError, TypeError, AttributeError):
        return SpotCoherenceReview(CoherenceStatus.INPUT_UNAVAILABLE, symbol)
