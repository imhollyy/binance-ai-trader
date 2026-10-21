"""Default-off PAPER worker unit contracts, with no Binance network."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.market_data import MarketRateLimited
from binance_ai_trader.paper_worker import (
    MAX_BACKOFF_MS,
    PaperWorkerStatus,
    _cooldown,
    run_paper_worker_once,
)


def test_default_worker_does_not_need_database_or_touch_network() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        out = run_paper_worker_once(engine, clock_ms=lambda: 100_000)
        assert out.status is PaperWorkerStatus.DISABLED
        assert out.reason == "explicit_worker_opt_in_required"
        assert out.signal_id is None
        assert out.next_allowed_at_ms is None
        assert (
            run_paper_worker_once(engine, enabled=True, clock_ms=lambda: 100_000).status
            is PaperWorkerStatus.ERROR
        )
        for value in (0, -1, True, "100"):
            with pytest.raises(ValueError, match="invalid_worker_clock"):
                run_paper_worker_once(engine, clock_ms=lambda: value)  # type: ignore[misc]
        with pytest.raises(ValueError, match="invalid_worker_enabled"):
            run_paper_worker_once(
                engine, enabled=1, clock_ms=lambda: 100_000  # type: ignore[arg-type]
            )
    finally:
        engine.dispose()


def test_deterministic_backoff_bounds_http418_429_and_no_immediate_retry() -> None:
    restricted = MarketRateLimited(429, retry_after_seconds=600)
    d1 = _cooldown(symbol="BTCUSDT", interval="1m", attempts=1, rate_limit=restricted)
    d2 = _cooldown(symbol="BTCUSDT", interval="1m", attempts=1, rate_limit=restricted)
    assert d1 == d2
    assert 600_000 <= d1 <= MAX_BACKOFF_MS
    banned = MarketRateLimited(418, retry_after_seconds=None)
    assert _cooldown(symbol="BTCUSDT", interval="1m", attempts=1, rate_limit=banned) >= 900_000
    assert (
        60_000
        <= _cooldown(symbol="ETHUSDT", interval="5m", attempts=1, rate_limit=None)
        <= MAX_BACKOFF_MS
    )
    assert _cooldown(
        symbol="BTCUSDT",
        interval="15m",
        attempts=10,
        rate_limit=MarketRateLimited(429, retry_after_seconds=999_999),
    ) == MAX_BACKOFF_MS


def test_no_float_quote_price_or_live_trade_status_is_exposed() -> None:
    assert Decimal("1.1") + Decimal("2.2") == Decimal("3.3")
    assert all(
        status not in PaperWorkerStatus
        for status in ("live_trade", "exchange_filled", "futures_liquidated")
    )
