"""PostgreSQL persistence and gap detection for validated CLOSED Spot candles."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle
from binance_ai_trader.models import SpotCandleRow

MAX_BACKFILL_WINDOW = 1500


def find_missing_open_times(
    observed_open_times: Sequence[int],
    *,
    start_ms: int,
    end_ms: int,
    interval: str,
) -> list[int]:
    """Identify bounded, aligned UTC opening times missing from a range, inclusive."""

    if interval not in INTERVAL_MS:
        raise ValueError("Unsupported interval")
    step = INTERVAL_MS[interval]
    if type(start_ms) is not int or type(end_ms) is not int:
        raise ValueError("Start/end timestamps must be integer milliseconds")
    if start_ms < 0 or end_ms < start_ms or start_ms % step or end_ms % step:
        raise ValueError("Start/end must be nonnegative aligned UTC times")
    expected_count = (end_ms - start_ms) // step + 1
    if expected_count > MAX_BACKFILL_WINDOW:
        raise ValueError("Gap scan exceeds bounded window")

    for timestamp in observed_open_times:
        if type(timestamp) is not int or timestamp % step:
            raise ValueError("Observed timestamp must be aligned")
        if not start_ms <= timestamp <= end_ms:
            raise ValueError("Observed timestamp outside requested range")
    present = set(observed_open_times)
    return [open_ms for open_ms in range(start_ms, end_ms + 1, step) if open_ms not in present]


def save_closed_candles(session: Session, candles: Sequence[Candle], *, now_ms: int) -> int:
    """Insert only fully closed, strictly validated candles.

    PostgreSQL ON CONFLICT makes repeated imports idempotent; caller controls
    the transaction and commits it. This function intentionally has no network I/O.
    """

    if type(now_ms) is not int or now_ms <= 0:
        raise ValueError("Invalid observation clock")
    rows: list[dict[str, str | int | Decimal]] = []
    seen: set[tuple[str, str, int]] = set()
    for candle in candles:
        step = INTERVAL_MS.get(candle.interval)
        if candle.symbol not in SUPPORTED_SYMBOLS or step is None:
            raise ValueError("Unsupported public market candle")
        if (
            candle.open_time_ms < 0
            or candle.open_time_ms % step != 0
            or candle.close_time_ms != candle.open_time_ms + step - 1
            or candle.close_time_ms >= now_ms
        ):
            raise ValueError("Only fully closed, aligned candles are allowed")
        prices = (candle.open, candle.high, candle.low, candle.close)
        if (
            any(not value.is_finite() or value <= 0 for value in prices)
            or not candle.volume.is_finite()
            or candle.volume < 0
            or candle.low > candle.high
            or any(not candle.low <= value <= candle.high for value in (candle.open, candle.close))
        ):
            raise ValueError("Invalid OHLCV values")
        key = (candle.symbol, candle.interval, candle.open_time_ms)
        if key in seen:
            raise ValueError("Duplicated candle in the same import batch")
        seen.add(key)
        rows.append(
            {
                "symbol": candle.symbol,
                "interval": candle.interval,
                "open_time_ms": candle.open_time_ms,
                "close_time_ms": candle.close_time_ms,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
        )

    if not rows:
        return 0

    statement = (
        insert(SpotCandleRow)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_spot_candles_symbol_interval_open")
        .returning(SpotCandleRow.id)
    )
    # psycopg reports rowcount=-1 for some multi-row inserts. RETURNING is exact.
    return len(session.scalars(statement).all())


def missing_stored_candles(
    session: Session,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[int]:
    """Return missing openings without contacting Binance or retrying its API."""

    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError("Unsupported symbol")
    # Validate bounds before querying the database.
    find_missing_open_times([], start_ms=start_ms, end_ms=end_ms, interval=interval)
    values = session.scalars(
        select(SpotCandleRow.open_time_ms).where(
            SpotCandleRow.symbol == symbol,
            SpotCandleRow.interval == interval,
            SpotCandleRow.open_time_ms >= start_ms,
            SpotCandleRow.open_time_ms <= end_ms,
        )
    ).all()
    return find_missing_open_times(values, start_ms=start_ms, end_ms=end_ms, interval=interval)
