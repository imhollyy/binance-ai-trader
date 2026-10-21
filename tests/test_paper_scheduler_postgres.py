"""Isolated PostgreSQL/MockTransport two-cycle scheduler, with no Binance orders."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from decimal import Decimal

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
from binance_ai_trader.paper_scheduler import SchedulerStatus, run_paper_scheduler
from binance_ai_trader.paper_worker import PaperWorkerReport, PaperWorkerStatus
from binance_ai_trader.risk_schemas import RiskSettings

DATABASE_URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
SAFE = bool(
    DATABASE_URL
    and make_url(DATABASE_URL).get_backend_name() == "postgresql"
    and make_url(DATABASE_URL).host in ("localhost", "127.0.0.1")
    and make_url(DATABASE_URL).database == "trader_scheduler_test"
)
pytestmark = pytest.mark.skipif(
    not SAFE or not ISOLATED,
    reason="Only an independently migrated disposable trader_scheduler_test database",
)


@pytest.fixture(scope="module")
def db() -> Iterator[Engine]:
    assert DATABASE_URL is not None and SAFE and ISOLATED
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with Session(engine) as session:
        for model in (
            SpotCandleRow,
            PaperSpotAccountRow,
            PaperSpotPositionRow,
            PaperSpotJournalRow,
            PaperSpotObservedQuoteRow,
            PaperWorkerGuardRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield engine
    engine.dispose()


def _counts(engine: Engine) -> tuple[int, int, int, int, int]:
    with Session(engine) as session:
        return (
            session.scalar(select(func.count()).select_from(SpotCandleRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperWorkerGuardRow)) or 0,
        )


def test_explicit_scheduler_two_ticks_429_restart_and_permission_revoke(db: Engine) -> None:
    base = (time.time_ns() // 60_000_000_000) * 60_000
    clock = [base + 100]
    status = [200]
    paths: list[str] = []
    logs: list[PaperWorkerReport] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data-api.binance.vision"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "X-MBX-APIKEY" not in request.headers
        paths.append(request.url.path)
        if status[0] != 200:
            return httpx.Response(status[0], headers={"Retry-After": "180"})
        if request.url.path == "/api/v3/klines":
            current = clock[0] // 60_000 * 60_000
            data: list[list[object]] = []
            for i in range(60):
                bar = current - (60 - i) * 60_000
                close = "102" if current > base and i == 59 else "100"
                data.append(
                    [
                        bar,
                        "100",
                        "110",
                        "90",
                        close,
                        "2",
                        bar + 59_999,
                        "0",
                        1,
                        "0",
                        "0",
                        "0",
                    ]
                )
            return httpx.Response(200, json=data)
        assert request.url.path == "/api/v3/ticker/price"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "102.2"})

    transport = httpx.MockTransport(handler)

    def wait(seconds: float) -> bool:
        assert seconds >= 60
        clock[0] += round(seconds * 1000)
        return False

    def scheduler(*, enabled: bool, ticks: int) -> SchedulerStatus:
        return run_paper_scheduler(
            db,
            enabled=enabled,
            symbol="BTCUSDT",
            interval="1m",
            max_ticks=ticks,
            transport=transport,
            clock_ms=lambda: clock[0],
            wait=wait,
            report=logs.append,
        ).status

    assert scheduler(enabled=False, ticks=2) is SchedulerStatus.DISABLED
    assert scheduler(enabled=True, ticks=2) is SchedulerStatus.DISABLED
    assert not paths
    assert _counts(db) == (0, 0, 0, 0, 0)

    with Session(db) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(
                    bot_enabled=False, manual_approval=False, risk_per_trade_pct=2
                ).model_dump(mode="json"),
            )
        )
    with Session(db) as session:
        ensure_paper_spot_account(session)
    assert scheduler(enabled=True, ticks=2) is SchedulerStatus.DISABLED
    assert not paths
    assert _counts(db) == (0, 0, 1, 0, 0)

    with Session(db) as session, session.begin():
        setting = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert setting is not None and account is not None
        setting.version = 2
        account.settings_version = 2
        setting.data = RiskSettings(
            bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
        ).model_dump(mode="json")

    logs.clear()
    assert scheduler(enabled=True, ticks=2) is SchedulerStatus.LIMIT_REACHED
    assert [x.status for x in logs] == [
        PaperWorkerStatus.NO_SIGNAL,
        PaperWorkerStatus.PAPER_RECORDED,
    ]
    assert _counts(db) == (61, 1, 2, 1, 1)
    assert paths.count("/api/v3/ticker/price") == 2
    with Session(db) as session:
        position = session.scalar(select(PaperSpotPositionRow))
        assert position is not None and position.status == "open"
        assert position.entry_fill_price == Decimal("102.2") * Decimal("1.0005")
        guard = session.get(PaperWorkerGuardRow, 1)
        assert guard is not None and guard.last_status == "paper_recorded"

    # Running at the same clock after a successful tick cannot duplicate.
    prev_http = len(paths)
    logs.clear()
    assert scheduler(enabled=True, ticks=1) is SchedulerStatus.LIMIT_REACHED
    assert [result.status for result in logs] == [PaperWorkerStatus.READY]
    assert len(paths) == prev_http
    assert _counts(db) == (61, 1, 2, 1, 1)

    # Next candle source throttled, even across Engine/process restart.
    clock[0] += 60_000
    status[0] = 429
    logs.clear()
    assert scheduler(enabled=True, ticks=1) is SchedulerStatus.LIMIT_REACHED
    assert [result.status for result in logs] == [PaperWorkerStatus.RATE_LIMITED]
    before = len(paths)
    with Session(db) as session:
        guard = session.get(PaperWorkerGuardRow, 1)
        assert guard is not None and guard.next_allowed_at_ms >= clock[0] + 180_000
    assert DATABASE_URL is not None
    separate_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        restarted = run_paper_scheduler(
            separate_engine,
            enabled=True,
            max_ticks=1,
            transport=transport,
            clock_ms=lambda: clock[0],
            wait=wait,
        )
        assert restarted.last_worker_status is PaperWorkerStatus.READY
        assert len(paths) == before
    finally:
        separate_engine.dispose()

    # Permission revoked while durable cooldown remains: must stop immediately.
    with Session(db) as session, session.begin():
        setting = session.get(TradingSettingsRow, 1)
        assert setting is not None
        setting.data = RiskSettings(
            bot_enabled=True, manual_approval=True, risk_per_trade_pct=2
        ).model_dump(mode="json")
    assert scheduler(enabled=True, ticks=3) is SchedulerStatus.DISABLED
    assert len(paths) == before
    assert _counts(db) == (61, 1, 2, 1, 1)
