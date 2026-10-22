"""BT-009B disposable PostgreSQL migration / physical dump / restore proof.

NEVER run against personal or shared data. Both databases must already exist,
have exact CI-only names, be on loopback and belong to the disposable test role.
No DROP/TRUNCATE is used by the test's setup or restore operation.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    StagingLoginGateRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.risk_schemas import RiskSettings

SOURCE_DB = "trader_stage_restore_source_test"
TARGET_DB = "trader_stage_restore_target_test"
ISOLATED = os.environ.get("STAGING_RESTORE_TEST_ISOLATED") == "1"
SRC_URL = os.environ.get("TEST_STAGING_RESTORE_SOURCE_URL")
DST_URL = os.environ.get("TEST_STAGING_RESTORE_TARGET_URL")

pytestmark = pytest.mark.skipif(
    not ISOLATED or not SRC_URL or not DST_URL,
    reason="Explicit isolated two-database PostgreSQL restore fixture required",
)


def assert_disposable_url(raw: str, expected_db: str) -> None:
    """Refuse anything other than an exact loopback CI-owned test database."""
    try:
        url = make_url(raw)
    except Exception as exc:
        raise ValueError("Not an isolated disposable PostgreSQL URL") from exc
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 5432
        or url.username != "trader_test"
        or not url.password
        or url.database != expected_db
        or bool(url.query)
    ):
        raise ValueError("Not an isolated disposable PostgreSQL URL")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _read_snapshot(engine: Engine) -> tuple[object, ...]:
    with Session(engine) as session:
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        gate = session.get(StagingLoginGateRow, 1)
        journal = session.scalars(
            select(PaperSpotJournalRow).order_by(PaperSpotJournalRow.id)
        ).all()
        sessions = session.scalars(
            select(StagingSessionRow).order_by(StagingSessionRow.token_hash)
        ).all()
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0008_ai_research_forecast_receipts"
        assert settings is not None and account is not None and gate is not None
        return (
            revision,
            settings.version,
            settings.data,
            (account.mode, account.market_type, account.initial_capital_usdt, account.cash_usdt),
            tuple(
                (
                    row.id,
                    row.event_id,
                    row.idempotency_key,
                    row.event_type,
                    row.cash_delta_usdt,
                    row.balance_after_usdt,
                    row.fee_usdt,
                    row.realized_pnl_delta_usdt,
                )
                for row in journal
            ),
            tuple(
                (
                    row.token_hash,
                    row.csrf_hash,
                    row.role,
                    row.issued_at_ms,
                    row.expires_at_ms,
                    row.revoked,
                )
                for row in sessions
            ),
            (gate.failures, gate.locked_until_ms),
        )


def _run_pg(command: list[str]) -> None:
    """Never dump shell commands, DB passwords, cookies or tool stderr into CI."""
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Disposable PostgreSQL utility failed ({command[0]})")


@pytest.fixture(scope="module")
def restored_database() -> Iterator[tuple[Engine, Engine]]:
    if not ISOLATED or not SRC_URL or not DST_URL:
        pytest.skip("Explicit isolated restore configuration is required")
    assert_disposable_url(SRC_URL, SOURCE_DB)
    assert_disposable_url(DST_URL, TARGET_DB)
    if not os.environ.get("PGPASSWORD"):
        raise ValueError("Isolated test role password must be set")
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.fail("pg_dump and pg_restore must be installed on the PostgreSQL CI runner")

    source = create_engine(SRC_URL, pool_pre_ping=True)
    target = create_engine(DST_URL, pool_pre_ping=True)
    try:
        assert inspect(target).get_table_names() == [], "Restore target must start empty"
        with Session(source) as session:
            assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 0
            assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
            assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0
            gate = session.get(StagingLoginGateRow, 1)
            assert gate is not None and gate.failures == 0
        with Session(source) as session, session.begin():
            session.add(
                TradingSettingsRow(id=1, version=1, data=RiskSettings().model_dump(mode="json"))
            )
        with Session(source) as session:
            snapshot = ensure_paper_spot_account(session)
            assert snapshot.initial_capital_usdt == Decimal("1000")
        now = time.time_ns() // 1_000_000
        with Session(source) as session, session.begin():
            gate = session.get(StagingLoginGateRow, 1)
            assert gate is not None
            gate.failures = 3
            gate.locked_until_ms = now + 120_000
            session.add_all(
                [
                    StagingSessionRow(
                        token_hash=_digest("synthetic-viewer-token-only"),
                        csrf_hash=_digest("synthetic-viewer-csrf-only"),
                        role="viewer",
                        issued_at_ms=now,
                        expires_at_ms=now + 900_000,
                        revoked=False,
                    ),
                    StagingSessionRow(
                        token_hash=_digest("synthetic-operator-token-only"),
                        csrf_hash=_digest("synthetic-operator-csrf-only"),
                        role="operator",
                        issued_at_ms=now,
                        expires_at_ms=now + 900_000,
                        revoked=True,
                    ),
                ]
            )
        expected = _read_snapshot(source)
        with tempfile.TemporaryDirectory(prefix="stage-restore-ci-") as tempdir:
            dump_path = Path(tempdir) / "synthetic.pgcustom"
            dump_path.touch(mode=0o600, exist_ok=False)
            _run_pg(
                [
                    "pg_dump",
                    "--host=127.0.0.1",
                    "--port=5432",
                    "--username=trader_test",
                    f"--dbname={SOURCE_DB}",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={dump_path}",
                ]
            )
            assert dump_path.stat().st_size > 0
            _run_pg(
                [
                    "pg_restore",
                    "--host=127.0.0.1",
                    "--port=5432",
                    "--username=trader_test",
                    f"--dbname={TARGET_DB}",
                    "--no-owner",
                    "--no-privileges",
                    "--single-transaction",
                    "--exit-on-error",
                    str(dump_path),
                ]
            )
        assert _read_snapshot(target) == expected
        yield source, target
    finally:
        source.dispose()
        target.dispose()


def test_migration_and_synthetic_journal_session_restore(
    restored_database: tuple[Engine, Engine],
) -> None:
    _, target = restored_database
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 1
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 2
        gate = session.get(StagingLoginGateRow, 1)
        assert gate is not None and gate.failures == 3
    with Session(target) as session:
        # The restore must not reset the balance or duplicate the opening event.
        snapshot = ensure_paper_spot_account(session)
        assert snapshot.cash_usdt == Decimal("1000")
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 1


def test_restored_journal_is_still_append_only(
    restored_database: tuple[Engine, Engine],
) -> None:
    _, target = restored_database
    with Session(target) as session:
        triggers = session.scalars(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'paper_spot_journal'::regclass AND NOT tgisinternal"
            )
        ).all()
        assert set(triggers) == {
            "paper_spot_journal_no_update_delete",
            "paper_spot_journal_no_truncate",
        }
        for statement in (
            "UPDATE paper_spot_journal SET fee_usdt = 0 WHERE event_id = 'initial-capital'",
            "DELETE FROM paper_spot_journal WHERE event_id = 'initial-capital'",
            "TRUNCATE TABLE paper_spot_journal",
        ):
            with pytest.raises(DBAPIError), session.begin_nested():
                session.execute(text(statement))
        session.rollback()
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 1


@pytest.mark.parametrize(
    ("bad_url", "expected"),
    (
        (
            "postgresql+psycopg://trader_test:fake@localhost:5432/" + SOURCE_DB,
            SOURCE_DB,
        ),
        ("postgresql+psycopg://trader_test:fake@127.0.0.1:5432/trader_dev", SOURCE_DB),
        (
            "postgresql+psycopg://trader_test:fake@127.0.0.1:5432/" + SOURCE_DB,
            TARGET_DB,
        ),
        (
            "postgresql+psycopg://admin:fake@127.0.0.1:5432/" + SOURCE_DB,
            SOURCE_DB,
        ),
    ),
)
def test_refuse_non_disposable_database_targets(bad_url: str, expected: str) -> None:
    with pytest.raises(ValueError, match="Not an isolated disposable"):
        assert_disposable_url(bad_url, expected)
