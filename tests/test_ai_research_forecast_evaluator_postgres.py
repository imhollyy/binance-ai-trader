"""PostgreSQL concurrency and immutability checks against disposable DB only."""

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

from binance_ai_trader.ai_research_forecast_evaluator_test import (
    evaluate_due_forecasts,
    read_evaluated_forecasts,
)
from binance_ai_trader.ai_research_forecast_receipts_test import ForecastReceiptRejected
from binance_ai_trader.market_data import Candle, MarketRateLimited, PublicSpotMarketClient
from binance_ai_trader.models import (
    AiResearchForecastOutcomeRow,
    AiResearchForecastReceiptRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    SpotCandleRow,
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

PUBLIC_FIXTURES: dict[int, tuple[Candle, ...]] = {}


class FixturePublicClient(PublicSpotMarketClient):
    """Offline mock of verified public API responses for PostgreSQL tests."""

    def fetch_closed_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time_ms: int | None = None,
    ) -> list[Candle]:
        if start_time_ms is None:
            return []
        return list(PUBLIC_FIXTURES.get(start_time_ms, ())[:limit])


class LimitedPublicClient(PublicSpotMarketClient):
    def fetch_closed_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time_ms: int | None = None,
    ) -> list[Candle]:
        raise MarketRateLimited(429, 60)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DB_URL and ISOLATED and VALID_TARGET
    db = create_engine(DB_URL, pool_pre_ping=True, pool_size=5)
    yield db
    db.dispose()


def seeded_pending(
    db: Engine,
    *,
    move_pct: Decimal = Decimal("0.01"),
    omit_target: bool = False,
    corrupt_target: bool = False,
    unclosed_target: bool = False,
) -> tuple[str, int]:
    """Insert explicitly synthetic, historic receipts/candles in this test DB."""
    suffix = uuid.uuid4().hex
    signal = "integration_" + suffix
    source_open = 1_600_000_000_000 + (int(suffix[:5], 16) * 60_000)
    source_open -= source_open % 60_000
    source_close = source_open + 59_999
    target_open = source_open + 60_000
    target_close = target_open + 59_999
    price = Decimal("100")
    moved = price * (Decimal(1) + move_pct)
    PUBLIC_FIXTURES[source_open] = (
        Candle(
            "BTCUSDT",
            "1m",
            source_open,
            source_close,
            price,
            price,
            price,
            price,
            Decimal("1"),
        ),
        Candle(
            "BTCUSDT",
            "1m",
            target_open,
            target_close,
            moved,
            moved,
            moved,
            moved,
            Decimal("1"),
        ),
    )
    with Session(db) as session, session.begin():
        # Deliberate historical timestamp insertion is limited to this fresh
        # disposable test DB; application code cannot provide a clock override.
        session.execute(
            text(
                "ALTER TABLE ai_research_forecast_receipts DISABLE TRIGGER ai_receipt_server_clock"
            )
        )
        session.execute(
            text(
                "INSERT INTO ai_research_forecast_receipts "
                "(signal_id,model_version,symbol,interval,observed_close_at_ms,"
                "outcome_due_at_ms,received_at_ms,prob_win,prob_loss,prob_flat) "
                "VALUES (:signal,'test_model_v1','BTCUSDT','1m',:observed,:due,:received,"
                "0.6,0.2,0.2)"
            ),
            {
                "signal": signal,
                "observed": source_close,
                "due": target_close,
                "received": source_close + 1,
            },
        )
        session.execute(
            text("ALTER TABLE ai_research_forecast_receipts ENABLE TRIGGER ai_receipt_server_clock")
        )
        candles = [
            SpotCandleRow(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=source_open,
                close_time_ms=source_close,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("1"),
            ),
            SpotCandleRow(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=target_open,
                close_time_ms=target_close,
                open=moved,
                high=moved,
                low=moved,
                close=moved,
                volume=Decimal("1"),
            ),
        ]
        if omit_target:
            candles.pop()
        if corrupt_target and not omit_target:
            candles[-1].close = Decimal("500")
        if unclosed_target and not omit_target:
            candles[-1].close_time_ms -= 1
        session.add_all(candles)
    with Session(db) as session:
        receipt_id = session.scalar(
            select(AiResearchForecastReceiptRow.id).where(
                AiResearchForecastReceiptRow.signal_id == signal
            )
        )
        assert receipt_id is not None
    return signal, receipt_id


