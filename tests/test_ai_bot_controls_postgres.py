"""BT-016 isolated PostgreSQL control persistence, audit and concurrency.

The tests create no PAPER account/position and connect only to an empty,
disposable localhost trader_bot_controls_test DB. No HTTP or Binance access.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_paper_bot_controls_test import (
    initialize_disposable_bot_control,
    read_disposable_bot_control,
    update_disposable_bot_control,
)
from binance_ai_trader.ai_position_protection import BotControlRejected, StopBehavior
from binance_ai_trader.models import (
    PaperBotControlEventRow,
    PaperBotControlRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.risk_schemas import RiskSettings

DB_URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("BOT_CONTROLS_TEST_ISOLATED") == "1"
VALID_TARGET = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("127.0.0.1", "localhost")
    and make_url(DB_URL).database == "trader_bot_controls_test"
)
pytestmark = pytest.mark.skipif(
    not (ISOLATED and VALID_TARGET),
    reason="Dedicated disposable localhost trader_bot_controls_test only",
)
CLOCK = 1_800_000_000_000
OPERATOR = "a" * 64
VIEWER = "b" * 64
REVOKED = "c" * 64
EXPIRED = "d" * 64


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DB_URL is not None and VALID_TARGET and ISOLATED
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        for table in (PaperBotControlRow, PaperBotControlEventRow,
                      PaperSpotAccountRow, PaperSpotJournalRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    yield db
    db.dispose()


def _write(
    db: Engine,
    *,
    actor: str = OPERATOR,
    version: int,
    state: BotState,
    behavior: StopBehavior = StopBehavior.MONITOR_ONLY,
    confirmed: bool = False,
    clock: int = CLOCK,
):
    return update_disposable_bot_control(
        db,
        actor_session_hash=actor,
        expected_revision=version,
        next_state=state,
        stop_behavior=behavior,
        confirm_reduce_only_review=confirmed,
        now_ms=clock,
    )


def test_disposable_controls_revisions_sessions_restarts_and_audit(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=4,
                data=RiskSettings(bot_enabled=True, manual_approval=False).model_dump(mode="json"),
            )
        )
        session.add_all(
            [
                StagingSessionRow(
                    token_hash=digest,
                    csrf_hash="0" * 64,
                    role=role,
                    issued_at_ms=CLOCK - 5000,
                    expires_at_ms=CLOCK + (5000 if digest != EXPIRED else -100),
                    revoked=digest == REVOKED,
                )
                for digest, role in (
                    (OPERATOR, "operator"), (VIEWER, "viewer"),
                    (REVOKED, "operator"), (EXPIRED, "operator"),
                )
            ]
        )
    assert read_disposable_bot_control(engine) is None
    initial = initialize_disposable_bot_control(engine, now_ms=CLOCK)
    assert initial.state is BotState.STOPPED
    assert initial.revision == 1 and initial.settings_version == 4
    assert initial.stop_behavior is StopBehavior.MONITOR_ONLY
    assert not initial.operator_confirmed_reduce_only
    assert initialize_disposable_bot_control(engine, now_ms=CLOCK + 1) == initial

    for actor in (VIEWER, REVOKED, EXPIRED, "e" * 64):
        with pytest.raises(BotControlRejected, match="authorized_operator_session_required"):
            _write(engine, actor=actor, version=1, state=BotState.PAUSED)
    with pytest.raises(BotControlRejected, match="explicit_paper_start_not_authorized"):
        _write(engine, version=1, state=BotState.RUNNING)
    assert read_disposable_bot_control(engine) == initial

    barrier = Barrier(3)

    def race(_: int) -> str:
        barrier.wait(timeout=10)
        try:
            return str(_write(engine, version=1, state=BotState.PAUSED).revision)
        except BotControlRejected as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(race, range(3)))
    assert sorted(outcomes) == [
        "2", "control_revision_or_settings_stale", "control_revision_or_settings_stale"
    ]
    paused = read_disposable_bot_control(engine)
    assert paused is not None and paused.state is BotState.PAUSED and paused.revision == 2

    with pytest.raises(BotControlRejected, match="no_op_transition_requires_new_revision"):
        _write(engine, version=2, state=BotState.PAUSED)
    configured = _write(
        engine,
        version=2,
        state=BotState.PAUSED,
        behavior=StopBehavior.REVIEW_REDUCE_ONLY,
        confirmed=True,
    )
    assert configured.revision == 3 and configured.operator_confirmed_reduce_only
    running = _write(
        engine,
        version=3,
        state=BotState.RUNNING,
        behavior=StopBehavior.REVIEW_REDUCE_ONLY,
        confirmed=True,
    )
    assert running.revision == 4 and running.state is BotState.RUNNING

    with pytest.raises(BotControlRejected, match="emergency_requires_operator_authorization"):
        # The isolated wrapper always attests operator authorization. Direct
        # pure state helper covers the explicitly unauthorized branch.
        from binance_ai_trader.ai_position_protection import change_paper_bot_state
        change_paper_bot_state(
            RiskSettings(bot_enabled=True, manual_approval=False), running,
            expected_revision=4,
            next_state=BotState.EMERGENCY_FLATTEN,
            operator_authorized=False,
        )
    _write(engine, version=4, state=BotState.EMERGENCY_FLATTEN)
    stopped = _write(engine, version=5, state=BotState.STOPPED)
    assert stopped.revision == 6 and stopped.state is BotState.STOPPED

    assert DB_URL is not None
    restarted = create_engine(DB_URL, pool_pre_ping=True)
    try:
        assert read_disposable_bot_control(restarted) == stopped
        assert initialize_disposable_bot_control(restarted, now_ms=CLOCK) == stopped
    finally:
        restarted.dispose()
    with Session(engine) as session:
        events = session.scalars(
            select(PaperBotControlEventRow).order_by(PaperBotControlEventRow.revision)
        ).all()
        assert [entry.revision for entry in events] == [2, 3, 4, 5, 6]
        assert all(entry.actor_session_hash != OPERATOR for entry in events)
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0

    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("UPDATE paper_bot_control_events SET revision = 9"))
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("DELETE FROM paper_bot_control_events"))
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("TRUNCATE paper_bot_control_events"))
        session.commit()

    with Session(engine) as session, session.begin():
        settings = session.get(TradingSettingsRow, 1)
        assert settings is not None
        settings.version = 5
    with pytest.raises(BotControlRejected, match="control_revision_or_settings_stale"):
        _write(engine, version=6, state=BotState.PAUSED)
    assert read_disposable_bot_control(engine) == stopped


def test_non_disposable_url_denied_without_connecting() -> None:
    fake = create_engine("postgresql+psycopg://localhost/trader_owner")
    try:
        with pytest.raises(BotControlRejected, match="isolated_bot_control_database_only"):
            read_disposable_bot_control(fake)
    finally:
        fake.dispose()
