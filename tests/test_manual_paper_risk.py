"""Manual PAPER Spot opt-in risk tests; no network, real orders or DB I/O."""

from __future__ import annotations

from decimal import Decimal

import pytest

from binance_ai_trader.paper_risk import (
    PaperRiskRequest,
    PaperRiskState,
    PaperRiskStatus,
    evaluate_paper_risk,
)
from binance_ai_trader.paper_spot import (
    PaperSpotRejected,
    SpotBuyIntent,
    new_paper_spot_book,
    simulate_spot_buy,
)
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def _state() -> PaperRiskState:
    return PaperRiskState(D("1000"), D("1000"), D("1000"), D("0"), 0)


def _buy(*, quantity: str = "0.001", stop: str = "99000") -> SpotBuyIntent:
    return SpotBuyIntent("manual_first", "BTCUSDT", D("100000"), D(quantity), D(stop))


def test_manual_buy_preserves_disabled_worker_and_requires_explicit_approval() -> None:
    settings = RiskSettings()
    assert settings.bot_enabled is False and settings.manual_approval is True
    initial = new_paper_spot_book(settings)
    with pytest.raises(PaperSpotRejected, match="paper_bot_disabled"):
        simulate_spot_buy(initial, settings, _state(), _buy())
    with pytest.raises(PaperSpotRejected, match="manual_paper_approval_required"):
        simulate_spot_buy(initial, settings, _state(), _buy(), manual_paper_trade=True)
    approved = simulate_spot_buy(
        initial, settings, _state(), _buy(), manual_paper_trade=True, explicit_approval=True
    )
    assert approved.positions[0].symbol == "BTCUSDT"
    assert approved.cash_usdt < D("1000")
    assert initial.cash_usdt == D("1000") and initial.positions == ()
    assert settings.bot_enabled is False  # Manual simulation cannot enable worker.


@pytest.mark.parametrize(
    ("changed", "name"),
    (
        ({"bot_enabled": True}, "manual_paper_approval_required"),
        ({"manual_approval": False}, "manual_paper_approval_required"),
    ),
)
def test_manual_gate_never_bypasses_bot_or_approval_configuration(
    changed: dict[str, bool], name: str
) -> None:
    settings = RiskSettings.model_validate(changed)
    with pytest.raises(PaperSpotRejected, match=name):
        simulate_spot_buy(
            new_paper_spot_book(settings),
            settings,
            _state(),
            _buy(),
            explicit_approval=True,
            manual_paper_trade=True,
        )


def test_manual_gate_keeps_all_risk_and_symbol_checks() -> None:
    settings = RiskSettings()
    for intent in (
        _buy(quantity="1"),  # Risk/capital is too large.
        _buy(stop="100000"),  # No stop protection.
        SpotBuyIntent("manual_wrong", "BNBUSDT", D("100000"), D("0.001"), D("99000")),
    ):
        with pytest.raises(PaperSpotRejected):
            simulate_spot_buy(
                new_paper_spot_book(settings),
                settings,
                _state(),
                intent,
                explicit_approval=True,
                manual_paper_trade=True,
            )


def test_default_risk_is_still_disabled_but_manual_path_needs_matching_settings() -> None:
    settings = RiskSettings()
    request = PaperRiskRequest(D("100"), D("1"))
    assert evaluate_paper_risk(settings, request, _state()).reason == "paper_bot_disabled"
    approved = evaluate_paper_risk(settings, request, _state(), manual_paper_trade=True)
    assert approved.status == PaperRiskStatus.APPROVAL_REQUIRED
    assert (
        evaluate_paper_risk(
            RiskSettings(bot_enabled=True), request, _state(), manual_paper_trade=True
        ).reason == "manual_paper_mode_required"
    )
