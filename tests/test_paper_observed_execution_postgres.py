"""Disposable PostgreSQL signal-to-observed-public-price tests (PAPER ONLY).

Runs only against a fresh localhost trader_observed_test PostgreSQL database.
The HTTP transport is MOCKED, and no real exchange endpoint is contacted.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_observed_execution import (
    ObservedPaperResult,
    ObservedPaperStatus,
    run_observed_paper_spot_once,
)
from binance_ai_trader.paper_strategy import SmaCrossParameters
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
PARAMS = SmaCrossParameters(fast_window=3, slow_window=6)
URL = os.environ.get("TEST_POSTGRES_URL")
VALID = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_observed_test"
)
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
pytestmark = pytest.mark.skipif(
    not VALID or not ISOLATED,
    reason="Requires a dedicated disposable localhost trader_observed_test DB",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and VALID and ISOLATED
    db = create_engine(URL, pool_pre_ping=True)
    with Session(db) as session:
        for model in (
            SpotCandleRow, PaperSpotAccountRow, PaperSpotPositionRow,
            PaperSpotJournalRow, PaperSpotObservedQuoteRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield db
    db.dispose()


def _row(ms: int, close: str = "100") -> SpotCandleRow:
    return SpotCandleRow(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=ms,
        close_time_ms=ms + 59_999,
        open=D("100"),
        high=D("110"),
        low=D("90"),
        close=D(close),
        volume=D("2"),
    )


def _counts(db: Engine) -> tuple[int, int, int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
        )


def _observer(
    db: Engine,
    clock_ms: int,
    *,
    ticker: str = "102.2",
    status: int = 200,
) -> PublicSpotObservationClient:
    with Session(db) as session:
        records = list(
            session.scalars(
                select(SpotCandleRow).order_by(SpotCandleRow.open_time_ms)
            )
        )
        history: list[list[object]] = [
            [
                row.open_time_ms, str(row.open), str(row.high), str(row.low),
                str(row.close), str(row.volume), row.close_time_ms,
                "0", 1, "0", "0", "0",
            ]
            for row in records
        ]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert "X-MBX-APIKEY" not in request.headers
        if request.url.path == "/api/v3/klines":
            return httpx.Response(200, json=history)
        assert request.url.path == "/api/v3/ticker/price"
        return httpx.Response(
            status, json={"symbol": "BTCUSDT", "price": ticker},
            headers={"Retry-After": "30"},
        )

    return PublicSpotObservationClient(
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: clock_ms,
    )


def _run(db: Engine, clock_ms: int, *, ticker: str = "102.2") -> ObservedPaperResult:
    return run_observed_paper_spot_once(
        db,
        _observer(db, clock_ms, ticker=ticker),
        symbol="BTCUSDT", interval="1m", params=PARAMS,
    )


def test_public_sample_atomic_account_audit_rollback_and_sell(engine: Engine) -> None:
    start = (time.time_ns() // 60_000_000_000) * 60_000
    clock = start + 100

    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(
                    bot_enabled=True, manual_approval=False, risk_per_trade_pct=2
                ).model_dump(mode="json"),
            )
        )
        session.add_all(
            [_row(start - (60 - i) * 60_000, "102" if i == 59 else "100")
             for i in range(60)]
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    initial = _counts(engine)
    assert initial == (1, 0, 1, 0)

    # Public rate limits, price outliers and gap-down risk MUST NOT debit cash.
    with pytest.raises(PaperExecutionRejected, match="public_spot_observation_blocked"):
        run_observed_paper_spot_once(
            engine, _observer(engine, clock, status=429),
            symbol="BTCUSDT", interval="1m", params=PARAMS,
        )
    with pytest.raises(PaperExecutionRejected, match="public_spot_observation_blocked"):
        _run(engine, clock, ticker="200")
    with pytest.raises(PaperExecutionRejected, match="stop_not_below_entry"):
        _run(engine, clock, ticker="99")
    assert _counts(engine) == initial

    # Audit failure MUST atomically roll back already staged CASH/position/journal.
    def fail_audit(_: Session, _context: object, _instances: object) -> None:
        if any(isinstance(x, PaperSpotObservedQuoteRow) for x in _.new):
            raise RuntimeError("simulated_public_quote_audit_failure")

    event.listen(Session, "before_flush", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="simulated_public_quote_audit_failure"):
            _run(engine, clock)
    finally:
        event.remove(Session, "before_flush", fail_audit)
    assert _counts(engine) == initial

    barrier = Barrier(4)

    def concurrent(_: int) -> ObservedPaperResult:
        barrier.wait(timeout=10)
        return _run(engine, clock)

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(concurrent, range(4)))
    recorded = [x for x in outcomes if x.status is ObservedPaperStatus.RECORDED]
    assert len(recorded) == 1
    first = recorded[0]
    assert first.receipt is not None
    assert first.receipt.event_type == "spot_buy"
    assert first.reference_price == D("102.2")
    assert _counts(engine) == (1, 1, 2, 1)

    with Session(engine) as session:
        quote = session.scalar(select(PaperSpotObservedQuoteRow))
        journal = session.scalar(
            select(PaperSpotJournalRow).where(PaperSpotJournalRow.event_type == "spot_buy")
        )
        position = session.scalar(select(PaperSpotPositionRow))
        assert quote is not None and journal is not None and position is not None
        assert quote.journal_id == journal.id
        assert quote.reference_price == D("102.2")
        assert quote.received_at_ms == clock
        assert quote.signal_close_time_ms == start - 1
        assert quote.fee_bps == 10 and quote.slippage_bps == 5
        assert position.entry_fill_price == D("102.2") * D("1.0005")
        assert quote.source == "binance_public_spot_ticker_price"

    # Existing journal and quote audit both refuse mutation.
    with Session(engine) as session, pytest.raises(Exception):
        session.execute(
            text("UPDATE paper_spot_observed_quotes SET reference_price = 100 WHERE id = 1")
        )
        session.commit()
    assert _run(engine, clock).status is ObservedPaperStatus.NO_SIGNAL
    assert _counts(engine) == (1, 1, 2, 1)

    # Four flat highs followed by a downward cross, with a new actually
    # observed public reference. This is not a historical N+1 OPEN fill.
    with Session(engine) as session, session.begin():
        session.add_all(
            [_row(start + i * 60_000, "102") for i in range(4)]
        )
        session.add(_row(start + 4 * 60_000, "98"))
    later = start + 300_100
    exit_result = _run(engine, later, ticker="98.1")
    assert exit_result.status is ObservedPaperStatus.RECORDED
    assert exit_result.receipt is not None
    assert exit_result.receipt.event_type == "spot_sell"
    assert _counts(engine) == (1, 1, 3, 2)
    with Session(engine) as session:
        assert session.scalar(select(PaperSpotPositionRow)).status == "closed"  # type: ignore[union-attr]
        quotes = session.scalars(
            select(PaperSpotObservedQuoteRow).order_by(PaperSpotObservedQuoteRow.id)
        ).all()
        assert [q.reference_price for q in quotes] == [D("102.2"), D("98.1")]
    assert _run(engine, later, ticker="98.1").status is ObservedPaperStatus.NO_SIGNAL
    assert _counts(engine) == (1, 1, 3, 2)
