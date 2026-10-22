"""BT-022: parse UNTRUSTED account fee fixtures and stress-test PAPER cost.

No authenticated USER_DATA reader, API key, storage, worker or order access.
Binance account/commission response data without a signed trusted adapter is
NEVER proof of the owner's actual fees. Discounts are deliberately ignored.
All outputs remain research-only; `trade_authorized` is always False.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from binance_ai_trader.market_data import SUPPORTED_SYMBOLS
from binance_ai_trader.public_spot_coherence import (
    CoherenceStatus,
    review_public_spot_coherence,
)
from binance_ai_trader.public_spot_cost_review import (
    PublicBookCostSample,
    SymbolCostRules,
)
from binance_ai_trader.public_spot_depth_review import (
    DepthReviewStatus,
    PublicSpotDepth,
    review_hypothetical_spot_depth,
)
from binance_ai_trader.public_spot_observation import PublicSpotObservation

BPS = Decimal("10000")
ZERO = Decimal(0)
MAX_OFFLINE_FEE_AGE_MS = 120_000
MAX_RATE_COMPONENT = Decimal("1")
MAX_STRESS_TAKER_BPS = Decimal("100")
OFFLINE_SOURCE = "unverified_offline_account_commission_fixture"


class OfflineCommissionInvalid(ValueError):
    """Untrusted commission data is malformed, inconsistent or unsupported."""


@dataclass(frozen=True, slots=True)
class RateComponents:
    maker: Decimal
    taker: Decimal
    buyer: Decimal
    seller: Decimal

    def for_taker_side(self, *, buy: bool) -> Decimal:
        return self.taker + (self.buyer if buy else self.seller)


@dataclass(frozen=True, slots=True)
class OfflineCommissionFixture:
    """Parsed UNVERIFIED fixture, not a token/credential/signed observation."""

    symbol: str
    standard: RateComponents
    tax: RateComponents
    special: RateComponents
    received_at_ms: int
    discount_enabled: bool
    source: str = OFFLINE_SOURCE

    @property
    def hypothetical_buy_taker_bps(self) -> Decimal:
        return BPS * sum(
            (part.for_taker_side(buy=True) for part in (self.standard, self.tax, self.special)),
            ZERO,
        )

    @property
    def hypothetical_sell_taker_bps(self) -> Decimal:
        return BPS * sum(
            (part.for_taker_side(buy=False) for part in (self.standard, self.tax, self.special)),
            ZERO,
        )


class FeeCostStatus(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    MARKET_DATA_INCOHERENT = "MARKET_DATA_INCOHERENT"
    ACCOUNT_FEE_MISSING = "ACCOUNT_FEE_MISSING"
    FEE_SYMBOL_MISMATCH = "FEE_SYMBOL_MISMATCH"
    FEE_STALE = "FEE_STALE"
    FEE_UNTRUSTED_OR_CORRUPT = "FEE_UNTRUSTED_OR_CORRUPT"
    FEE_EXCESSIVE_OR_ZERO = "FEE_EXCESSIVE_OR_ZERO"
    UNKNOWN_MARKET_FILTER = "UNKNOWN_MARKET_FILTER"
    SIZE_OR_NOTIONAL_INVALID = "SIZE_OR_NOTIONAL_INVALID"
    DEPTH_REJECTED = "DEPTH_REJECTED"
    COST_EXCEEDS_EXPECTED_EDGE = "COST_EXCEEDS_EXPECTED_EDGE"
    UNVERIFIED_FEE_SIMULATION_ONLY = "UNVERIFIED_FEE_SIMULATION_ONLY"


@dataclass(frozen=True, slots=True)
class FeeCostReview:
    status: FeeCostStatus
    hypothetical_buy_taker_bps: Decimal | None = None
    hypothetical_sell_taker_bps: Decimal | None = None
    conservative_roundtrip_cost_bps: Decimal | None = None
    hypothetical_net_edge_bps: Decimal | None = None

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _fraction(raw: object, *, name: str) -> Decimal:
    if type(raw) is not str:
        raise OfflineCommissionInvalid("invalid_" + name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise OfflineCommissionInvalid("invalid_" + name) from exc
    if not value.is_finite() or not ZERO <= value <= MAX_RATE_COMPONENT:
        raise OfflineCommissionInvalid("invalid_" + name)
    return value


def _components(payload: object, *, name: str) -> RateComponents:
    if not isinstance(payload, dict) or set(payload) != {"maker", "taker", "buyer", "seller"}:
        raise OfflineCommissionInvalid("invalid_" + name + "_shape")
    return RateComponents(
        maker=_fraction(payload["maker"], name=name + "_maker"),
        taker=_fraction(payload["taker"], name=name + "_taker"),
        buyer=_fraction(payload["buyer"], name=name + "_buyer"),
        seller=_fraction(payload["seller"], name=name + "_seller"),
    )


def parse_offline_account_commission_fixture(
    payload: object, *, symbol: str, received_at_ms: int
) -> OfflineCommissionFixture:
    """Strictly parse the Binance Spot account-commission RESPONSE SHAPE.

    Never call the private endpoint. A syntactically valid response dictionary
    CANNOT attest authenticated origin or effective account commission.
    """
    if (
        symbol not in SUPPORTED_SYMBOLS
        or type(received_at_ms) is not int
        or received_at_ms <= 0
        or not isinstance(payload, dict)
        or set(payload)
        != {
            "symbol",
            "standardCommission",
            "taxCommission",
            "specialCommission",
            "discount",
        }
        or payload["symbol"] != symbol
    ):
        raise OfflineCommissionInvalid("invalid_offline_fee_envelope")
    discount: Any = payload["discount"]
    if not isinstance(discount, dict) or set(discount) != {
        "enabledForAccount",
        "enabledForSymbol",
        "discountAsset",
        "discount",
    }:
        raise OfflineCommissionInvalid("invalid_offline_discount_shape")
    if (
        type(discount["enabledForAccount"]) is not bool
        or type(discount["enabledForSymbol"]) is not bool
        or type(discount["discountAsset"]) is not str
        or not 1 <= len(discount["discountAsset"]) <= 16
    ):
        raise OfflineCommissionInvalid("invalid_offline_discount_details")
    _fraction(discount["discount"], name="discount")
    return OfflineCommissionFixture(
        symbol=symbol,
        standard=_components(payload["standardCommission"], name="standard"),
        tax=_components(payload["taxCommission"], name="tax"),
        special=_components(payload["specialCommission"], name="special"),
        received_at_ms=received_at_ms,
        discount_enabled=discount["enabledForAccount"] and discount["enabledForSymbol"],
    )


def _decimal_ok(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (value > ZERO if positive else value >= ZERO)
    )


def _rates_intact(components: object) -> bool:
    if type(components) is not RateComponents:
        return False
    return all(
        _decimal_ok(part) and part <= MAX_RATE_COMPONENT
        for part in (components.maker, components.taker, components.buyer, components.seller)
    )


def _quantity_fits(amount: Decimal, rules: SymbolCostRules, bid: Decimal, ask: Decimal) -> bool:
    lot = rules.lot
    if (
        not _decimal_ok(lot.step, positive=True)
        or not _decimal_ok(lot.minimum)
        or not _decimal_ok(lot.maximum, positive=True)
        or (
            rules.market_lot is not None
            and (
                not _decimal_ok(rules.market_lot.step, positive=True)
                or not _decimal_ok(rules.market_lot.minimum)
                or not _decimal_ok(rules.market_lot.maximum, positive=True)
            )
        )
    ):
        return False
    if not (lot.minimum <= amount <= lot.maximum and amount % lot.step == ZERO) or (
        rules.market_lot is not None
        and not (
            rules.market_lot.minimum <= amount <= rules.market_lot.maximum
            and amount % rules.market_lot.step == ZERO
        )
    ):
        return False
    return amount * bid >= rules.minimum_notional and (
        rules.maximum_notional is None or amount * ask <= rules.maximum_notional
    )


def review_offline_fee_adjusted_spot_depth(
    *,
    observation: PublicSpotObservation,
    book: PublicBookCostSample,
    depth: PublicSpotDepth,
    offline_fee: OfflineCommissionFixture | None,
    expected_symbol: str,
    quantity: Decimal,
    adverse_slippage_bps: Decimal,
    projected_gross_edge_bps: Decimal,
    minimum_net_edge_bps: Decimal,
    maximum_depth_impact_bps: Decimal,
    now_ms: int,
) -> FeeCostReview:
    """Research-only, conservative two-side fee + depth VWAP stress test.

    Parses all taker/buyer/seller rate components; ignores optional BNB discounts
    and uses the larger per-side rate for BOTH directions. This can overstate
    costs. It cannot authenticate fees, infer liquidity or authorize trading.
    """
    if (
        expected_symbol not in SUPPORTED_SYMBOLS
        or type(now_ms) is not int
        or now_ms <= 0
        or not _decimal_ok(quantity, positive=True)
    ):
        return FeeCostReview(FeeCostStatus.INVALID_INPUT)
    coherence = review_public_spot_coherence(
        observation, book, depth, expected_symbol=expected_symbol, now_ms=now_ms
    )
    if coherence.status is not CoherenceStatus.COHERENT_RESEARCH_ONLY:
        return FeeCostReview(FeeCostStatus.MARKET_DATA_INCOHERENT)
    if offline_fee is None:
        return FeeCostReview(FeeCostStatus.ACCOUNT_FEE_MISSING)
    if type(offline_fee) is not OfflineCommissionFixture or offline_fee.symbol != expected_symbol:
        return FeeCostReview(FeeCostStatus.FEE_SYMBOL_MISMATCH)
    if (
        type(offline_fee.received_at_ms) is not int
        or offline_fee.received_at_ms <= 0
        or now_ms < offline_fee.received_at_ms
        or now_ms - offline_fee.received_at_ms > MAX_OFFLINE_FEE_AGE_MS
    ):
        return FeeCostReview(FeeCostStatus.FEE_STALE)
    if (
        offline_fee.source != OFFLINE_SOURCE
        or type(offline_fee.discount_enabled) is not bool
        or not all(
            _rates_intact(part)
            for part in (offline_fee.standard, offline_fee.tax, offline_fee.special)
        )
    ):
        return FeeCostReview(FeeCostStatus.FEE_UNTRUSTED_OR_CORRUPT)
    buy_fee = offline_fee.hypothetical_buy_taker_bps
    sell_fee = offline_fee.hypothetical_sell_taker_bps
    if min(buy_fee, sell_fee) <= ZERO or max(buy_fee, sell_fee) > MAX_STRESS_TAKER_BPS:
        return FeeCostReview(FeeCostStatus.FEE_EXCESSIVE_OR_ZERO, buy_fee, sell_fee)
    rules = book.rules
    if rules.needs_unavailable_average or rules.other_filters:
        return FeeCostReview(FeeCostStatus.UNKNOWN_MARKET_FILTER, buy_fee, sell_fee)
    if not _quantity_fits(quantity, rules, book.bid, book.ask):
        return FeeCostReview(FeeCostStatus.SIZE_OR_NOTIONAL_INVALID, buy_fee, sell_fee)
    depth_result = review_hypothetical_spot_depth(
        depth,
        expected_symbol=expected_symbol,
        quantity=quantity,
        hypothetical_taker_fee_bps=max(buy_fee, sell_fee),
        adverse_slippage_bps=adverse_slippage_bps,
        projected_gross_edge_bps=projected_gross_edge_bps,
        minimum_net_edge_bps=minimum_net_edge_bps,
        maximum_depth_impact_bps=maximum_depth_impact_bps,
        now_ms=now_ms,
    )
    if depth_result.status not in (
        DepthReviewStatus.SIMULATION_ONLY,
        DepthReviewStatus.NEGATIVE_NET_EDGE,
    ):
        return FeeCostReview(FeeCostStatus.DEPTH_REJECTED, buy_fee, sell_fee)
    return FeeCostReview(
        FeeCostStatus.UNVERIFIED_FEE_SIMULATION_ONLY
        if depth_result.status is DepthReviewStatus.SIMULATION_ONLY
        else FeeCostStatus.COST_EXCEEDS_EXPECTED_EDGE,
        buy_fee,
        sell_fee,
        depth_result.roundtrip_cost_bps,
        depth_result.hypothetical_net_edge_bps,
    )
