"""Paper-only risk decisions are deterministic, decimal and fail closed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from binance_ai_trader.paper_risk import (
    PaperRiskRequest,
    PaperRiskState,
    PaperRiskStatus,
    evaluate_paper_risk,
)
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def safe_state(
    *,
    equity: str = "1000",
    start: str = "1000",
    peak: str = "1000",
    reserved: str = "0",
    positions: int = 0,
) -> PaperRiskState:
    return PaperRiskState(D(equity), D(start), D(peak), D(reserved), positions)


def spot_settings(**values: object) -> RiskSettings:
    return RiskSettings.model_validate({
        "market_type": "spot",
        "spot_capital_usdt": 1000,
        "bot_enabled": True,
        "manual_approval": False,
        **values,
    })


def test_disabled_bot_always_rejects() -> None:
    result = evaluate_paper_risk(
        RiskSettings(),
        PaperRiskRequest(D("100"), D("1")),
        safe_state(),
    )
    assert result.status is PaperRiskStatus.REJECTED
    assert result.reason == "paper_bot_disabled"


def test_spot_eligible_maximum_notional_and_decimal_loss() -> None:
    result = evaluate_paper_risk(
        spot_settings(),
        PaperRiskRequest(D("100.25"), D("1")),
        safe_state(),
    )
    assert result.status is PaperRiskStatus.ELIGIBLE
    assert result.estimated_stop_loss_usdt == D("1.0025")
    assert result.max_notional_usdt == D("250")


def test_manual_approval_only_marks_request_pending() -> None:
    result = evaluate_paper_risk(
        spot_settings(manual_approval=True),
        PaperRiskRequest(D("100"), D("1")),
        safe_state(),
    )
    assert result.status is PaperRiskStatus.APPROVAL_REQUIRED


@pytest.mark.parametrize(
    ("stop", "notional"),
    [
        ("0", "100"),
        ("-1", "100"),
        ("NaN", "100"),
        ("Infinity", "100"),
        ("25", "100"),
        ("1", "0"),
        ("1", "-3"),
        ("1", "NaN"),
    ],
)
def test_bad_stop_or_notional_rejected(stop: str, notional: str) -> None:
    result = evaluate_paper_risk(
        spot_settings(), PaperRiskRequest(D(notional), D(stop)), safe_state()
    )
    assert result.reason == "invalid_stop_or_notional"


def test_spot_does_not_allow_leverage() -> None:
    decision = evaluate_paper_risk(
        spot_settings(),
        PaperRiskRequest(D("100"), D("1"), leverage=2),
        safe_state(),
    )
    assert decision.reason == "leverage_out_of_bounds"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (safe_state(equity="984"), "daily_loss_limit"),
        (safe_state(equity="949", peak="1000", start="950"), "maximum_drawdown_limit"),
        (safe_state(positions=3), "maximum_open_positions"),
        (safe_state(reserved="1000"), "paper_capital_unavailable"),
        (safe_state(reserved="950"), "paper_capital_limit"),
        (safe_state(equity="NaN"), "invalid_paper_account_state"),
        (safe_state(reserved="-1"), "invalid_paper_account_state"),
    ],
)
def test_account_risk_guardrails(state: PaperRiskState, expected: str) -> None:
    req = PaperRiskRequest(D("100"), D("1"))
    decision = evaluate_paper_risk(spot_settings(), req, state)
    assert decision.status is PaperRiskStatus.REJECTED
    assert decision.reason == expected


def test_per_trade_stop_risk_budget_enforced() -> None:
    decision = evaluate_paper_risk(
        spot_settings(), PaperRiskRequest(D("300"), D("1")), safe_state()
    )
    assert decision.reason == "estimated_trade_risk_limit"
    assert decision.max_notional_usdt == D("250")


def test_futures_paper_uses_separate_margin_budget_and_leverage() -> None:
    config = RiskSettings(
        market_type="futures",
        spot_capital_usdt=1000,
        futures_capital_usdt=200,
        max_leverage=3,
        risk_per_trade_pct=2,
        bot_enabled=True,
        manual_approval=False,
    )
    passing = evaluate_paper_risk(
        config, PaperRiskRequest(D("300"), D("1"), leverage=3), safe_state()
    )
    assert passing.status is PaperRiskStatus.ELIGIBLE
    assert passing.max_notional_usdt == D("400")

    excessive = evaluate_paper_risk(
        config,
        PaperRiskRequest(D("300"), D("1"), leverage=3),
        safe_state(reserved="120"),
    )
    assert excessive.reason == "paper_capital_limit"

    unsupported = evaluate_paper_risk(
        config, PaperRiskRequest(D("100"), D("1"), leverage=4), safe_state()
    )
    assert unsupported.reason == "leverage_out_of_bounds"


def test_risk_engine_never_mutates_account_state() -> None:
    context = safe_state()
    proposed = PaperRiskRequest(D("80"), D("1"))
    first = evaluate_paper_risk(spot_settings(), proposed, context)
    second = evaluate_paper_risk(spot_settings(), proposed, context)
    assert first == second
    assert context.reserved_capital_usdt == 0
