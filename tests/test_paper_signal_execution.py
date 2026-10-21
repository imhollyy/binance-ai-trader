"""PAPER-only signal execution must fail closed before any database mutation."""

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.paper_backtest import BacktestDecision
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_signal_execution import _identity, execute_paper_signal_once
from binance_ai_trader.paper_signal_plan import (
    PaperSignalPlan,
    SignalPlanStatus,
    SourceState,
    _stable_key,
)
from binance_ai_trader.paper_strategy import SmaCrossParameters

PARAMS = SmaCrossParameters(fast_window=3, slow_window=6)


def candidate() -> PaperSignalPlan:
    return PaperSignalPlan(
        status=SignalPlanStatus.CANDIDATE,
        reason="paper_diagnostic_only_next_open_unknown",
        symbol="BTCUSDT",
        interval="1m",
        evaluated_at_ms=120_001,
        latest_close_time_ms=119_999,
        checked_candles=60,
        settings_version=1,
        decision=BacktestDecision(
            119_999, "buy", Decimal("1"), Decimal("99")
        ),
        idempotency_key=_stable_key("BTCUSDT", "1m", 119_999, PARAMS, "buy"),
    )


def test_stable_key_includes_settings_version_but_trade_id_stays_locked() -> None:
    first = candidate()
    second = replace(first, settings_version=2)
    key1, trade1 = _identity(first)
    key2, trade2 = _identity(second)
    assert key1 != key2
    assert trade1 == trade2
    assert len(key1) < 128
    assert len(trade1) < 64
    assert _identity(first) == _identity(first)


def test_invalid_signals_cannot_start_any_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        original = candidate()
        changes = [
            replace(original, status=SignalPlanStatus.BLOCKED),
            replace(original, status=SignalPlanStatus.NO_SIGNAL),
            replace(original, decision=None),
            replace(original, idempotency_key=None),
            replace(original, settings_version=None),
        ]
        for plan in changes:
            with pytest.raises(PaperExecutionRejected):
                execute_paper_signal_once(
                    engine,
                    plan,
                    source_state=SourceState.HEALTHY,
                    source_checked_at_ms=180_001,
                    params=PARAMS,
                    now_ms=180_001,
                )
        for plan in [
            replace(original, idempotency_key="invented"),
            replace(original, latest_close_time_ms=120_000),
            replace(original, evaluated_at_ms=180_001),
            replace(original, decision=replace(original.decision, explicit_approval=True)),
        ]:
            with pytest.raises(PaperExecutionRejected):
                execute_paper_signal_once(
                    engine,
                    plan,
                    source_state=SourceState.HEALTHY,
                    source_checked_at_ms=180_001,
                    params=PARAMS,
                    now_ms=180_001,
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", [None, SourceState.RATE_LIMITED, SourceState.UNAVAILABLE])
def test_untrusted_source_is_never_authorized(state: SourceState | None) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(PaperExecutionRejected):
            execute_paper_signal_once(
                engine, candidate(), source_state=state,
                source_checked_at_ms=180_001, params=PARAMS, now_ms=180_001,
            )
    finally:
        engine.dispose()
