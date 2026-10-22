"""BT-023D PostgreSQL-only receipt ingestion tests. No owner account or orders."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_research_forecast_receipts_test import (
    ForecastReceipt,
    ForecastReceiptInput,
    ForecastReceiptRejected,
    append_disposable_forecast,
    read_disposable_forecasts,
)
from binance_ai_trader.models import (
    AiResearchForecastReceiptRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
)

DB_URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("AI_FORECAST_TEST_ISOLATED") == "1"
VALID_TARGET = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("127.0.0.1", "localhost")
    and make_url(DB_URL).database == "trader_ai_forecast_test"
)
pytestmark = pytest.mark.skipif(
    not (ISOLATED and VALID_TARGET),
    reason="Dedicated disposable localhost trader_ai_forecast_test only",
)
D = Decimal
MINUTE = 60_000
HORIZON = 30 * MINUTE
RUN_PREFIX = "forecast_" + uuid.uuid4().hex[:8] + "_"


def _test_receipts(db: Engine) -> tuple[ForecastReceipt, ...]:
    return tuple(
        item for item in read_disposable_forecasts(db) if item.signal_id.startswith(RUN_PREFIX)
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DB_URL and ISOLATED and VALID_TARGET
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AiResearchForecastReceiptRow)
                .where(AiResearchForecastReceiptRow.signal_id.startswith(RUN_PREFIX))
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
    yield db
    db.dispose()


def _now(db: Engine) -> int:
    with Session(db) as session:
        return int(
            session.scalar(
                text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint")
            )
        )


def request(db: Engine, signal_id: str | None = None) -> ForecastReceiptInput:
    now = _now(db)
    closed = now // MINUTE * MINUTE - 1
    return ForecastReceiptInput(
        signal_id=signal_id or RUN_PREFIX + "initial_0001",
        model_version="research_model_v1",
        symbol="BTCUSDT",
        interval="1m",
        observed_close_at_ms=closed,
        outcome_due_at_ms=closed + HORIZON,
        forecast=ProbabilityEstimate(win=D(".50"), loss=D(".30"), flat=D(".20")),
    )


def test_server_clocked_immutable_receipts_and_restart(engine: Engine) -> None:
    before = _now(engine)
    first = append_disposable_forecast(engine, forecast=request(engine))
    after = _now(engine)
    assert before <= first.received_at_ms <= after
    assert first.received_at_ms < first.outcome_due_at_ms
    assert first.source == "disposable_postgres_offline_forecast"
    assert first.no_trade and not first.trade_authorized
    assert not first.qualifies_as_calibrated_model
    assert _test_receipts(engine) == (first,)
    assert DB_URL is not None
    reopened = create_engine(DB_URL, pool_pre_ping=True)
    try:
        assert _test_receipts(reopened) == (first,)
    finally:
        reopened.dispose()
    with Session(engine) as session:
        row = session.scalar(
            select(AiResearchForecastReceiptRow).where(
                AiResearchForecastReceiptRow.signal_id == first.signal_id
            )
        )
        assert row is not None
        assert (row.prob_win, row.prob_loss, row.prob_flat) == (D(".50"), D(".30"), D(".20"))
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("UPDATE ai_research_forecast_receipts SET prob_win = 0"))
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("DELETE FROM ai_research_forecast_receipts"))
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError, match="append only"):
        session.execute(text("TRUNCATE ai_research_forecast_receipts"))
        session.commit()


def test_sql_ingest_server_clock_cannot_be_backdated(engine: Engine) -> None:
    now = _now(engine)
    # Even a direct INSERT with forged received_at_ms is assigned PostgreSQL
    # server time and rejected if the outcome horizon is already past.
    stale_close = (now // MINUTE - 20) * MINUTE - 1
    with Session(engine) as session, pytest.raises(DBAPIError, match="before outcome"):
        session.execute(
            text(
                "INSERT INTO ai_research_forecast_receipts "
                "(signal_id,model_version,symbol,interval,observed_close_at_ms,"
                "outcome_due_at_ms,received_at_ms,prob_win,prob_loss,prob_flat) "
                "VALUES ('expired_forecast', 'model_v1', 'BTCUSDT', '1m', "
                ":close, :due, 1, 0.5, 0.3, 0.2)"
            ),
            {"close": stale_close, "due": stale_close + MINUTE},
        )
        session.commit()
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AiResearchForecastReceiptRow)
                .where(AiResearchForecastReceiptRow.signal_id.startswith(RUN_PREFIX))
            )
            == 1
        )


def test_duplicate_concurrent_writers_accept_exactly_one(engine: Engine) -> None:
    item = request(engine, RUN_PREFIX + "concurrent")

    def race(_: int) -> str:
        try:
            append_disposable_forecast(engine, forecast=item)
            return "accepted"
        except ForecastReceiptRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(race, range(3)))
    assert sorted(outcomes) == ["accepted", "rejected", "rejected"]
    assert len(_test_receipts(engine)) == 2


def test_invalid_inputs_fail_before_database_mutation(engine: Engine) -> None:
    item = request(engine, RUN_PREFIX + "invalid_0001")
    bad = (
        replace(item, symbol="XRPUSDT"),
        replace(item, signal_id="bad"),
        replace(item, interval="2m"),
        replace(item, outcome_due_at_ms=item.observed_close_at_ms + 1000),
        replace(item, observed_close_at_ms=item.observed_close_at_ms - 1),
        replace(
            item,
            forecast=ProbabilityEstimate.model_construct(win=D("1.2"), loss=D("-.2"), flat=D(0)),
        ),
    )
    for candidate in bad:
        with pytest.raises(ForecastReceiptRejected):
            append_disposable_forecast(engine, forecast=candidate)
    assert len(_test_receipts(engine)) == 2


def test_non_disposable_database_is_rejected_before_connection() -> None:
    fake = create_engine("postgresql+psycopg://localhost/trader_owner")
    try:
        with pytest.raises(ForecastReceiptRejected, match="isolated_forecast_database_only"):
            read_disposable_forecasts(fake)
        with pytest.raises(ForecastReceiptRejected, match="isolated_forecast_database_only"):
            append_disposable_forecast(
                fake,
                forecast=ForecastReceiptInput(
                    "x" * 9,
                    "v1",
                    "BTCUSDT",
                    "1m",
                    1,
                    60001,
                    ProbabilityEstimate(win=D(".5"), loss=D(".3"), flat=D(".2")),
                ),
            )
    finally:
        fake.dispose()
