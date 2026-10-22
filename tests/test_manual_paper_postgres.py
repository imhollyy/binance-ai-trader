"""Disposable PostgreSQL manual PAPER quote, replay and position settlement.

Never run against a user's database or the real Binance API. Only isolated
localhost trader_manual_test is eligible. All public HTTP is mocked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import MarketDataError
from binance_ai_trader.manual_paper_execution import (
    ManualPaperRequest,
    run_manual_paper_once,
)
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
URL = os.environ.get("TEST_POSTGRES_URL")
SAFE = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_manual_test"
)
pytestmark = pytest.mark.skipif(
    not SAFE or os.environ.get("PAPER_LEDGER_TEST_ISOLATED") != "1",
    reason="Only a fresh migrated disposable localhost trader_manual_test DB",
)
BUY_ID = UUID("00000000-0000-4000-8000-000000000011")
SELL_ID = UUID("00000000-0000-4000-8000-000000000012")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and SAFE
    db = create_engine(URL, pool_pre_ping=True)
    with Session(db) as session:
        for model in (TradingSettingsRow, PaperSpotAccountRow, PaperSpotJournalRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield db
    db.dispose()


def _mock_observer(*, now: int, ticker: str = "100", status: int = 200) -> PublicSpotObservationClient:
    start = now // 60_000 * 60_000
    klines = [
        [
            start - (60 - i) * 60_000, "100", "101", "99", "100", "4",
            start - (60 - i) * 60_000 + 59_999,
            "0", 1, "0", "0", "0",
        ]
        for i in range(60)
    ]

    def reply(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "data-api.binance.vision"
        assert req.method == "GET"
        assert "X-MBX-APIKEY" not in req.headers
        if req.url.path == "/api/v3/klines":
            return httpx.Response(200, json=klines)
        assert req.url.path == "/api/v3/ticker/price"
        return httpx.Response(
            status,
            json={"symbol": req.url.params["symbol"], "price": ticker},
            headers={"Retry-After": "30"},
        )

    return PublicSpotObservationClient(
        transport=httpx.MockTransport(reply), clock_ms=lambda: now
    )


def _request(
    action: str = "buy",
    request_id: UUID = BUY_ID,
    *,
    budget: str | None = "100",
    stop: str | None = "2",
) -> ManualPaperRequest:
    return ManualPaperRequest.model_validate(
        {
            "action": action,
            "symbol": "BTCUSDT",
            "request_id": str(request_id),
            "confirmation": "CONFIRM_MANUAL_PAPER_ONLY",
            "budget_usdt": budget,
            "stop_distance_pct": stop,
        }
    )


def _totals(db: Engine) -> tuple[int, int, int, D]:
    with Session(db) as s:
        account = s.get(PaperSpotAccountRow, 1)
        assert account is not None
        return (
            s.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            s.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            s.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
            account.cash_usdt,
        )


def test_manual_paper_account_consent_replay_quote_audit_and_close(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings().model_dump(mode="json"),
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    assert _totals(engine) == (0, 1, 0, D("1000"))

    # A non-price request failure, fake/outlier or Binance public 429 must not
    # create a position, journal row or a misleading quote audit.
    now = 1_800_000_000_200
    for price, status in (("200", 200), ("100", 429)):
        with pytest.raises((MarketDataError, ValueError)):
            run_manual_paper_once(
                engine, _request(), observer=_mock_observer(now=now, ticker=price, status=status)
            )
    assert _totals(engine) == (0, 1, 0, D("1000"))

    observer = _mock_observer(now=now)
    original = run_manual_paper_once(engine, _request(), observer=observer)
    assert original.receipt.event_type == "spot_buy"
    assert original.receipt.cash_after_usdt >= D("900")
    assert original.receipt.cash_after_usdt < D("901")
    assert original.reference_price == D("100")
    assert _totals(engine)[:3] == (1, 2, 1)
    with Session(engine) as session:
        stored = session.scalar(select(PaperSpotPositionRow))
        assert stored is not None and stored.status == "open"
        assert stored.quantity > 0 and stored.stop_price == D("98")
        assert session.scalar(select(PaperSpotObservedQuoteRow)) is not None

    # A restarted SQLAlchemy engine replays the exact persisted receipt without
    # fetching a new market sample (observer omitted intentionally).
    assert URL is not None
    reopened = create_engine(URL, pool_pre_ping=True)
    try:
        replay = run_manual_paper_once(reopened, _request())
        assert replay == original
    finally:
        reopened.dispose()
    assert _totals(engine)[:3] == (1, 2, 1)

    with pytest.raises(PaperExecutionRejected, match="idempotency_conflict"):
        run_manual_paper_once(engine, _request(budget="50"), observer=observer)
    with pytest.raises(PaperExecutionRejected, match="manual_position_already_open"):
        run_manual_paper_once(
            engine,
            _request(request_id=UUID("00000000-0000-4000-8000-000000000013")),
            observer=observer,
        )
    assert _totals(engine)[:3] == (1, 2, 1)

    after = _mock_observer(now=now + 60_000, ticker="100")
    closed = run_manual_paper_once(
        engine, _request("sell", SELL_ID, budget=None, stop=None), observer=after
    )
    assert closed.receipt.event_type == "spot_sell"
    assert closed.receipt.realized_pnl_delta_usdt < 0
    assert _totals(engine)[:3] == (1, 3, 2)
    assert run_manual_paper_once(
        engine, _request("sell", SELL_ID, budget=None, stop=None)
    ) == closed
    assert _totals(engine)[:3] == (1, 3, 2)
    with Session(engine) as session:
        pos = session.scalar(select(PaperSpotPositionRow))
        assert pos is not None and pos.status == "closed"
        row = session.get(TradingSettingsRow, 1)
        assert row is not None
        settings = RiskSettings.model_validate(row.data)
        assert settings.bot_enabled is False
