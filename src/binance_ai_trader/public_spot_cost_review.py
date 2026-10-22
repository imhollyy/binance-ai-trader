"""BT-019: public-only Binance Spot book/filters and hypothetical fee-aware cost review.

NO authenticated tradeFee, order, account, ledger, scheduler or worker paths.
The bookTicker has NO exchange event timestamp or fill guarantee.
Even positive hypothetical net edge NEVER authorizes a trade.
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

BPS = Decimal("10000")
MAX_AGE_MS = 3000
KNOWN_FILTERS = frozenset(
    {"LOT_SIZE", "MARKET_LOT_SIZE", "NOTIONAL", "MIN_NOTIONAL", "PRICE_FILTER"}
)


@dataclass(frozen=True, slots=True)
class LotConstraint:
    minimum: Decimal
    maximum: Decimal
    step: Decimal


@dataclass(frozen=True, slots=True)
class SymbolCostRules:
    symbol: str
    tick: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    lot: LotConstraint
    market_lot: LotConstraint | None
    minimum_notional: Decimal
    maximum_notional: Decimal | None
    needs_unavailable_average: bool
    other_filters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicBookCostSample:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal
    ask_quantity: Decimal
    requested_at_ms: int
    received_at_ms: int
    rules: SymbolCostRules
    source: str = "binance_public_spot_book_ticker"
    exchange_quote_time_ms: None = None

    @property
    def spread_bps(self) -> Decimal:
        return BPS * (self.ask - self.bid) / ((self.ask + self.bid) / 2)


class CostReviewStatus(StrEnum):
    INVALID_SOURCE = "INVALID_SOURCE"
    STALE = "STALE"
    UNEVALUATED_FILTER = "UNEVALUATED_FILTER"
    MARKET_SIZE_INVALID = "MARKET_SIZE_INVALID"
    TOP_BOOK_LIQUIDITY = "TOP_BOOK_LIQUIDITY"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    ACCOUNT_FEE_UNKNOWN = "ACCOUNT_FEE_UNKNOWN"
    INVALID_STRESS = "INVALID_STRESS"
    NEGATIVE_NET_EDGE = "NEGATIVE_NET_EDGE"
    SIMULATION_ONLY = "SIMULATION_ONLY"


@dataclass(frozen=True, slots=True)
class CostReview:
    status: CostReviewStatus
    spread_bps: Decimal | None = None
    modeled_roundtrip_cost_bps: Decimal | None = None
    modeled_net_edge_bps: Decimal | None = None

    @property
    def trade_authorized(self) -> bool:
        return False


def _decimal(raw: object, key: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(raw, str):
        raise MarketDataInvalid("invalid_public_" + key)
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise MarketDataInvalid("invalid_public_" + key) from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise MarketDataInvalid("invalid_public_" + key)
    return number


def _lot(rule: dict[str, Any], *, allow_disabled: bool) -> LotConstraint | None:
    minimum = _decimal(rule.get("minQty"), "min_qty", allow_zero=True)
    maximum = _decimal(rule.get("maxQty"), "max_qty", allow_zero=True)
    step = _decimal(rule.get("stepSize"), "step", allow_zero=True)
    if allow_disabled and minimum == maximum == step == 0:
        return None
    if maximum <= 0 or step <= 0 or maximum < minimum:
        raise MarketDataInvalid("invalid_public_lot_rule")
    return LotConstraint(minimum, maximum, step)


def _rules(raw: object, symbol: str) -> SymbolCostRules:
    if not isinstance(raw, dict):
        raise MarketDataInvalid("invalid_exchange_info")
    symbols = raw.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != 1 or not isinstance(symbols[0], dict):
        raise MarketDataInvalid("missing_exchange_symbol")
    entry = symbols[0]
    permissions = entry.get("permissionSets")
    if (
        entry.get("symbol") != symbol
        or entry.get("status") != "TRADING"
        or entry.get("isSpotTradingAllowed") is not True
        or not isinstance(entry.get("orderTypes"), list)
        or "MARKET" not in entry["orderTypes"]
        or not isinstance(permissions, list)
        or len(permissions) != 1
        or not isinstance(permissions[0], list)
        or "SPOT" not in permissions[0]
    ):
        raise MarketDataInvalid("spot_permission_or_trading_status_unknown")
    filters = entry.get("filters")
    if not isinstance(filters, list) or not filters:
        raise MarketDataInvalid("missing_exchange_filters")
    by_type: dict[str, dict[str, Any]] = {}
    for item in filters:
        if not isinstance(item, dict) or not isinstance(item.get("filterType"), str):
            raise MarketDataInvalid("invalid_exchange_filter")
        kind = item["filterType"]
        if kind in by_type:
            raise MarketDataInvalid("duplicate_exchange_filter")
        by_type[kind] = item
    if "LOT_SIZE" not in by_type or "PRICE_FILTER" not in by_type:
        raise MarketDataInvalid("required_spot_filter_missing")
    lot = _lot(by_type["LOT_SIZE"], allow_disabled=False)
    if lot is None:
        raise MarketDataInvalid("invalid_public_lot_rule")
    market_lot = (
        _lot(by_type["MARKET_LOT_SIZE"], allow_disabled=True)
        if "MARKET_LOT_SIZE" in by_type
        else None
    )
    price_filter = by_type["PRICE_FILTER"]
    tick = _decimal(price_filter.get("tickSize"), "tick")
    minimum_price = _decimal(price_filter.get("minPrice"), "min_price", allow_zero=True)
    maximum_price = _decimal(price_filter.get("maxPrice"), "max_price", allow_zero=True)
    if maximum_price and minimum_price > maximum_price:
        raise MarketDataInvalid("contradictory_price_filter")
    minimums: list[Decimal] = []
    maximums: list[Decimal] = []
    needs_average = False
    for kind in ("MIN_NOTIONAL", "NOTIONAL"):
        item = by_type.get(kind)
        if item is None:
            continue
        average_minutes = item.get("avgPriceMins")
        if type(average_minutes) is not int or not 0 <= average_minutes <= 1000:
            raise MarketDataInvalid("invalid_average_price_window")
        min_applies = (
            item.get("applyToMarket") if kind == "MIN_NOTIONAL" else item.get("applyMinToMarket")
        )
        if type(min_applies) is not bool:
            raise MarketDataInvalid("unverified_market_notional_rule")
        if min_applies:
            minimums.append(_decimal(item.get("minNotional"), "min_notional", allow_zero=True))
            needs_average = needs_average or average_minutes > 0
        if kind == "NOTIONAL":
            max_applies = item.get("applyMaxToMarket")
            if type(max_applies) is not bool:
                raise MarketDataInvalid("unverified_market_notional_rule")
            if max_applies:
                maximums.append(_decimal(item.get("maxNotional"), "max_notional"))
                needs_average = needs_average or average_minutes > 0
    minimum_notional = max(minimums) if minimums else Decimal(0)
    maximum_notional = min(maximums) if maximums else None
    if maximum_notional is not None and minimum_notional > maximum_notional:
        raise MarketDataInvalid("contradictory_notional_filters")
    return SymbolCostRules(
        symbol, tick, minimum_price, maximum_price, lot, market_lot,
        minimum_notional, maximum_notional, needs_average,
        tuple(sorted(set(by_type) - KNOWN_FILTERS)),
    )


def _http_response(response: httpx.Response) -> None:
    if response.status_code in (418, 429):
        raise MarketRateLimited(
            response.status_code, _retry_after(response.headers.get("Retry-After"))
        )
    if response.status_code != 200:
        raise MarketDataUnavailable("public_spot_cost_http_failure")


class PublicSpotCostClient:
    """Two bounded PUBLIC GETs, fixed host; no keys, signatures, redirects or retries."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or value <= 0:
            raise MarketDataInvalid("invalid_public_cost_clock")
        return value

    def observe(self, *, symbol: str) -> PublicBookCostSample:
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
                exchange = client.get("/api/v3/exchangeInfo", params={"symbol": symbol})
                _http_response(exchange)
                ticker = client.get("/api/v3/ticker/bookTicker", params={"symbol": symbol})
                _http_response(ticker)
        except httpx.RequestError as exc:
            raise MarketDataUnavailable("public_spot_cost_network_failure") from exc
        received = self.now_ms()
        if not 0 <= received - requested <= MAX_AGE_MS:
            raise MarketDataInvalid("stale_public_cost_request")
        try:
            exchange_data: Any = exchange.json()
            book_data: Any = ticker.json()
        except ValueError as exc:
            raise MarketDataInvalid("malformed_public_spot_cost_json") from exc
        rules = _rules(exchange_data, symbol)
        if (
            not isinstance(book_data, dict)
            or set(book_data) != {"symbol", "bidPrice", "askPrice", "bidQty", "askQty"}
            or book_data["symbol"] != symbol
        ):
            raise MarketDataInvalid("invalid_public_book_ticker")
        bid = _decimal(book_data["bidPrice"], "bid")
        ask = _decimal(book_data["askPrice"], "ask")
        bid_qty = _decimal(book_data["bidQty"], "bid_qty")
        ask_qty = _decimal(book_data["askQty"], "ask_qty")
        if (
            bid >= ask
            or bid % rules.tick != 0
            or ask % rules.tick != 0
            or (rules.minimum_price > 0 and bid < rules.minimum_price)
            or (rules.maximum_price > 0 and ask > rules.maximum_price)
        ):
            raise MarketDataInvalid("crossed_or_off_tick_public_book")
        return PublicBookCostSample(
            symbol, bid, ask, bid_qty, ask_qty, requested, received, rules
        )