def database_now_ms(db: Engine) -> int:
    with Session(db) as session:
        return int(
            session.scalar(
                text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint")
            )
        )


def test_concurrent_workers_are_idempotent_and_restart_preserves_outcome(engine: Engine) -> None:
    signal, receipt_id = seeded_pending(engine)
    before = database_now_ms(engine)

    def evaluate(_: int) -> tuple[str, ...]:
        return tuple(
            row.signal_id
            for row in evaluate_due_forecasts(engine, market_client=FixturePublicClient())
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, range(2)))
    after = database_now_ms(engine)
    assert sum(result.count(signal) for result in results) == 1
    assert evaluate_due_forecasts(engine, market_client=FixturePublicClient()) == ()
    with Session(engine) as session:
        outcomes = session.scalars(
            select(AiResearchForecastOutcomeRow).where(
                AiResearchForecastOutcomeRow.receipt_id == receipt_id
            )
        ).all()
        receipt = session.get(AiResearchForecastReceiptRow, receipt_id)
        assert len(outcomes) == 1
        assert receipt is not None
        assert outcomes[0].outcome == "WIN"
        assert before <= outcomes[0].evaluated_at_ms <= after
        assert outcomes[0].evaluated_at_ms >= receipt.outcome_due_at_ms
        assert outcomes[0].source_url == "https://data-api.binance.vision/api/v3/klines"
        assert len(outcomes[0].source_candle_sha256) == 64
        assert outcomes[0].source_retrieved_at_ms <= outcomes[0].evaluated_at_ms
    assert DB_URL is not None
    reopened = create_engine(DB_URL, pool_pre_ping=True)
    try:
        with Session(reopened) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AiResearchForecastOutcomeRow)
                    .where(AiResearchForecastOutcomeRow.receipt_id == receipt_id)
                )
                == 1
            )
        loaded = {item.signal_id: item for item in read_evaluated_forecasts(reopened)}[signal]
        assert loaded.forecast_recorded_at_ms is not None
        assert loaded.forecast_recorded_at_ms < loaded.label_available_at_ms
        assert loaded.realized_outcome.value == "WIN"
    finally:
        reopened.dispose()


def test_direct_sql_cannot_forge_outcome_and_rows_are_immutable(engine: Engine) -> None:
    _, receipt_id = seeded_pending(engine)
    with Session(engine) as session, pytest.raises(DBAPIError):
        session.execute(
            text(
                "INSERT INTO ai_research_forecast_outcomes "
                "(receipt_id,outcome,entry_close,target_close,flat_band_bps,source_url,"
                "source_candle_sha256,source_retrieved_at_ms,evaluated_at_ms) "
                "VALUES (:id,'LOSS',100,101,10,"
                "'https://data-api.binance.vision/api/v3/klines',repeat('0',64),1,1)"
            ),
            {"id": receipt_id},
        )
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError):
        session.execute(
            text(
                "INSERT INTO ai_research_forecast_outcomes "
                "(receipt_id,outcome,entry_close,target_close,flat_band_bps,source_url,"
                "source_candle_sha256,source_retrieved_at_ms,evaluated_at_ms) "
                "VALUES (:id,'WIN',100,101,10,'https://example.invalid/klines',"
                "repeat('0',64),1,1)"
            ),
            {"id": receipt_id},
        )
        session.commit()
    with Session(engine) as session, pytest.raises(DBAPIError):
        session.execute(
            text(
                "INSERT INTO ai_research_forecast_outcomes "
                "(receipt_id,outcome,entry_close,target_close,flat_band_bps,source_url,"
                "source_candle_sha256,source_retrieved_at_ms,evaluated_at_ms) "
                "VALUES (:id,'WIN',100,101,0,"
                "'https://data-api.binance.vision/api/v3/klines',repeat('0',64),1,1)"
            ),
            {"id": receipt_id},
        )
        session.commit()
    evaluate_due_forecasts(engine, market_client=FixturePublicClient())
    for statement in (
        "UPDATE ai_research_forecast_outcomes SET outcome='LOSS' WHERE receipt_id=:id",
        "DELETE FROM ai_research_forecast_outcomes WHERE receipt_id=:id",
        "TRUNCATE ai_research_forecast_outcomes",
    ):
        with Session(engine) as session, pytest.raises(DBAPIError):
            session.execute(text(statement), {"id": receipt_id})
            session.commit()
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AiResearchForecastOutcomeRow)
                .where(AiResearchForecastOutcomeRow.receipt_id == receipt_id)
            )
            == 1
        )
        receipt = session.get(AiResearchForecastReceiptRow, receipt_id)
        assert receipt is not None
        statements = (
            "UPDATE spot_candles SET close=close WHERE symbol='BTCUSDT' "
            "AND interval='1m' AND close_time_ms=:due",
            "DELETE FROM spot_candles WHERE symbol='BTCUSDT' "
            "AND interval='1m' AND close_time_ms=:due",
        )
        for statement in statements:
            with Session(engine) as mutation, pytest.raises(DBAPIError):
                mutation.execute(text(statement), {"due": receipt.outcome_due_at_ms})
                mutation.commit()
        with Session(engine) as mutation, pytest.raises(DBAPIError):
            mutation.execute(text("TRUNCATE spot_candles"))
            mutation.commit()
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0


