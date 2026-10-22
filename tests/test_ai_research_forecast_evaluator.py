"""BT-023E evaluator scope and clock-input guards."""

from __future__ import annotations

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
