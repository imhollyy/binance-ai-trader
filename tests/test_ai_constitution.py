"""AI Constitution v1: strict proposals never authorize real or PAPER orders."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from binance_ai_trader.ai_constitution import (
    AiSpotProposal,
    BotState,
    ConstitutionAction,
    IndependentRiskPolicy,
    TrustedSpotEvidence,
    evaluate_constitution_spot_proposal,
)
from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
CLOSE = 1_800_000_000_000
NOW = CLOSE + 2_000


def proposal(**changes: object) -> AiSpotProposal:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "signal_id": "synthetic_signal_0001",
        "model_version": "calibrated_synthetic_v1",
        "market": "SPOT",
        "mode": "PAPER",
        "symbol": "BTCUSDT",
        "execution_timeframe": "1m",
        "observed_close_at_ms": CLOSE,
        "created_at_ms": CLOSE + 1_000,
        "valid_until_ms": CLOSE + 60_000,
        "decision": "BUY",
        "market_regime": "UPTREND",
        "reason_codes": ["SMA_CROSS_UP"],
        "probability": {"win": "0.6", "loss": "0.3", "flat": "0.1"},
        "expected_win_bps": "130",
        "expected_loss_bps": "20",
        "suggested_notional_usdt": "100",
        "stop_price": "99",
    }
    data.update(changes)
    return AiSpotProposal.model_validate(data)


def guard_policy(**changes: Any) -> IndependentRiskPolicy:
    settings = RiskSettings(
        market_type="spot",
        spot_capital_usdt=1000,
        bot_enabled=True,
        manual_approval=False,
    )
    base = IndependentRiskPolicy(
        settings=settings,
        account_state=PaperRiskState(D("1000"), D("1000"), D("1000"), D("0"), 0),
        bot_state=BotState.RUNNING,
        settings_version=3,
        account_settings_version=3,
        calibrated_model_versions=frozenset({"calibrated_synthetic_v1"}),
        min_net_edge_bps=D("10"),
        min_edge_to_cost_ratio=D("1.5"),
        max_market_age_ms=120_000,
    )
    return replace(base, **changes)


def trusted(**changes: Any) -> TrustedSpotEvidence:
    base = TrustedSpotEvidence(
        symbol="BTCUSDT",
        interval="1m",
        last_closed_at_ms=CLOSE,
        received_at_ms=CLOSE + 500,
        reference_price=D("100"),
        taker_fee_bps=D("10"),
        spread_bps=D("2"),
        slippage_bps=D("5"),
        critical_data_ok=True,
        exchange_constraints_ok=True,
    )
    return replace(base, **changes)


def verdict_reason(
    candidate: AiSpotProposal | None = None,
    policy: IndependentRiskPolicy | None = None,
    evidence: TrustedSpotEvidence | None = None,
) -> str:
    result = evaluate_constitution_spot_proposal(
        candidate or proposal(),
        policy or guard_policy(),
        evidence if evidence is not None else trusted(),
        now_ms=NOW,
    )
    return result.reason_code


def test_calibrated_synthetic_example_is_only_an_eligible_proposal() -> None:
    result = evaluate_constitution_spot_proposal(
        proposal(), guard_policy(), trusted(), now_ms=NOW
    )
    assert result.proposal_eligible
    assert result.decision is ConstitutionAction.BUY
    assert result.round_trip_cost_bps == D("32")
    assert result.expected_net_edge_bps == D("40")
    assert result.allowed_notional_usdt == D("100")
    assert not hasattr(result, "place_order")
    assert result.signal_id == "synthetic_signal_0001"


@pytest.mark.parametrize("state", list(BotState))
def test_all_non_running_modes_fail_closed(state: BotState) -> None:
    reason = verdict_reason(policy=guard_policy(bot_state=state))
    assert reason == ("BOT_NOT_RUNNING" if state is not BotState.RUNNING else "PROPOSAL_ELIGIBLE")


@pytest.mark.parametrize(
    ("risk_setting", "expected"),
    [
        ({"bot_enabled": False}, "BOT_NOT_RUNNING"),
        ({"manual_approval": True}, "BOT_NOT_RUNNING"),
        ({"allowed_symbols": ["ETHUSDT"]}, "USER_HARD_LIMIT"),
        ({"risk_per_trade_pct": 0.05}, "PORTFOLIO_RISK"),
        ({"spot_capital_usdt": 0}, "USER_HARD_LIMIT"),
    ],
)
def test_user_limits_override_even_extreme_model_edge(
    risk_setting: dict[str, object], expected: str
) -> None:
    config = guard_policy().settings.model_copy(update=risk_setting)
    assert verdict_reason(policy=guard_policy(settings=config)) == expected


def test_settings_and_account_version_must_match() -> None:
    assert verdict_reason(policy=guard_policy(account_settings_version=2)) == "USER_HARD_LIMIT"


@pytest.mark.parametrize(
    ("evidence_change", "expected"),
    [
        ({"critical_data_ok": False}, "DATA_QUALITY_FAILURE"),
        ({"last_closed_at_ms": CLOSE - 60_000}, "DATA_QUALITY_FAILURE"),
        ({"received_at_ms": CLOSE + 5_000}, "DATA_QUALITY_FAILURE"),
        ({"source": "ai_says_verified"}, "DATA_QUALITY_FAILURE"),
        ({"symbol": "ETHUSDT"}, "DATA_QUALITY_FAILURE"),
        ({"exchange_constraints_ok": False}, "EXCHANGE_CONSTRAINT"),
        ({"taker_fee_bps": D("0")}, "UNVERIFIED_TRANSACTION_COST"),
        ({"slippage_bps": D("0")}, "UNVERIFIED_TRANSACTION_COST"),
        ({"spread_bps": D("-1")}, "UNVERIFIED_TRANSACTION_COST"),
    ],
)
def test_independent_data_quality_and_costs_reject_model_claims(
    evidence_change: dict[str, object], expected: str
) -> None:
    assert verdict_reason(evidence=trusted(**evidence_change)) == expected


def test_calibration_is_not_the_models_own_claim() -> None:
    assert (
        verdict_reason(policy=guard_policy(calibrated_model_versions=frozenset()))
        == "MODEL_NOT_CALIBRATED"
    )
    assert verdict_reason(candidate=proposal(probability=None)) == "MODEL_NOT_CALIBRATED"
    assert verdict_reason(candidate=proposal(expected_win_bps=None, expected_loss_bps=None)) == (
        "MODEL_NOT_CALIBRATED"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"probability": {"win": "0.8", "loss": "0.3", "flat": "0"}},
        {"suggested_notional_usdt": 100.5},
        {"stop_price": 99.5},
        {"expected_win_bps": 20.0},
        {"market": "FUTURES_USDT_M"},
        {"mode": "LIVE"},
        {"schema_version": "0.9"},
        {"risk_per_trade_pct": 50},
        {"max_leverage": 100},
        {"decision": "LONG"},
        {"reason_codes": ["SMA CROSS UP"]},
        {"valid_until_ms": CLOSE + 1_000},
    ],
)
def test_malformed_model_json_or_limit_injection_is_rejected(invalid: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        proposal(**invalid)


def test_missing_edge_and_cost_margin_deny_entry() -> None:
    assert verdict_reason(candidate=proposal(expected_win_bps="5")) == (
        "INSUFFICIENT_EXPECTED_NET_EDGE"
    )
    assert verdict_reason(policy=guard_policy(min_net_edge_bps=D("100"))) == (
        "INSUFFICIENT_EXPECTED_NET_EDGE"
    )
    assert verdict_reason(policy=guard_policy(min_edge_to_cost_ratio=D("3"))) == (
        "INSUFFICIENT_EXPECTED_NET_EDGE"
    )
    assert verdict_reason(policy=guard_policy(min_edge_to_cost_ratio=D("0"))) == (
        "COST_POLICY_UNAVAILABLE"
    )


def test_missing_or_stale_feed_never_filled_with_fictional_prices() -> None:
    result = evaluate_constitution_spot_proposal(proposal(), guard_policy(), None, now_ms=NOW)
    assert result.decision is ConstitutionAction.NO_TRADE
    assert result.reason_code == "EXCHANGE_CONSTRAINT"
    assert verdict_reason(candidate=proposal(observed_close_at_ms=CLOSE - 60_000)) == (
        "DATA_QUALITY_FAILURE"
    )
    assert verdict_reason(candidate=proposal(valid_until_ms=CLOSE + 1_500)) == (
        "DATA_QUALITY_FAILURE"
    )


def test_hold_and_unimplemented_exit_are_never_orders() -> None:
    assert verdict_reason(candidate=proposal(decision="NO_TRADE")) == "MODEL_NO_TRADE"
    assert verdict_reason(candidate=proposal(decision="HOLD")) == "MODEL_HOLD"
    assert verdict_reason(candidate=proposal(decision="SELL")) == "EXIT_ENGINE_NOT_INTEGRATED"


def test_open_positions_drawdown_and_wide_stop_are_blocked() -> None:
    default = guard_policy()
    assert verdict_reason(
        policy=replace(
            default,
            account_state=PaperRiskState(D("1000"), D("1000"), D("1000"), D("100"), 1),
        )
    ) == "PORTFOLIO_EXPOSURE"
    assert verdict_reason(
        policy=replace(
            default,
            account_state=PaperRiskState(D("980"), D("1000"), D("1000"), D("0"), 0),
        )
    ) == "PORTFOLIO_RISK"
    assert verdict_reason(candidate=proposal(stop_price="101")) == "INVALID_STOP_LOSS"
    assert verdict_reason(candidate=proposal(stop_price="90")) == "PORTFOLIO_RISK"


def test_read_only_evaluation_and_decimal_json_serialization() -> None:
    candidate, policy, evidence = proposal(), guard_policy(), trusted()
    before = candidate.model_dump(mode="json")
    first = evaluate_constitution_spot_proposal(candidate, policy, evidence, now_ms=NOW)
    second = evaluate_constitution_spot_proposal(candidate, policy, evidence, now_ms=NOW)
    assert first == second
    assert candidate.model_dump(mode="json") == before
    assert evidence.reference_price == D("100")
    assert policy.account_state.equity_usdt == D("1000")
    assert isinstance(before["stop_price"], str)