def test_future_receipts_and_missing_candle_pairs_are_not_evaluated(engine: Engine) -> None:
    before = len(evaluate_due_forecasts(engine, market_client=FixturePublicClient()))
    now = database_now_ms(engine)
    close = now // 60_000 * 60_000 - 1
    # Normal API accepts a future outcome; no caller clock is exposed to evaluator.
    from binance_ai_trader.ai_constitution import ProbabilityEstimate
    from binance_ai_trader.ai_research_forecast_receipts_test import (
        ForecastReceiptInput,
        append_disposable_forecast,
    )

    append_disposable_forecast(
        engine,
        forecast=ForecastReceiptInput(
            "future_test_" + uuid.uuid4().hex[:16],
            "test_model_v1",
            "BTCUSDT",
            "1m",
            close,
            close + 60 * 60_000,
            ProbabilityEstimate(win=Decimal(".4"), loss=Decimal(".4"), flat=Decimal(".2")),
        ),
    )
    assert evaluate_due_forecasts(engine, market_client=FixturePublicClient()) == ()
    assert before >= 0


@pytest.mark.parametrize("case", ["missing", "corrupt", "unclosed"])
def test_missing_or_corrupt_closed_candle_data_is_deferred(engine: Engine, case: str) -> None:
    signal, receipt_id = seeded_pending(
        engine,
        omit_target=case == "missing",
        corrupt_target=case == "corrupt",
        unclosed_target=case == "unclosed",
    )
    assert signal not in {
        row.signal_id for row in evaluate_due_forecasts(engine, market_client=FixturePublicClient())
    }
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AiResearchForecastOutcomeRow)
                .where(AiResearchForecastOutcomeRow.receipt_id == receipt_id)
            )
            == 0
        )


def test_public_source_mismatch_and_429_fail_closed(engine: Engine) -> None:
    signal, receipt_id = seeded_pending(engine)
    with Session(engine) as session:
        receipt = session.get(AiResearchForecastReceiptRow, receipt_id)
        assert receipt is not None
        source_open = receipt.observed_close_at_ms - 60_000 + 1
    fixture = PUBLIC_FIXTURES[source_open]
    PUBLIC_FIXTURES[source_open] = (
        fixture[0],
        replace(
            fixture[1],
            open=Decimal("102"),
            high=Decimal("102"),
            low=Decimal("102"),
            close=Decimal("102"),
        ),
    )
    assert signal not in {
        row.signal_id for row in evaluate_due_forecasts(engine, market_client=FixturePublicClient())
    }
    with pytest.raises(MarketRateLimited):
        evaluate_due_forecasts(engine, market_client=LimitedPublicClient())
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AiResearchForecastOutcomeRow)
                .where(AiResearchForecastOutcomeRow.receipt_id == receipt_id)
            )
            == 0
        )
    PUBLIC_FIXTURES[source_open] = fixture
    assert signal in {
        row.signal_id for row in evaluate_due_forecasts(engine, market_client=FixturePublicClient())
    }


def test_database_scope_guard_does_not_touch_preview_or_remote(engine: Engine) -> None:
    assert DB_URL is not None
    fake = create_engine("postgresql+psycopg://localhost/trader_owner")
    try:
        with pytest.raises(ForecastReceiptRejected, match="isolated_forecast_database_only"):
            evaluate_due_forecasts(fake)
    finally:
        fake.dispose()