def _bounded(value: object, minimum: Decimal, maximum: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and minimum <= value <= maximum


def _fits(quantity: Decimal, rule: LotConstraint) -> bool:
    return rule.minimum <= quantity <= rule.maximum and quantity % rule.step == 0


def review_hypothetical_spot_cost(
    sample: PublicBookCostSample,
    *,
    quantity: Decimal,
    projected_gross_edge_bps: Decimal,
    hypothetical_taker_fee_bps: Decimal | None,
    stress_slippage_bps: Decimal,
    required_net_edge_bps: Decimal,
    now_ms: int,
) -> CostReview:
    """Always simulation-only: actual account tier is NOT authenticated."""
    if (
        not isinstance(sample, PublicBookCostSample)
        or sample.source != "binance_public_spot_book_ticker"
        or sample.exchange_quote_time_ms is not None
        or sample.symbol not in SUPPORTED_SYMBOLS
        or not isinstance(sample.rules, SymbolCostRules)
        or sample.rules.symbol != sample.symbol
        or type(now_ms) is not int
        or type(sample.requested_at_ms) is not int
        or type(sample.received_at_ms) is not int
        or not _bounded(sample.bid, Decimal("0.000000000001"), Decimal("1e20"))
        or not _bounded(sample.ask, Decimal("0.000000000001"), Decimal("1e20"))
        or sample.bid >= sample.ask
        or not _bounded(sample.bid_quantity, Decimal("0.000000000001"), Decimal("1e20"))
        or not _bounded(sample.ask_quantity, Decimal("0.000000000001"), Decimal("1e20"))
    ):
        return CostReview(CostReviewStatus.INVALID_SOURCE)
    if (
        sample.received_at_ms < sample.requested_at_ms
        or sample.received_at_ms - sample.requested_at_ms > MAX_AGE_MS
        or now_ms < sample.received_at_ms
        or now_ms - sample.received_at_ms > MAX_AGE_MS
    ):
        return CostReview(CostReviewStatus.STALE)
    spread = sample.spread_bps
    if spread > 100:
        return CostReview(CostReviewStatus.SPREAD_TOO_WIDE, spread)
    rules = sample.rules
    if rules.needs_unavailable_average or rules.other_filters:
        return CostReview(CostReviewStatus.UNEVALUATED_FILTER, spread)
    if (
        not _bounded(quantity, Decimal("0.000000000001"), Decimal("1e20"))
        or not _fits(quantity, rules.lot)
        or (rules.market_lot is not None and not _fits(quantity, rules.market_lot))
        or quantity * sample.bid < rules.minimum_notional
        or (rules.maximum_notional is not None and quantity * sample.ask > rules.maximum_notional)
    ):
        return CostReview(CostReviewStatus.MARKET_SIZE_INVALID, spread)
    if quantity > min(sample.bid_quantity, sample.ask_quantity):
        return CostReview(CostReviewStatus.TOP_BOOK_LIQUIDITY, spread)
    if hypothetical_taker_fee_bps is None:
        return CostReview(CostReviewStatus.ACCOUNT_FEE_UNKNOWN, spread)
    if (
        not _bounded(hypothetical_taker_fee_bps, Decimal("0.000001"), Decimal("100"))
        or not _bounded(stress_slippage_bps, Decimal(0), Decimal(100))
        or not _bounded(projected_gross_edge_bps, Decimal(0), BPS)
        or not _bounded(required_net_edge_bps, Decimal(0), BPS)
    ):
        return CostReview(CostReviewStatus.INVALID_STRESS, spread)
    fee = hypothetical_taker_fee_bps / BPS
    slip = stress_slippage_bps / BPS
    buy_cost = sample.ask * (1 + slip) * (1 + fee)
    sell_proceeds = sample.bid * (1 - slip) * (1 - fee)
    roundtrip_cost = (1 - sell_proceeds / buy_cost) * BPS
    projected_net = projected_gross_edge_bps - roundtrip_cost
    return CostReview(
        CostReviewStatus.SIMULATION_ONLY
        if projected_net > required_net_edge_bps
        else CostReviewStatus.NEGATIVE_NET_EDGE,
        spread,
        roundtrip_cost,
        projected_net,
    )
