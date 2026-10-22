"""BT-023E disposable PostgreSQL evaluator; offline research only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from binance_ai_trader.ai_research_forecast_receipts_test import ForecastReceiptRejected
from binance_ai_trader.models import (
    AiResearchForecastOutcomeRow,
    AiResearchForecastReceiptRow,
    SpotCandleRow,
)


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


def _guard(engine: Engine) -> None:
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("127.0.0.1", "localhost")
        or engine.url.database != "trader_ai_forecast_test"
        or os.environ.get("AI_FORECAST_TEST_ISOLATED") != "1"
    ):
        raise ForecastReceiptRejected("isolated_forecast_database_only")


def evaluate_due_forecasts(
    engine: Engine, *, flat_band_bps: Decimal = Decimal("0")
) -> tuple[Evaluation, ...]:
    """Evaluate eligible receipts using exact persisted source/target closes.

    Eligibility uses PostgreSQL clock; due candle must be closed before evaluation.
    Row locks with SKIP LOCKED make concurrent workers idempotent.
    """
    _guard(engine)
    if (
        type(flat_band_bps) is not Decimal
        or not flat_band_bps.is_finite()
        or not 0 <= flat_band_bps <= 200
    ):
        raise ForecastReceiptRejected("invalid_flat_band")
    results: list[Evaluation] = []
    with Session(engine) as session, session.begin():
        now_ms = int(
            session.scalar(
                text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint")
            )
        )
        rows = session.scalars(
            select(AiResearchForecastReceiptRow)
            .where(AiResearchForecastReceiptRow.outcome_due_at_ms <= now_ms)
            .order_by(AiResearchForecastReceiptRow.id)
            .with_for_update(skip_locked=True)
        ).all()
        for receipt in rows:
            exists = session.scalar(
                select(AiResearchForecastOutcomeRow.id).where(
                    AiResearchForecastOutcomeRow.receipt_id == receipt.id
                )
            )
            if exists is not None:
                continue
            candles = session.scalars(
                select(SpotCandleRow).where(
                    SpotCandleRow.symbol == receipt.symbol,
                    SpotCandleRow.interval == receipt.interval,
                    SpotCandleRow.close_time_ms.in_(
                        (receipt.observed_close_at_ms, receipt.outcome_due_at_ms)
                    ),
                )
            ).all()
            by_close = {c.close_time_ms: c for c in candles}
            source, target = (
                by_close.get(receipt.observed_close_at_ms),
                by_close.get(receipt.outcome_due_at_ms),
            )
            if source is None or target is None or target.close_time_ms >= now_ms:
                continue
            signed_bps = (target.close / source.close - Decimal(1)) * Decimal(10_000)
            label = (
                "WIN"
                if signed_bps > flat_band_bps
                else "LOSS"
                if signed_bps < -flat_band_bps
                else "FLAT"
            )
            row = AiResearchForecastOutcomeRow(
                receipt_id=receipt.id,
                outcome=label,
                entry_close=source.close,
                target_close=target.close,
                evaluated_at_ms=now_ms,
            )
            session.add(row)
            session.flush()
            results.append(
                Evaluation(
                    receipt.signal_id, label, source.close, target.close, row.evaluated_at_ms
                )
            )
    return tuple(results)
