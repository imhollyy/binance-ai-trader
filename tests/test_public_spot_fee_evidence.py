"""BT-022 synthetic commission component, freshness, filter and net-PAPER-cost tests.

NO authenticated Binance request, actual wallet/order or owner PAPER write.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from binance_ai_trader.market_data import Candle
from binance_ai_trader.public_spot_cost_review import (
    LotConstraint,
    PublicBookCostSample,
    SymbolCostRules,
)
from binance_ai_trader.public_spot_depth_review import DepthLevel, PublicSpotDepth
from binance_ai_trader.public_spot_fee_evidence import (
    FeeCostReview,
    FeeCostStatus,
    OfflineCommissionInvalid,
    OfflineCommissionFixture,
    parse_offline_account_commission_fixture,
    review_offline_fee_adjusted_spot_depth,
)
from binance_ai_trader.public_spot_observation import PublicSpotObservation

D = Decimal
START = 1_800_000_180_000
NOW = START + 50
FINAL = NOW + 20
SIZE = Decimal("3")
SLIPPAGE = Decimal("5")
EDGE = Decimal("100")
MINIMUM = Decimal("10")
IMPACT = Decimal("30")


def fake_fee_payload() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "standardCommission": {
            "maker": "0.0001",
            "taker": "0.0006",
            "buyer": "0.0001",
            "seller": "0.0002",
        },
        "taxCommission": {
            "maker": "0.000001",
            "taker": "0.00001",
            "buyer": "0.000001",
            "seller": "0.000002",
        },
        "specialCommission": {
            "maker": "0",
            "taker": "0.00001",
            "buyer": "0",
            "seller": "0",
        },
        "discount": {
            "enabledForAccount": True,
            "enabledForSymbol": True,
            "discountAsset": "BNB",
            "discount": "0.25",
        },
    }


def snapshot() -> OfflineCommissionFixture:
    return parse_offline_account_commission_fixture(
        fake_fee_payload(), symbol="BTCUSDT", received_at_ms=NOW
    )


def market() -> tuple[PublicSpotObservation, PublicBookCostSample, PublicSpotDepth]:
    candles = tuple(
        Candle(
            "BTCUSDT",
            "1m",
            START - (60 - idx) * 60_000,
            START - (60 - idx - 1) * 60_000 - 1,
            D("100"),
            D("101"),
            D("99"),
            D("100"),
            D("20"),
        )
        for idx in range(60)
    )
    observation = PublicSpotObservation(
        "BTCUSDT",
        "1m",
        candles,
        D("100.00"),
        NOW,
        NOW + 5,
    )
    rules = SymbolCostRules(
        "BTCUSDT",
        D("0.01"),
        D("0.01"),
        D("1000000"),
        LotConstraint(D("0.01"), D("10"), D("0.01")),
        LotConstraint(D("0.01"), D("5"), D("0.01")),
        D("10"),
        D("10000"),
        False,
        (),
    )
    book = PublicBookCostSample(
        "BTCUSDT",
        D("99.90"),
        D("100.10"),
        D("2"),
        D("2"),
        NOW + 5,
        NOW + 10,
        rules,
    )
    depth = PublicSpotDepth(
        "BTCUSDT",
        10,
        (DepthLevel(D("99.90"), D("2")), DepthLevel(D("99.80"), D("2"))),
        (DepthLevel(D("100.10"), D("2")), DepthLevel(D("100.20"), D("2"))),
        NOW + 10,
        NOW + 15,
    )
    return observation, book, depth


def cost(
    *,
    fee: OfflineCommissionFixture | None = None,
    no_fee: bool = False,
    inputs: tuple[PublicSpotObservation, PublicBookCostSample, PublicSpotDepth] | None = None,
    quantity: Decimal = SIZE,
    slippage: Decimal = SLIPPAGE,
    gross: Decimal = EDGE,
    minimum: Decimal = MINIMUM,
    impact: Decimal = IMPACT,
    now_ms: int = FINAL,
) -> FeeCostReview:
    observation, book, depth = market() if inputs is None else inputs
    return review_offline_fee_adjusted_spot_depth(
        observation=observation,
        book=book,
        depth=depth,
        offline_fee=None if no_fee else snapshot() if fee is None else fee,
        expected_symbol="BTCUSDT",
        quantity=quantity,
        adverse_slippage_bps=slippage,
        projected_gross_edge_bps=gross,
        minimum_net_edge_bps=minimum,
        maximum_depth_impact_bps=impact,
        now_ms=now_ms,
    )


def test_offline_fee_breakdown_includes_side_taxes_and_special_without_discount() -> None:
    fee = snapshot()
    assert fee.source == "unverified_offline_account_commission_fixture"
    assert fee.discount_enabled
    assert fee.hypothetical_buy_taker_bps == D("7.21")
    assert fee.hypothetical_sell_taker_bps == D("8.22")
    result = cost()
    assert result.status is FeeCostStatus.UNVERIFIED_FEE_SIMULATION_ONLY
    assert result.hypothetical_buy_taker_bps == D("7.21")
    assert result.hypothetical_sell_taker_bps == D("8.22")
    assert result.conservative_roundtrip_cost_bps is not None
    assert D("40") < result.conservative_roundtrip_cost_bps < D("55")
    assert result.hypothetical_net_edge_bps is not None
    assert result.hypothetical_net_edge_bps > MINIMUM
    assert not result.trade_authorized and result.no_trade
    assert cost(gross=D("20")).status is FeeCostStatus.COST_EXCEEDS_EXPECTED_EDGE

    raw = fake_fee_payload()
    raw["discount"]["discount"] = "0.90"
    discounted = parse_offline_account_commission_fixture(
        raw, symbol="BTCUSDT", received_at_ms=NOW
    )
    assert discounted.hypothetical_buy_taker_bps == fee.hypothetical_buy_taker_bps
    assert discounted.hypothetical_sell_taker_bps == fee.hypothetical_sell_taker_bps
    assert cost(fee=discounted).conservative_roundtrip_cost_bps == (
        result.conservative_roundtrip_cost_bps
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"symbol": "ETHUSDT"},
        {"symbol": 200},
        {"secret": "do-not-accept"},
        {"standardCommission": {"taker": "0.0001"}},
        {"taxCommission": {}},
        {"discount": {}},
        {"discount": {"enabledForAccount": 1}},
        {
            "discount": {
                "enabledForAccount": True,
                "enabledForSymbol": True,
                "discountAsset": "",
                "discount": "0.25",
            }
        },
    ],
)
def test_corrupted_envelope_never_passes_strict_parser(patch: dict[str, Any]) -> None:
    raw = fake_fee_payload()
    raw.update(patch)
    with pytest.raises(OfflineCommissionInvalid):
        parse_offline_account_commission_fixture(raw, symbol="BTCUSDT", received_at_ms=NOW)


@pytest.mark.parametrize("bad", ["NaN", "-0.1", "Infinity", "abc", "1.1", 0.001, True])
def test_malformed_taker_fractions_rejected(bad: object) -> None:
    raw = fake_fee_payload()
    raw["standardCommission"]["taker"] = bad
    with pytest.raises(OfflineCommissionInvalid):
        parse_offline_account_commission_fixture(raw, symbol="BTCUSDT", received_at_ms=NOW)


def test_missing_mismatched_stale_corrupted_and_zero_fees_all_block() -> None:
    assert cost(no_fee=True).status is FeeCostStatus.ACCOUNT_FEE_MISSING
    assert cost(fee=replace(snapshot(), symbol="ETHUSDT")).status is (
        FeeCostStatus.FEE_SYMBOL_MISMATCH
    )
    assert cost(fee=replace(snapshot(), received_at_ms=NOW - 120_001)).status is (
        FeeCostStatus.FEE_STALE
    )
    assert cost(fee=replace(snapshot(), received_at_ms=NOW + 100)).status is (
        FeeCostStatus.FEE_STALE
    )
    assert cost(fee=replace(snapshot(), source="signed_approved")).status is (
        FeeCostStatus.FEE_UNTRUSTED_OR_CORRUPT
    )
    assert cost(fee=replace(snapshot(), discount_enabled="yes")).status is (
        FeeCostStatus.FEE_UNTRUSTED_OR_CORRUPT
    )
    all_zero = fake_fee_payload()
    for category in ("standardCommission", "taxCommission", "specialCommission"):
        for side in ("maker", "taker", "buyer", "seller"):
            all_zero[category][side] = "0"
    fee = parse_offline_account_commission_fixture(
        all_zero, symbol="BTCUSDT", received_at_ms=NOW
    )
    assert cost(fee=fee).status is FeeCostStatus.FEE_EXCESSIVE_OR_ZERO
    too_expensive = replace(snapshot(), special=replace(snapshot().special, taker=D("0.2")))
    assert cost(fee=too_expensive).status is FeeCostStatus.FEE_EXCESSIVE_OR_ZERO


def test_lot_market_notional_and_visible_depth_restrictions() -> None:
    assert cost(quantity=D("0.005")).status is FeeCostStatus.SIZE_OR_NOTIONAL_INVALID
    assert cost(quantity=D("0.01")).status is FeeCostStatus.SIZE_OR_NOTIONAL_INVALID
    assert cost(quantity=D("6")).status is FeeCostStatus.SIZE_OR_NOTIONAL_INVALID
    assert cost(quantity=D("5")).status is FeeCostStatus.DEPTH_REJECTED
    assert cost(quantity=D("3"), impact=D("1")).status is FeeCostStatus.DEPTH_REJECTED
    assert cost(slippage=D("101")).status is FeeCostStatus.DEPTH_REJECTED
    a, b, c = market()
    too_strict = replace(b.rules, market_lot=LotConstraint(D("0.01"), D("5"), D("0")))
    assert cost(inputs=(a, replace(b, rules=too_strict), c)).status is (
        FeeCostStatus.SIZE_OR_NOTIONAL_INVALID
    )


def test_market_data_mismatch_or_filter_unknown_rejected_before_fee_math() -> None:
    a, b, c = market()
    bad_data = (
        (replace(a, reference_price=D("110")), b, c),
        (a, replace(b, symbol="ETHUSDT"), c),
        (a, b, replace(c, asks=(DepthLevel(D("105"), D("2")),))),
        (a, b, replace(c, received_at_ms=c.received_at_ms - 100)),
        (a, replace(b, rules=replace(b.rules, needs_unavailable_average=True)), c),
        (a, replace(b, rules=replace(b.rules, other_filters=("UNKNOWN",))), c),
    )
    for inputs in bad_data:
        result = cost(inputs=inputs)
        assert result.status is FeeCostStatus.MARKET_DATA_INCOHERENT
        assert not result.trade_authorized and result.no_trade


def test_stale_public_bundle_or_invalid_quantity_does_not_produce_edge() -> None:
    assert cost(now_ms=FINAL + 3001).status is FeeCostStatus.MARKET_DATA_INCOHERENT
    assert cost(quantity=D("NaN")).status is FeeCostStatus.INVALID_INPUT
    assert cost(quantity=D("-1")).status is FeeCostStatus.INVALID_INPUT
