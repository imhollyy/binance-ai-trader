"""BT-023E disposable PostgreSQL evaluator; offline research only."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_model_review import ForecastOutcome, ObservedOutcome
from binance_ai_trader.ai_research_forecast_receipts_test import ForecastReceiptRejected
from binance_ai_trader.market_data import (
    INTERVAL_MS,
    PUBLIC_MARKET_URL,
    Candle,
    PublicSpotMarketClient,
)
from binance_ai_trader.models import (
    AiResearchForecastOutcomeRow,
    AiResearchForecastReceiptRow,
    SpotCandleRow,
)

FLAT_BAND_BPS = Decimal("10")


@dataclass(frozen=True, slots=True)
class Evaluation:
    signal_id: str
    outcome: str
    entry_close: Decimal
    target_close: Decimal
    evaluated_at_ms: int

    @property
    def no_trade(self) -> bool:
        return True


def _valid_closed_candle(candle: SpotCandleRow, *, period_ms: int) -> bool:
    prices = (candle.open, candle.high, candle.low, candle.close)
    return (
        candle.open_time_ms > 0
        and candle.open_time_ms % period_ms == 0
        and candle.close_time_ms == candle.open_time_ms + period_ms - 1
        and all(value.is_finite() and value > 0 for value in prices)
        and candle.volume.is_finite()
        and candle.volume >= 0
        and candle.low <= min(candle.open, candle.close)
        and candle.high >= max(candle.open, candle.close)
    )


def _matches_persisted(public: Candle, stored: SpotCandleRow) -> bool:
    return (
        public.symbol == stored.symbol
        and public.interval == stored.interval
        and public.open_time_ms == stored.open_time_ms
        and public.close_time_ms == stored.close_time_ms
        and public.open == stored.open
        and public.high == stored.high
        and public.low == stored.low
        and public.close == stored.close
        and public.volume == stored.volume
    )


def _source_digest(candles: list[Candle]) -> str:
    rows = [
        [
            candle.symbol,
            candle.interval,
            candle.open_time_ms,
            candle.close_time_ms,
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
        ]
        for candle in candles
    ]
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _guard(engine: Engine) -> None:
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("127.0.0.1", "localhost")
        or engine.url.database != "trader_ai_forecast_test"
        or os.environ.get("AI_FORECAST_TEST_ISOLATED") != "1"
    ):
        raise ForecastReceiptRejected("isolated_forecast_database_only")


def evaluate_due_forecasts(
    engine: Engine,
    *,
    market_client: PublicSpotMarketClient | None = None,
    batch_limit: int = 100,
) -> tuple[Evaluation, ...]:
    """Verify due stored candles against Binance public Spot data, then label.

    No auth, order, or account endpoints are available. Exchange throttling and
    network errors propagate without an outcome for the affected receipt.
    """
    _guard(engine)
    if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
        raise ForecastReceiptRejected("invalid_evaluation_batch_limit")
    source_client = market_client or PublicSpotMarketClient()
    with Session(engine) as session:
        now_ms = int(
            session.scalar(
                text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint")
            )
        )
        candidates = session.execute(
            select(
                AiResearchForecastReceiptRow.id,
                AiResearchForecastReceiptRow.signal_id,
                AiResearchForecastReceiptRow.symbol,
                AiResearchForecastReceiptRow.interval,
                AiResearchForecastReceiptRow.observed_close_at_ms,
                AiResearchForecastReceiptRow.outcome_due_at_ms,
            )
            .where(
                AiResearchForecastReceiptRow.outcome_due_at_ms <= now_ms,
                ~select(AiResearchForecastOutcomeRow.id)
                .where(AiResearchForecastOutcomeRow.receipt_id == AiResearchForecastReceiptRow.id)
                .exists(),
            )
            .order_by(AiResearchForecastReceiptRow.id)
            .limit(batch_limit)
        ).all()
    results: list[Evaluation] = []
    for candidate in candidates:
        receipt_id, signal_id, symbol, interval, observed_close, due_close = candidate
        period = INTERVAL_MS.get(interval)
        if period is None:
            continue
        expected_count = (due_close - observed_close) // period + 1
        source_open = observed_close - period + 1
        public_candles: list[Candle] = []
        cursor = source_open
        while len(public_candles) < expected_count:
            page_limit = min(1000, expected_count - len(public_candles))
            page = source_client.fetch_closed_klines(
                symbol=symbol,
                interval=interval,
                limit=page_limit,
                start_time_ms=cursor,
            )
            if not page:
                break
            public_candles.extend(page)
            cursor = page[-1].open_time_ms + period
            if len(page) < page_limit:
                break
        if len(public_candles) != expected_count or any(
            candle.open_time_ms != source_open + index * period or candle.close_time_ms >= now_ms
            for index, candle in enumerate(public_candles)
        ):
            continue
        source_retrieved_at_ms = time.time_ns() // 1_000_000
        source_digest = _source_digest(public_candles)
        with Session(engine) as session, session.begin():
            transaction_now = int(
                session.scalar(
                    text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint")
                )
            )
            receipt = session.scalar(
                select(AiResearchForecastReceiptRow)
                .where(
                    AiResearchForecastReceiptRow.id == receipt_id,
                    AiResearchForecastReceiptRow.outcome_due_at_ms <= transaction_now,
                )
                .with_for_update(skip_locked=True)
            )
            if receipt is None:
                continue
            exists = session.scalar(
                select(AiResearchForecastOutcomeRow.id).where(
                    AiResearchForecastOutcomeRow.receipt_id == receipt_id
                )
            )
            if exists is not None:
                continue
            stored_candles = session.scalars(
                select(SpotCandleRow)
                .where(
                    SpotCandleRow.symbol == symbol,
                    SpotCandleRow.interval == interval,
                    SpotCandleRow.close_time_ms.between(observed_close, due_close),
                )
                .order_by(SpotCandleRow.close_time_ms)
            ).all()
            if len(stored_candles) != expected_count or any(
                not _valid_closed_candle(candle, period_ms=period)
                or not _matches_persisted(public_candle, candle)
                for public_candle, candle in zip(public_candles, stored_candles, strict=False)
            ):
                continue
            source, target = stored_candles[0], stored_candles[-1]
            if (
                source.close_time_ms != observed_close
                or target.close_time_ms != due_close
                or target.close_time_ms >= transaction_now
            ):
                continue
            signed_bps = (target.close / source.close - Decimal(1)) * Decimal(10_000)
            label = (
                "WIN"
                if signed_bps > FLAT_BAND_BPS
                else "LOSS"
                if signed_bps < -FLAT_BAND_BPS
                else "FLAT"
            )
            row = AiResearchForecastOutcomeRow(
                receipt_id=receipt_id,
                outcome=label,
                entry_close=source.close,
                target_close=target.close,
                flat_band_bps=FLAT_BAND_BPS,
                source_url=PUBLIC_MARKET_URL + "/api/v3/klines",
                source_candle_sha256=source_digest,
                source_retrieved_at_ms=source_retrieved_at_ms,
                evaluated_at_ms=transaction_now,
            )
            session.add(row)
            session.flush()
            results.append(
                Evaluation(signal_id, label, source.close, target.close, row.evaluated_at_ms)
            )
    return tuple(results)


def read_evaluated_forecasts(engine: Engine) -> tuple[ForecastOutcome, ...]:
    """Read only server-received forecasts with immutable outcomes from test DB."""
    _guard(engine)
    with Session(engine) as session:
        rows = session.execute(
            select(AiResearchForecastReceiptRow, AiResearchForecastOutcomeRow)
            .join(
                AiResearchForecastOutcomeRow,
                AiResearchForecastOutcomeRow.receipt_id == AiResearchForecastReceiptRow.id,
            )
            .order_by(AiResearchForecastReceiptRow.observed_close_at_ms)
        ).all()
        return tuple(
            ForecastOutcome(
                signal_id=receipt.signal_id,
                model_version=receipt.model_version,
                symbol=receipt.symbol,
                interval=receipt.interval,
                observed_close_at_ms=receipt.observed_close_at_ms,
                label_available_at_ms=receipt.outcome_due_at_ms,
                forecast=ProbabilityEstimate(
                    win=receipt.prob_win,
                    loss=receipt.prob_loss,
                    flat=receipt.prob_flat,
                ),
                realized_outcome=ObservedOutcome(outcome.outcome),
                forecast_recorded_at_ms=receipt.received_at_ms,
            )
            for receipt, outcome in rows
        )
