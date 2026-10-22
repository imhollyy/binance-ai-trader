"""BT-023E evaluator scope and clock-input guards."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.ai_research_forecast_evaluator_test import evaluate_due_forecasts
from binance_ai_trader.ai_research_forecast_receipts_test import ForecastReceiptRejected


def test_evaluator_rejects_non_postgres_database_before_connection() -> None:
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(ForecastReceiptRejected, match="isolated_forecast_database_only"):
            evaluate_due_forecasts(engine)
    finally:
        engine.dispose()


def test_evaluator_rejects_invalid_flat_band_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_FORECAST_TEST_ISOLATED", "1")
    engine = create_engine("postgresql+psycopg://127.0.0.1/trader_ai_forecast_test")
    try:
        for band in (Decimal("-1"), Decimal("201"), Decimal("NaN"), 1):
            with pytest.raises(ForecastReceiptRejected, match="invalid_flat_band"):
                evaluate_due_forecasts(engine, flat_band_bps=band)  # type: ignore[arg-type]
    finally:
        engine.dispose()
