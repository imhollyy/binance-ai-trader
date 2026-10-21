"""Disposable PostgreSQL-only BT-006B signal settlement and idempotency tests."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import PaperExecutionRejected, PaperTradeReceipt
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_signal_execution import execute_paper_signal_once
from binance_ai_trader.paper_signal_plan import (
    PaperSignalPlan,
    SignalPlanStatus,
    SourceState,
    plan_paper_spot_signal_once,
)
from binance_ai_trader.paper_strategy import SmaCrossParameters
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
PARAMS = SmaCrossParameters(fast_window=3, slow_window=6)
DB_URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
VALID_DB = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("127.0.0.1", "localhost")
    and make_url(DB_URL).database == "trader_signal_exec_test"
)
pytestmark = pytest.mark.skipif(
    not (ISOLATED and VALID_DB),
    reason="Requires a disposable localhost trader_signal_exec_test DB",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DB_URL is not None and ISOLATED and VALID_DB
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert session.scalar(select(func.count()).select_from(SpotCandleRow)) == 0
    yield db
    db.dispose()


def _candle(open_ms: int, close_price: str = "100") -> SpotCandleRow:
    price = D(close_price)
    return SpotCandleRow(
        symbol="BTCUSDT", interval="1m",
        open_time_ms=open_ms, close_time_ms=open_ms + 59_999,
        open=price, high=price + D("1"), low=price - D("1"),
        close=price, volume=D("1"),
    )


def _plan(db: Engine, at_ms: int) -> PaperSignalPlan:
    return plan_paper_spot_signal_once(
        db,
        symbol="BTCUSDT", interval="1m",
        source_state=SourceState.HEALTHY, source_checked_at_ms=at_ms,
        params=PARAMS, now_ms=at_ms,
    )


def _execute(db: Engine, plan: PaperSignalPlan, at_ms: int) -> PaperTradeReceipt:
    return execute_paper_signal_once(
        db, plan, source_state=SourceState.HEALTHY,
        source_checked_at_ms=at_ms, params=PARAMS, now_ms=at_ms,
    )


def _counts(db: Engine) -> tuple[int, int, int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(SpotCandleRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
        )


def test_next_closed_candle_atomic_buy_sell_replay_and_fail_closed(engine: Engine) -> None:
    start = (time.time_ns() // 60_000_000_000) * 60_000
    now_first = start + 1

    # Fixture setup only, before the read-only planner / atomic settlement.
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1, version=1,
                data=RiskSettings(
                    bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
                ).model_dump(mode="json"),
            )
        )
        session.add_all([
            _candle(start - (60 - i) * 60_000, "102" if i == 59 else "100")
            for i in range(60)
        ])
    with Session(engine) as session:
        ensure_paper_spot_account(session)

    buy = _plan(engine, now_first)
    assert buy.status is SignalPlanStatus.CANDIDATE
    assert buy.decision is not None and buy.decision.action == "buy"
    assert _counts(engine) == (60, 1, 0, 1)

    # A signal at candle N close CANNOT fill at that same close,
    # and it cannot fill before a valid N+1 closed-candle quality check.
    with pytest.raises(PaperExecutionRejected, match="next_closed_candle_required"):
        _execute(engine, buy, start + 1)
    assert _counts(engine) == (60, 1, 0, 1)

    with Session(engine) as session, session.begin():
        session.add(_candle(start, "102"))
    with pytest.raises(PaperExecutionRejected, match="feed_not_ready"):
        _execute(engine, buy, start + 30_000)
    assert _counts(engine) == (61, 1, 0, 1)

    # Source restricted, or manual approval suddenly required => zero writes.
    end = start + 60_001
    with pytest.raises(PaperExecutionRejected, match="source_unverified_or_restricted"):
        execute_paper_signal_once(
            engine, buy, source_state=SourceState.RATE_LIMITED,
            source_checked_at_ms=end, params=PARAMS, now_ms=end,
        )
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        settings.version = 2
        account.settings_version = 2
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=True, risk_per_trade_pct=2
        ).model_dump(mode="json")
    with pytest.raises(PaperExecutionRejected, match="signal_settings_or_approval_changed"):
        _execute(engine, buy, end)
    assert _counts(engine) == (61, 1, 0, 1)
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        settings.version = 1
        account.settings_version = 1
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
        ).model_dump(mode="json")

    # Real PostgreSQL transaction lock + UNIQUE journal/position keys protect
    # one simulated buy against concurrent and repeated identical calls.
    sync = Barrier(4)

    def concurrent(_: int) -> PaperTradeReceipt:
        sync.wait(timeout=10)
        return _execute(engine, buy, end)

    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(concurrent, range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert receipts[0].event_type == "spot_buy"
    assert receipts[0].cash_after_usdt < D("1000")
    assert _counts(engine) == (61, 1, 1, 2)
    assert _execute(engine, buy, end) == receipts[0]

    assert DB_URL is not None
    restarted = create_engine(DB_URL, pool_pre_ping=True)
    try:
        assert _execute(restarted, buy, end) == receipts[0]
    finally:
        restarted.dispose()

    # The same historical signal cannot open again, even after settings
    # changes: its trade_id remains stable and UNIQUE in paper_spot_positions.
    new_version = _plan(engine, end)
    assert new_version.status is SignalPlanStatus.NO_SIGNAL

    # Next later genuine downward SMA cross while a position exists.
    with Session(engine) as session, session.begin():
        session.add_all(
            [_candle(start + i * 60_000, "102") for i in range(1, 5)]
        )
        session.add(_candle(start + 5 * 60_000, "98"))
    sell_signal_clock = start + 360_001
    sell = _plan(engine, sell_signal_clock)
    assert sell.status is SignalPlanStatus.CANDIDATE
    assert sell.decision is not None and sell.decision.action == "sell"
    assert _counts(engine) == (66, 1, 1, 2)

    with pytest.raises(PaperExecutionRejected, match="next_closed_candle_required"):
        _execute(engine, sell, sell_signal_clock)
    with Session(engine) as session, session.begin():
        session.add(_candle(start + 6 * 60_000, "98"))
    sell_end = start + 420_001
    before = _counts(engine)
    exit_receipt = _execute(engine, sell, sell_end)
    assert exit_receipt.event_type == "spot_sell"
    assert exit_receipt.realized_pnl_delta_usdt < D("0")
    assert _execute(engine, sell, sell_end) == exit_receipt
    assert _counts(engine) == (before[0], 1, 1, 3)
    with Session(engine) as session:
        positions = session.scalars(select(PaperSpotPositionRow)).all()
        assert len(positions) == 1 and positions[0].status == "closed"
        events = session.scalars(select(PaperSpotJournalRow)).all()
        assert [event.event_type for event in events] == [
            "account_opened", "spot_buy", "spot_sell"
        ]
    # A stale identical replay is read-only: no second debit or journal row.
    assert _execute(engine, sell, sell_end + 300_000) == exit_receipt
    assert _counts(engine) == (67, 1, 1, 3)
