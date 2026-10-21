"""Disposable PostgreSQL tests for single-run PAPER signal planning.

CI must set up a dedicated EMPTY localhost trader_signal_test database.
No user database, no network, no authenticated trading, no persistent write
by the planner. Fixture-only inserts are explicitly marked as test data.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import open_paper_spot
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_signal_plan import (
    PaperSignalPlan,
    SignalPlanStatus,
    SourceState,
    plan_paper_spot_signal_once,
)
from binance_ai_trader.paper_spot import SpotBuyIntent
from binance_ai_trader.paper_strategy import SmaCrossParameters
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
DB_URL = os.environ.get("TEST_POSTGRES_URL")
VALID_TEST_DB = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("127.0.0.1", "localhost")
    and make_url(DB_URL).database == "trader_signal_test"
)
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
pytestmark = pytest.mark.skipif(
    not (VALID_TEST_DB and ISOLATED),
    reason="Only disposable localhost trader_signal_test PostgreSQL is allowed",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert VALID_TEST_DB and ISOLATED and DB_URL is not None
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(SpotCandleRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) == 0
    yield db
    db.dispose()


def _counts(db: Engine) -> tuple[int, int, int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(SpotCandleRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
        )


def _plan(db: Engine, clock: int, **options: object) -> PaperSignalPlan:
    params = options.get("params")
    state = options.get("state")
    checked_at = options.get("checked_at")
    return plan_paper_spot_signal_once(
        db,
        symbol="BTCUSDT",
        interval="1m",
        now_ms=clock,
        source_checked_at_ms=checked_at if isinstance(checked_at, int) else clock,
        source_state=state if isinstance(state, SourceState) else SourceState.HEALTHY,
        params=(
            params
            if isinstance(params, SmaCrossParameters)
            else SmaCrossParameters(fast_window=3, slow_window=6)
        ),
    )


def _row(open_ms: int, *, close: str = "100") -> SpotCandleRow:
    price = D(close)
    return SpotCandleRow(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=open_ms,
        close_time_ms=open_ms + 59_999,
        open=D("100"),
        high=max(D("105"), price),
        low=min(D("95"), price),
        close=price,
        volume=D("2.000000000000000001"),
    )


def test_readonly_snapshot_latest_only_replay_quality_and_sell(engine: Engine) -> None:
    base = (time.time_ns() // 1_000_000 // 60_000) * 60_000
    clock = base + 1
    params = SmaCrossParameters(fast_window=3, slow_window=6)
    absent = _plan(engine, clock)
    assert absent.status is SignalPlanStatus.BLOCKED
    assert absent.reason == "paper_account_or_settings_missing"

    # Fixture creation happens OUTSIDE the planner. The planner must only read.
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(
                    bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
                ).model_dump(mode="json"),
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)

    empty = _plan(engine, clock)
    assert empty.status is SignalPlanStatus.BLOCKED
    assert empty.reason == "feed_empty"

    with Session(engine) as session, session.begin():
        session.add_all(
            [_row(base - (60 - i) * 60_000, close="102" if i == 59 else "100") for i in range(60)]
        )
    assert _counts(engine) == (60, 1, 0, 1)
    before = _counts(engine)
    result = _plan(engine, clock)
    assert result.status is SignalPlanStatus.CANDIDATE
    assert result.decision is not None and result.decision.action == "buy"
    assert result.latest_close_time_ms == base - 1
    assert result.decision.observed_close_time_ms == base - 1
    assert result.idempotency_key is not None
    assert result.settings_version == 1
    assert result.checked_candles == 60
    assert _plan(engine, clock) == result

    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(pool.map(lambda _: _plan(engine, clock), range(12)))
    assert all(p == result for p in concurrent)
    assert _counts(engine) == before

    # Every source-state branch blocks without any persistent mutation.
    for state in (None, SourceState.RATE_LIMITED, SourceState.UNAVAILABLE):
        out = plan_paper_spot_signal_once(
            engine,
            symbol="BTCUSDT",
            interval="1m",
            now_ms=clock,
            source_state=state,
            source_checked_at_ms=clock,
            params=params,
        )
        assert out.status is SignalPlanStatus.BLOCKED
        assert out.decision is None and out.idempotency_key is None
    assert _counts(engine) == before

    # Unknown age/stale market data is denied even with caller-provided health.
    assert _plan(engine, clock + 240_000).status is SignalPlanStatus.BLOCKED
    assert _plan(engine, clock, checked_at=clock - 120_000).status is SignalPlanStatus.BLOCKED

    # Respect persisted human approval policy without auto-approving buys.
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        settings.version = 2
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=True, risk_per_trade_pct=2
        ).model_dump(mode="json")
        account.settings_version = 2
    denied = _plan(engine, clock)
    assert denied.status is SignalPlanStatus.BLOCKED
    assert denied.reason == "manual_approval_required"
    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        settings.version = 3
        settings.data = RiskSettings(
            bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
        ).model_dump(mode="json")
        account.settings_version = 3

    # Contradictory/still-open latest candle fails the feed gate.
    with Session(engine) as session, session.begin():
        last = session.scalar(
            select(SpotCandleRow).where(SpotCandleRow.open_time_ms == base - 60_000)
        )
        assert last is not None
        last.close_time_ms = base + 59_999
    assert _plan(engine, clock).reason == "feed_invalid"
    with Session(engine) as session, session.begin():
        last = session.scalar(
            select(SpotCandleRow).where(SpotCandleRow.open_time_ms == base - 60_000)
        )
        assert last is not None
        last.close_time_ms = base - 1

    # Missing stored interval: no older signal is replayed as the latest.
    with Session(engine) as session, session.begin():
        missing = session.scalar(
            select(SpotCandleRow).where(SpotCandleRow.open_time_ms == base - 30 * 60_000)
        )
        assert missing is not None
        session.delete(missing)
    assert _plan(engine, clock).reason == "feed_gaps"
    with Session(engine) as session, session.begin():
        session.add(_row(base - 30 * 60_000))

    # DB uniqueness denies duplicate candle fixtures as expected.
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(_row(base - 60_000))

    # No SMA cross at the latest close -> explicit NO_SIGNAL.
    with Session(engine) as session, session.begin():
        last = session.scalar(
            select(SpotCandleRow).where(SpotCandleRow.open_time_ms == base - 60_000)
        )
        assert last is not None
        last.close = D("100")
    no_signal = _plan(engine, clock)
    assert no_signal.status is SignalPlanStatus.NO_SIGNAL
    assert no_signal.decision is None and no_signal.idempotency_key is None

    # Open a hypothetical position ONLY as a fixture, via existing atomic service.
    with Session(engine) as session:
        open_paper_spot(
            session,
            SpotBuyIntent("signal-existing", "BTCUSDT", D("100"), D("1"), D("99")),
            "signal-existing-open",
        )
    with Session(engine) as session, session.begin():
        last = session.scalar(
            select(SpotCandleRow).where(SpotCandleRow.open_time_ms == base - 60_000)
        )
        assert last is not None
        last.close = D("98")
    journal_before = _counts(engine)
    selling = _plan(engine, clock)
    assert selling.status is SignalPlanStatus.CANDIDATE
    assert selling.decision is not None and selling.decision.action == "sell"
    assert selling.idempotency_key != result.idempotency_key
    assert _counts(engine) == journal_before

    # Hard fail-closed for corrupt account/position state.
    with Session(engine) as session, session.begin():
        account = session.get(PaperSpotAccountRow, 1)
        assert account is not None
        account.cash_usdt = D("7")
    assert _plan(engine, clock).reason == "paper_ledger_unreconciled"
