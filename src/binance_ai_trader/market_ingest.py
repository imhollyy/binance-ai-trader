"""Bounded single-shot public Spot candle ingestion; NO orders or scheduler."""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import PublicSpotMarketClient
from binance_ai_trader.market_store import missing_stored_candles, save_closed_candles


@dataclass(frozen=True, slots=True)
class IngestReport:
    symbol: str
    interval: str
    received_closed: int
    inserted_new: int
    missing_open_times_ms: tuple[int, ...]


def ingest_once(
    engine: Engine,
    client: PublicSpotMarketClient,
    *,
    symbol: str,
    interval: str,
    limit: int = 100,
    now_ms: int | None = None,
) -> IngestReport:
    """Fetch closed market candles, then atomically save and identify gaps.

    A Binance failure causes no database writes. The caller must decide when
    to execute this function; it does not schedule work or automatically retry.
    """

    clock_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if type(clock_ms) is not int or clock_ms <= 0:
        raise ValueError("Invalid observation time")

    # Entire network operation finishes BEFORE the database transaction.
    candles = client.fetch_closed_klines(symbol=symbol, interval=interval, limit=limit)
    if not candles:
        return IngestReport(symbol, interval, 0, 0, ())

    with Session(engine) as session:
        with session.begin():
            inserted = save_closed_candles(session, candles, now_ms=clock_ms)
            missing = missing_stored_candles(
                session,
                symbol=symbol,
                interval=interval,
                start_ms=candles[0].open_time_ms,
                end_ms=candles[-1].open_time_ms,
            )
    return IngestReport(symbol, interval, len(candles), inserted, tuple(missing))
