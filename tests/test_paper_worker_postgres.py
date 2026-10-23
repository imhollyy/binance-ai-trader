"""Dedicated disposable PostgreSQL: one-shot PAPER worker, NEVER exchange orders."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    PaperWorkerGuardRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_strategy import SmaCrossParameters
from binance_ai_trader.paper_worker import PaperWorkerStatus, run_paper_worker_once
from binance_ai_trader.risk_schemas import RiskSettings

DB_URL = os.environ.get("TEST_POSTGRES_URL")
VALID = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("localhost", "127.0.0.1")
    and make_url(DB_URL).database == "trader_worker_test"
)
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
pytestmark = pytest.mark.skipif(
    not VALID or not ISOLATED,
    reason="Requires a migrated, empty, disposable localhost trader_worker_test",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DB_URL is not None and ISOLATED and VALID
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        for model in (
            PaperSpotAccountRow,
            PaperSpotPositionRow,
            PaperSpotJournalRow,
            PaperSpotObservedQuoteRow,
            PaperWorkerGuardRow,
            SpotCandleRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield db
    db.dispose()


def _counts(db: Engine) -> tuple[int, int, int, int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(SpotCandleRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperWorkerGuardRow)) or 0,
        )


def _fixture_transport(
    clock: list[int],
    calls: list[str],
    *,
    status: list[int],
    upward: list[bool],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "signature" not in request.url.params
        assert "X-MBX-APIKEY" not in request.headers
        calls.append(request.url.path)
        if status[0] != 200:
            return httpx.Response(status[0], headers={"Retry-After": "120"})
        if request.url.path == "/api/v3/klines":
            base = clock[0] // 60_000 * 60_000
            closes = ["100"] * 59 + (["102"] if upward[0] else ["100"])
            candles = []
            for i in range(60):
                ms = base - (60 - i) * 60_000
                candles.append(
                    [
                        ms,
                        "100",
                        "110",
                        "90",
                        closes[i],
                        "2",
                        ms + 59_999,
                        "0",
                        1,
                        "0",
                        "0",
                        "0",
                    ]
                )
            return httpx.Response(200, json=candles)
        assert request.url.path == "/api/v3/ticker/price"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "102.2"})

    return httpx.MockTransport(handler)


def test_disabled_optin_no_http_readonly_cooldown_restart_and_concurrent(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicitly test the existing worker and ledger behavior with a test-only
    # RiskGuard fixture. Production execution adapters do not install this.
    import binance_ai_trader.paper_observed_execution as observed_execution

    monkeypatch.setattr(observed_execution, "require_atomic_riskguard_authorization", lambda: None)
    base = time.time_ns() // 60_000_000_000 * 60_000
    clock = [base + 100]
    calls: list[str] = []
    status = [200]
    upward = [False]
    transport = _fixture_transport(clock, calls, status=status, upward=upward)

    def tick(enabled: bool = True, db: Engine = engine) -> PaperWorkerStatus:
        return run_paper_worker_once(
            db,
            enabled=enabled,
            symbol="BTCUSDT",
            interval="1m",
            params=SmaCrossParameters(fast_window=3, slow_window=6),
            clock_ms=lambda: clock[0],
            transport=transport,
        ).status

    assert tick(enabled=False) is PaperWorkerStatus.DISABLED
    assert tick(enabled=True) is PaperWorkerStatus.DISABLED
    assert _counts(engine) == (0, 0, 0, 0, 0)
    assert calls == []

    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(
                    bot_enabled=False, manual_approval=False, risk_per_trade_pct=2
                ).model_dump(mode="json"),
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    assert tick() is PaperWorkerStatus.DISABLED
    assert _counts(engine) == (0, 0, 1, 0, 0)
    assert calls == []

    # Test fixture explicitly opts in to PAPER only. Real exchange orders
    # are impossible: the injected transport permits just two public paths.
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        settings.version = 2
        account.settings_version = 2
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
        ).model_dump(mode="json")
    assert tick() is PaperWorkerStatus.NO_SIGNAL
    assert _counts(engine) == (60, 0, 1, 0, 1)
    assert calls == ["/api/v3/klines", "/api/v3/klines", "/api/v3/ticker/price"]
    assert tick() is PaperWorkerStatus.READY
    assert len(calls) == 3
    with Session(engine) as session:
        guard = session.get(PaperWorkerGuardRow, 1)
        assert guard is not None and guard.last_status == "no_signal"
        assert guard.next_allowed_at_ms == base + 60_100

    # New fully CLOSED signal at the next interval: concurrent processes
    # compete for the same session-scoped advisory lease. Only one can debit.
    clock[0] += 60_000
    upward[0] = True
    gate = Barrier(4)

    def work(_: int) -> PaperWorkerStatus:
        gate.wait(timeout=10)
        return tick()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(work, range(4)))
    assert PaperWorkerStatus.PAPER_RECORDED in outcomes
    assert all(s in (PaperWorkerStatus.PAPER_RECORDED, PaperWorkerStatus.READY) for s in outcomes)
    assert _counts(engine) == (61, 1, 2, 1, 1)
    assert tick() is PaperWorkerStatus.READY
    assert _counts(engine) == (61, 1, 2, 1, 1)

    # Mismatching public HTTP 429 blocks the next attempt and persists its
    # Retry-After + exponential guard. No trade, no further HTTP on cooldown.
    clock[0] += 60_000
    status[0] = 429
    assert tick() is PaperWorkerStatus.RATE_LIMITED
    calls_before = len(calls)
    assert tick() is PaperWorkerStatus.READY
    assert len(calls) == calls_before
    with Session(engine) as session:
        guard = session.get(PaperWorkerGuardRow, 1)
        assert guard is not None and guard.last_status == "rate_limited"
        next_time = guard.next_allowed_at_ms
        assert next_time >= clock[0] + 120_000
    assert DB_URL is not None
    restarted = create_engine(DB_URL, pool_pre_ping=True)
    try:
        assert tick(db=restarted) is PaperWorkerStatus.READY
        assert len(calls) == calls_before
    finally:
        restarted.dispose()

    # A user revoking permission disables the service immediately, even
    # when a persisted retry-after guard exists.
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        assert settings is not None
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=True, risk_per_trade_pct=2
        ).model_dump(mode="json")
    assert tick() is PaperWorkerStatus.DISABLED
    assert len(calls) == calls_before
    assert _counts(engine) == (61, 1, 2, 1, 1)
