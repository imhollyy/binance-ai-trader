# ruff: noqa: E501
from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from binance_ai_trader.ai_research_store import (
    ResearchStoreRejected,
    append_prediction,
    pending_predictions,
)
from binance_ai_trader.ai_trainable_model import ModelAction, ResearchPrediction

URL = os.environ.get("DATABASE_URL", "")
ISOLATED = (
    os.environ.get("TRADEPILOT_BT023_TEST_DB") == "1"
    and bool(URL)
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).port == 55444
    and make_url(URL).database == "trader_ai_bt023_test"
)
pytestmark = pytest.mark.skipif(
    not ISOLATED, reason="requires isolated PostgreSQL on localhost:55444"
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    db = create_engine(URL, pool_pre_ping=True)
    yield db
    db.dispose()


def test_prediction_restart_duplicate_clock_and_pending(engine: Engine) -> None:
    with engine.connect() as conn:
        now: int = conn.execute(
            text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint")
        ).scalar_one()
    close = now // 60_000 * 60_000 - 1
    row = ResearchPrediction(
        ModelAction.NO_TRADE,
        "bt023g_signal_0001",
        "model-v1",
        "1.0",
        "BTCUSDT",
        "1m",
        close,
        close + 3_600_000,
        None,
        False,
        "RESEARCH_ONLY",
        "a" * 64,
        "b" * 64,
    )
    receipt = append_prediction(engine, row)
    assert close < receipt.received_at_ms < receipt.outcome_due_at_ms
    assert (
        row.signal_id,
        receipt.received_at_ms,
        receipt.outcome_due_at_ms,
    ) in pending_predictions(engine)
    with pytest.raises(ResearchStoreRejected):
        append_prediction(engine, row)
    # A fresh engine represents process restart; durable receipt remains visible.
    restarted = create_engine(URL, pool_pre_ping=True)
    try:
        assert any(x[0] == row.signal_id for x in pending_predictions(restarted))
    finally:
        restarted.dispose()


def test_concurrent_duplicate_prediction_is_recorded_once(engine: Engine) -> None:
    with engine.connect() as conn:
        now: int = conn.execute(
            text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint")
        ).scalar_one()
    close = now // 60_000 * 60_000 - 1
    row = ResearchPrediction(
        ModelAction.HOLD,
        "bt023g_race_signal_0001",
        "model-v1",
        "1.0",
        "ETHUSDT",
        "1m",
        close,
        close + 3_600_000,
        None,
        False,
        "UNCALIBRATED",
        "d" * 64,
        "e" * 64,
    )

    def attempt(_: int) -> bool:
        try:
            append_prediction(engine, row)
            return True
        except ResearchStoreRejected:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(attempt, (1, 2)))
    assert sum(accepted) == 1
    with engine.connect() as conn:
        count: int = conn.execute(
            text("SELECT count(*) FROM ai_research_prediction_log WHERE signal_id=:id"),
            {"id": row.signal_id},
        ).scalar_one()
    assert count == 1


def test_append_only_research_tables_reject_update_delete_and_truncate(engine: Engine) -> None:
    with engine.connect() as conn:
        now: int = conn.execute(
            text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint")
        ).scalar_one()
    close = now // 60_000 * 60_000 - 1
    row = ResearchPrediction(
        ModelAction.HOLD,
        "append_only_signal_0001",
        "model-v1",
        "1.0",
        "BTCUSDT",
        "1m",
        close,
        close + 60_000,
        None,
        False,
        "RESEARCH_ONLY",
        "c" * 64,
        "d" * 64,
    )
    append_prediction(engine, row)
    # Outcome insertion is gated by the database clock and cannot be backdated.
    with engine.begin() as conn:
        conn.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, (:due - FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000))/1000.0))"
            ),
            {"due": close + 60_000},
        )
        conn.execute(
            text(
                "INSERT INTO ai_research_prediction_outcomes(signal_id,observed_outcome,source_sha256) VALUES(:signal,'HOLD',:hash)"
            ),
            {"signal": row.signal_id, "hash": "e" * 64},
        )
    for table in ("ai_research_prediction_log", "ai_research_prediction_outcomes"):
        truncate = (
            "TRUNCATE ai_research_prediction_log, ai_research_prediction_outcomes"
            if table == "ai_research_prediction_log"
            else "TRUNCATE ai_research_prediction_outcomes"
        )
        for statement in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}", truncate):
            with pytest.raises(DBAPIError, match="append only"), engine.begin() as conn:
                conn.execute(text(statement))
