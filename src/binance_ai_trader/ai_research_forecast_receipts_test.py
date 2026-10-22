"""BT-023D: TEST-ONLY immutable PostgreSQL forecast receipt rehearsal.

This deliberately cannot connect to a non-disposable/remote database or a
live worker. PostgreSQL sets receipt timestamps; caller cannot backdate them.
Forecasts are UNVERIFIED research input, not calibrated models or trade signals.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS
from binance_ai_trader.models import AiResearchForecastReceiptRow

_ALLOWED_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ALLOWED_MODEL: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_MAX_HORIZON = 86_400_000
_MAX_DECIMAL_PLACES = 12


class ForecastReceiptRejected(ValueError):
    """No record was accepted; deny-by-default research outcome."""


@dataclass(frozen=True, slots=True)
class ForecastReceiptInput:
    signal_id: str
    model_version: str
    symbol: str
    interval: str
    observed_close_at_ms: int
    outcome_due_at_ms: int
    forecast: ProbabilityEstimate


@dataclass(frozen=True, slots=True)
class ForecastReceipt:
    id: int
    signal_id: str
    symbol: str
    interval: str
    received_at_ms: int
    outcome_due_at_ms: int
    source: str = "disposable_postgres_offline_forecast"

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _disposable_only(engine: Engine) -> None:
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("127.0.0.1", "localhost")
        or engine.url.database != "trader_ai_forecast_test"
        or os.environ.get("AI_FORECAST_TEST_ISOLATED") != "1"
    ):
        raise ForecastReceiptRejected("isolated_forecast_database_only")


def _valid_probability(value: object) -> bool:
    if type(value) is not Decimal or not value.is_finite() or not _ZERO <= value <= _ONE:
        return False
    exponent = value.normalize().as_tuple().exponent
    return isinstance(exponent, int) and exponent >= -_MAX_DECIMAL_PLACES


def _validate(item: object) -> ForecastReceiptInput:
    if type(item) is not ForecastReceiptInput:
        raise ForecastReceiptRejected("invalid_forecast_receipt_input")
    if (
        type(item.signal_id) is not str
        or _ALLOWED_NAME.fullmatch(item.signal_id) is None
        or type(item.model_version) is not str
        or _ALLOWED_MODEL.fullmatch(item.model_version) is None
        or type(item.symbol) is not str
        or item.symbol not in SUPPORTED_SYMBOLS
        or type(item.interval) is not str
        or item.interval not in INTERVAL_MS
        or type(item.observed_close_at_ms) is not int
        or type(item.outcome_due_at_ms) is not int
        or type(item.forecast) is not ProbabilityEstimate
    ):
        raise ForecastReceiptRejected("invalid_forecast_receipt_input")
    period = INTERVAL_MS[item.interval]
    horizon = item.outcome_due_at_ms - item.observed_close_at_ms
    if (
        item.observed_close_at_ms <= 0
        or (item.observed_close_at_ms + 1) % period != 0
        or not period <= horizon <= _MAX_HORIZON
        or horizon % period != 0
    ):
        raise ForecastReceiptRejected("invalid_closed_candle_horizon")
    probabilities = (item.forecast.win, item.forecast.loss, item.forecast.flat)
    if not all(_valid_probability(p) for p in probabilities) or sum(probabilities, _ZERO) != _ONE:
        raise ForecastReceiptRejected("invalid_forecast_probabilities")
    return item


def _receipt(row: AiResearchForecastReceiptRow) -> ForecastReceipt:
    return ForecastReceipt(
        id=row.id,
        signal_id=row.signal_id,
        symbol=row.symbol,
        interval=row.interval,
        received_at_ms=row.received_at_ms,
        outcome_due_at_ms=row.outcome_due_at_ms,
    )


def append_disposable_forecast(
    engine: Engine, *, forecast: ForecastReceiptInput
) -> ForecastReceipt:
    """Create one immutable row; recorded_at is always PostgreSQL clock.

    Does NOT accept an operator token, caller clock, provenance override,
    synthetic label, training approval or order execution request.
    """
    _disposable_only(engine)
    item = _validate(forecast)
    try:
        with Session(engine) as session, session.begin():
            row = AiResearchForecastReceiptRow(
                signal_id=item.signal_id,
                model_version=item.model_version,
                symbol=item.symbol,
                interval=item.interval,
                observed_close_at_ms=item.observed_close_at_ms,
                outcome_due_at_ms=item.outcome_due_at_ms,
                prob_win=item.forecast.win,
                prob_loss=item.forecast.loss,
                prob_flat=item.forecast.flat,
            )
            session.add(row)
            session.flush()
            return _receipt(row)
    except IntegrityError as exc:
        raise ForecastReceiptRejected("duplicate_or_out_of_window_forecast") from exc


def read_disposable_forecasts(engine: Engine) -> tuple[ForecastReceipt, ...]:
    """Read only server-attested local receive times; never infer a label."""
    _disposable_only(engine)
    with Session(engine) as session:
        rows = session.scalars(
            select(AiResearchForecastReceiptRow).order_by(AiResearchForecastReceiptRow.id)
        ).all()
        return tuple(_receipt(row) for row in rows)
