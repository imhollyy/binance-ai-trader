from __future__ import annotations

from decimal import Decimal

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_shadow_preflight import (
    ShadowEvidence,
    ShadowPreflightResult,
    evaluate_shadow_preflight,
)
from binance_ai_trader.ai_trainable_model import ModelAction, ResearchPrediction
from binance_ai_trader.risk_guard import RiskProfile, RiskReason, RiskRequest, evaluate_risk


def _profile(**changes: object) -> RiskProfile:
    values: dict[str, object] = dict(
        schema_version="1.0",
        profile_version=2,
        risk_level=2,
        max_positions=2,
        risk_per_trade_pct=Decimal("1"),
        max_open_capital=Decimal("500"),
        spot_budget=Decimal("500"),
        daily_loss_pct=Decimal("3"),
        max_drawdown_pct=Decimal("10"),
        max_consecutive_losses=3,
        user_hard_limits=(("capital", Decimal("400")),),
    )
    values.update(changes)
    return RiskProfile(**values)  # type: ignore[arg-type]


def _request(**changes: object) -> RiskRequest:
    values: dict[str, object] = dict(
        settings_version=2,
        bot_state=BotState.RUNNING,
        proposed_notional=Decimal("100"),
        stop_loss_price=Decimal("99"),
        take_profit_price=Decimal("102"),
        reference_price=Decimal("100"),
        fee_bps_round_trip=Decimal("5"),
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        expected_gross_edge_bps=Decimal("20"),
        min_net_edge_bps=Decimal("5"),
        current_open_capital=Decimal("0"),
        open_positions=0,
        equity=Decimal("1000"),
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
        consecutive_losses=0,
    )
    values.update(changes)
    return RiskRequest(**values)  # type: ignore[arg-type]


def test_risk_guard_limits_modes_and_unverified_edges_fail_closed() -> None:
    allowed = evaluate_risk(_profile(), _request())
    assert allowed.allowed and allowed.reason is RiskReason.ALLOW
    assert not evaluate_risk(_profile(), _request(settings_version=1)).allowed
    assert (
        evaluate_risk(
            _profile(), _request(day_start_equity=Decimal("1000"), equity=Decimal("970"))
        ).reason
        is RiskReason.LOSS_LIMIT
    )
    assert (
        evaluate_risk(
            _profile(),
            _request(proposed_notional=Decimal("101"), current_open_capital=Decimal("300")),
        ).reason
        is RiskReason.CAPITAL_LIMIT
    )
    assert (
        evaluate_risk(_profile(risk_level=5), _request(expected_gross_edge_bps=Decimal("6"))).reason
        is RiskReason.NET_EDGE_UNVERIFIED
    )
    assert (
        evaluate_risk(_profile(), _request(bot_state=BotState.PAUSED)).reason
        is RiskReason.PROTECTION_UNDEFINED
    )
    assert (
        evaluate_risk(_profile(), _request(bot_state=BotState.EMERGENCY_FLATTEN)).reason
        is RiskReason.EMERGENCY_ENGINE_UNAVAILABLE
    )
    assert (
        evaluate_risk(_profile(), _request(consecutive_losses=3)).reason is RiskReason.LOSS_STREAK
    )


def test_shadow_chain_audits_paper_suitability_without_order_intent() -> None:
    prediction = ResearchPrediction(
        ModelAction.BUY,
        "shadow_signal_0001",
        "model-v1",
        "1.0",
        "BTCUSDT",
        "1m",
        60_000,
        960_000,
        None,
        False,
        "UNCALIBRATED_RESEARCH_ONLY",
        "a" * 64,
        "b" * 64,
    )
    evidence = ShadowEvidence(
        "BTCUSDT",
        "1m",
        60_000,
        60_001,
        120_000,
        Decimal("100"),
        Decimal("5"),
        Decimal("1"),
        Decimal("1"),
        "c" * 64,
        "d" * 64,
        True,
        True,
    )

    class Recorder:
        def __init__(self) -> None:
            self.recorded: list[tuple[ResearchPrediction, ShadowPreflightResult]] = []

        def __call__(self, row: ResearchPrediction, result: ShadowPreflightResult) -> None:
            self.recorded.append((row, result))

    audit = Recorder()

    result = evaluate_shadow_preflight(
        prediction,
        _profile(),
        _request(),
        evidence,
        now_ms=70_000,
        max_data_age_ms=60_000,
        audit=audit,
    )
    assert result.risk_decision is not None and result.risk_decision.allowed
    assert result.status == "BLOCKED"
    assert result.reason_code == "MODEL_CALIBRATION_UNVERIFIED"
    assert not result.paper_suitability and not result.order_intent_created
    assert audit.recorded == [(prediction, result)]
