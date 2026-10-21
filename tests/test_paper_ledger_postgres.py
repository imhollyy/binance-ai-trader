"""Ledger contract tests. Run only against an explicitly disposable PostgreSQL DB."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_ledger import (
    PaperAccountRejected,
    PaperAccountSnapshot,
    ensure_paper_spot_account,
)
from binance_ai_trader.risk_schemas import RiskSettings

DB_URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
pytestmark = pytest.mark.skipif(
    not DB_URL or not ISOLATED, reason="Requires explicitly isolated PostgreSQL test database"
)


@pytest.fixture(scope="module")
def ledger_engine() -> Iterator[Engine]:
    assert DB_URL is not None
    engine = create_engine(DB_URL, pool_pre_ping=True)
    with engine.begin() as connection:
        if connection.scalar(select(func.count()).select_from(PaperSpotAccountRow)) != 0:
            raise RuntimeError("Ledger tests require an empty, disposable paper account table")
        connection.execute(text("TRUNCATE TABLE settings_audit, trading_settings RESTART IDENTITY"))
    yield engine
    engine.dispose()


def _position(**overrides: object) -> PaperSpotPositionRow:
    values: dict[str, object] = {
        "account_id": 1,
        "trade_id": "trade-1",
        "idempotency_key": "position-key-1",
        "symbol": "BTCUSDT",
        "quantity": Decimal("0.1"),
        "entry_fill_price": Decimal("100"),
        "entry_notional_usdt": Decimal("10"),
        "entry_fee_usdt": Decimal("0.01"),
        "stop_price": Decimal("90"),
        "estimated_stop_loss_usdt": Decimal("1.02"),
    }
    values.update(overrides)
    return PaperSpotPositionRow(**values)


def _journal(**overrides: object) -> PaperSpotJournalRow:
    values: dict[str, object] = {
        "account_id": 1,
        "position_id": 1,
        "event_id": "buy-1",
        "idempotency_key": "journal-key-1",
        "event_type": "spot_buy",
        "cash_delta_usdt": Decimal("-10"),
        "balance_after_usdt": Decimal("990"),
        "fee_usdt": Decimal("0.01"),
    }
    values.update(overrides)
    return PaperSpotJournalRow(**values)


def test_schema_has_required_keys_indexes_and_immutable_triggers(ledger_engine: Engine) -> None:
    with ledger_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_paper_spot_observed_quotes"
        )
        indexes = set(
            connection.scalars(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND tablename IN ('paper_spot_positions', 'paper_spot_journal')"
                )
            )
        )
        assert {
            "ix_paper_spot_positions_account_status",
            "ix_paper_spot_journal_account_order",
            "ix_paper_spot_journal_position",
        } <= indexes
        triggers = set(
            connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid = 'paper_spot_journal'::regclass "
                    "AND NOT tgisinternal"
                )
            )
        )
        assert triggers == {
            "paper_spot_journal_no_update_delete",
            "paper_spot_journal_no_truncate",
        }


def test_account_bootstrap_constraints_rollback_and_restart(ledger_engine: Engine) -> None:
    with (
        Session(ledger_engine) as session,
        pytest.raises(PaperAccountRejected, match="persisted_paper_settings_required"),
    ):
        ensure_paper_spot_account(session)
    with Session(ledger_engine) as session, session.begin():
        session.add(
            TradingSettingsRow(id=1, version=1, data=RiskSettings().model_dump(mode="json"))
        )

    def fail_journal_insert(_session: Session, _flush_context: object, _instances: object) -> None:
        raise RuntimeError("simulated journal write failure")

    with Session(ledger_engine) as session:
        event.listen(session, "before_flush", fail_journal_insert)
        with pytest.raises(RuntimeError, match="simulated journal write failure"):
            ensure_paper_spot_account(session)
        event.remove(session, "before_flush", fail_journal_insert)
    with Session(ledger_engine) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0

    barrier = Barrier(2)

    def bootstrap(_: int) -> PaperAccountSnapshot:
        barrier.wait(timeout=5)
        with Session(ledger_engine) as session:
            return ensure_paper_spot_account(session)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(bootstrap, range(2)))
    assert first == second
    assert first.initial_capital_usdt == Decimal("1000")
    assert first.cash_usdt == Decimal("1000")
    with Session(ledger_engine) as session:
        assert ensure_paper_spot_account(session) == first
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 1
        session.rollback()

    # A later paper cash change must survive another bootstrap call.
    with Session(ledger_engine) as session, session.begin():
        account = session.get(PaperSpotAccountRow, 1)
        assert account is not None
        account.cash_usdt = Decimal("900.123456789123456789")
    with Session(ledger_engine) as session:
        again = ensure_paper_spot_account(session)
        assert again.cash_usdt == Decimal("900.123456789123456789")
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 1
        session.rollback()

    # A config budget change must fail closed and leave the original ledger intact.
    with Session(ledger_engine) as session, session.begin():
        row = session.get(TradingSettingsRow, 1)
        assert row is not None
        changed = RiskSettings(spot_capital_usdt=1100)
        row.data = changed.model_dump(mode="json")
        row.version = 2
    with (
        Session(ledger_engine) as session,
        pytest.raises(PaperAccountRejected, match="paper_spot_capital_changed"),
    ):
        ensure_paper_spot_account(session)
    with Session(ledger_engine) as session, session.begin():
        row = session.get(TradingSettingsRow, 1)
        assert row is not None
        row.data = RiskSettings().model_dump(mode="json")
        row.version = 3

    assert DB_URL is not None
    restarted = create_engine(DB_URL, pool_pre_ping=True)
    try:
        with Session(restarted) as session:
            persisted = ensure_paper_spot_account(session)
            assert persisted.initial_capital_usdt == Decimal("1000")
            assert persisted.cash_usdt == Decimal("900.123456789123456789")
            assert persisted.settings_version == 1
    finally:
        restarted.dispose()

    with Session(ledger_engine) as session, session.begin():
        session.add(_position())
    invalid_positions = [
        _position(trade_id="trade-1", idempotency_key="position-key-2"),
        _position(trade_id="trade-2", idempotency_key="position-key-1"),
        _position(trade_id="trade-3", idempotency_key="position-key-3", account_id=2),
        _position(trade_id="trade-4", idempotency_key="position-key-4", quantity=Decimal("0")),
        _position(
            trade_id="trade-5", idempotency_key="position-key-5", entry_fee_usdt=Decimal("NaN")
        ),
        _position(trade_id="trade-6", idempotency_key="position-key-6", status="closed"),
    ]
    with Session(ledger_engine) as session, session.begin():
        for position_candidate in invalid_positions:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(position_candidate)
                session.flush()
    with Session(ledger_engine) as session, session.begin():
        session.add(_journal())
    invalid_journal = [
        _journal(event_id="buy-1", idempotency_key="journal-key-2"),
        _journal(event_id="buy-2", idempotency_key="journal-key-1"),
        _journal(event_id="buy-3", idempotency_key="journal-key-3", position_id=99999),
        _journal(event_id="buy-4", idempotency_key="journal-key-4", fee_usdt=Decimal("-1")),
        _journal(event_id="buy-5", idempotency_key="journal-key-5", cash_delta_usdt=Decimal("NaN")),
    ]
    with Session(ledger_engine) as session, session.begin():
        for journal_candidate in invalid_journal:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(journal_candidate)
                session.flush()
        for statement in (
            "UPDATE paper_spot_journal SET fee_usdt = 0 WHERE event_id = 'buy-1'",
            "DELETE FROM paper_spot_journal WHERE event_id = 'buy-1'",
            "TRUNCATE TABLE paper_spot_journal",
        ):
            with (
                pytest.raises(DBAPIError, match="append only|cannot truncate"),
                session.begin_nested(),
            ):
                session.execute(text(statement))
    with Session(ledger_engine) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 2
