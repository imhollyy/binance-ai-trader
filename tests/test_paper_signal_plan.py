"""BT-006A: fail-closed run-once diagnostic never executes an order."""

from __future__ import annotations

from decimal import Decimal

import pytest

from binance_ai_trader.market_data import MarketRateLimited
from binance_ai_trader.paper_signal_plan import (
    SignalPlanStatus,
    SourceState,
    _stable_key,
    plan_paper_spot_signal_once,
)
from binance_ai_trader.paper_strategy import SmaCrossParameters

D = Decimal


def test_invalid_clock_or_market_parameters_fail_without_io() -> None:
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "source_state": SourceState.HEALTHY,
        "source_checked_at_ms": 120_000,
        "now_ms": 120_000,
    }
    for value in (-1, 0, True, "120000"):
        with pytest.raises(ValueError, match="invalid_utc_clock"):
            plan_paper_spot_signal_once(None, **(params | {"now_ms": value}))
    with pytest.raises(ValueError, match="unsupported_spot_market"):
        plan_paper_spot_signal_once(None, **(params | {"symbol": "DOGEUSDT"}))
    for limit in (0, 49, 1001, True):
        with pytest.raises(ValueError, match="invalid_history_limit"):
            plan_paper_spot_signal_once(None, **params, candles_limit=limit)


def test_fail_closed_unknown_and_rate_limited_source_state() -> None:
    for state in (None, SourceState.RATE_LIMITED, SourceState.UNAVAILABLE):
        result = plan_paper_spot_signal_once(
            None,
            symbol="BTCUSDT",
            interval="1m",
            source_state=state,
            source_checked_at_ms=120_000,
            now_ms=120_000,
        )
        assert result.status is SignalPlanStatus.BLOCKED
        assert result.decision is None
        assert result.idempotency_key is None
    for checked_at in (None, 0, 120_001):
        result = plan_paper_spot_signal_once(
            None, symbol="BTCUSDT", interval="1m",
            source_state=SourceState.HEALTHY,
            source_checked_at_ms=checked_at, now_ms=120_000,
        )
        assert result.reason == "source_unverified_or_restricted"


@pytest.mark.parametrize("status", [418, 429])
def test_exchange_restriction_error_prevents_any_database_read(status: int) -> None:
    result = plan_paper_spot_signal_once(
        None, symbol="BTCUSDT", interval="1m", source_state=SourceState.HEALTHY,
        source_checked_at_ms=120_000, now_ms=120_000,
        source_error=MarketRateLimited(status, retry_after_seconds=30),
    )
    assert result.status is SignalPlanStatus.BLOCKED
    assert result.reason == "source_error"


def test_no_postgres_connection_is_not_a_silent_no_signal() -> None:
    result = plan_paper_spot_signal_once(
        None, symbol="BTCUSDT", interval="1m",
        source_state=SourceState.HEALTHY, source_checked_at_ms=120_000, now_ms=120_000,
    )
    assert result.status is SignalPlanStatus.BLOCKED
    assert result.reason == "postgresql_required"


def test_strategy_versioned_idempotency_stable_and_sensitive() -> None:
    p = SmaCrossParameters()
    key = _stable_key("BTCUSDT", "1m", 120_000, p)
    assert key == _stable_key("BTCUSDT", "1m", 120_000, p)
    assert key != _stable_key("ETHUSDT", "1m", 120_000, p)
    assert key != _stable_key("BTCUSDT", "1m", 180_000, p)
    assert key != _stable_key(
        "BTCUSDT", "1m", 120_000,
        SmaCrossParameters(quantity=D("2")),
    )
    assert len(key) == 77
    assert all(c.isalnum() or c == "-" for c in key)
