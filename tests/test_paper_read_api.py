"""Read-only PAPER Spot API never bootstraps balances or exposes orders."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import (
    Base,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
)

TOKEN = "paper-read-unit-test-token-with-40-chars"


@pytest.fixture
def ledger() -> Iterator[tuple[TestClient, Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(config=ApiConfig(database_url=None, admin_token=TOKEN), engine=engine)
    with TestClient(app) as client, Session(engine) as session:
        yield client, session
    engine.dispose()


def test_paper_spot_read_auth_origin_and_empty_state(
    ledger: tuple[TestClient, Session],
) -> None:
    client, session = ledger
    uri = "/api/v1/paper/spot"
    assert client.get(uri).status_code == 401
    assert client.get(uri, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert (
        client.get(
            uri, headers={"X-Admin-Token": TOKEN, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    host_response = client.get(uri, headers={"X-Admin-Token": TOKEN, "Host": "evil.example"})
    assert host_response.status_code == 400
    response = client.get(uri, headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    body = response.json()
    assert body["mode"] == "paper"
    assert body["market_type"] == "spot"
    assert body["initialized"] is False
    assert body["account"] is None
    assert body["positions"] == []
    assert body["events"] == []
    assert body["total_positions"] == body["open_positions"] == body["total_events"] == 0
    assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
    assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
    invalid_limit = client.get(uri + "?events_limit=101", headers={"X-Admin-Token": TOKEN})
    assert invalid_limit.status_code == 422


def test_paper_account_values_are_strings_not_live_equity(
    ledger: tuple[TestClient, Session],
) -> None:
    client, session = ledger
    session.add(
        PaperSpotAccountRow(
            id=1,
            settings_version=1,
            initial_capital_usdt=Decimal("1000.00000000"),
            cash_usdt=Decimal("975.00000000"),
            realized_pnl_usdt=Decimal("-20.00000000"),
            paid_fees_usdt=Decimal("5.00000000"),
        )
    )
    session.commit()
    response = client.get("/api/v1/paper/spot", headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True
    assert body["account"]["mode"] == "paper"
    assert isinstance(body["account"]["cash_usdt"], str)
    assert isinstance(body["account"]["realized_pnl_usdt"], str)
    assert body["positions"] == []
    assert body["events"] == []
    for forbidden in ("equity", "unrealized_pnl", "net_worth", "live_price", "trade_endpoint"):
        assert forbidden not in body
        assert forbidden not in body["account"]


def test_journal_quote_cost_evidence_is_attached_and_missing_spread_stays_unknown(
    ledger: tuple[TestClient, Session],
) -> None:
    client, session = ledger
    account = PaperSpotAccountRow(
        id=1,
        settings_version=1,
        initial_capital_usdt=Decimal("1000"),
        cash_usdt=Decimal("900"),
        realized_pnl_usdt=Decimal("0"),
        paid_fees_usdt=Decimal("0.1"),
    )
    session.add(account)
    session.flush()
    session.add(
        PaperSpotPositionRow(
            id=1,
            account_id=1,
            trade_id="paper-trade-1",
            idempotency_key="paper-trade-key-1",
            symbol="BTCUSDT",
            status="closed",
            quantity=Decimal("1"),
            entry_fill_price=Decimal("100"),
            entry_notional_usdt=Decimal("100"),
            entry_fee_usdt=Decimal("1"),
            stop_price=Decimal("95"),
            estimated_stop_loss_usdt=Decimal("5.1"),
            exit_fill_price=Decimal("99"),
            exit_fee_usdt=Decimal("2"),
            net_proceeds_usdt=Decimal("98.8"),
            realized_pnl_usdt=Decimal("-13"),
            opened_at=datetime(2026, 10, 22, tzinfo=UTC),
            closed_at=datetime(2026, 10, 23, tzinfo=UTC),
        )
    )
    session.flush()
    event = PaperSpotJournalRow(
        id=1,
        account_id=1,
        position_id=1,
        event_id="paper-buy-event",
        idempotency_key="paper-buy-idempotency",
        event_type="spot_buy",
        cash_delta_usdt=Decimal("-100"),
        balance_after_usdt=Decimal("900"),
        fee_usdt=Decimal("0.1"),
        realized_pnl_delta_usdt=Decimal("0"),
        quantity_delta=Decimal("1"),
    )
    session.add(event)
    session.flush()
    session.add(
        PaperSpotObservedQuoteRow(
            id=1,
            journal_id=1,
            symbol="BTCUSDT",
            interval="1m",
            source="binance_public_spot_ticker_price",
            signal_close_time_ms=1_800_000_000_000,
            requested_at_ms=1_800_000_000_100,
            received_at_ms=1_800_000_000_150,
            reference_price=Decimal("100"),
            fee_bps=10,
            slippage_bps=5,
            spread_bps=None,
        )
    )
    session.commit()

    response = client.get("/api/v1/paper/spot", headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    body = response.json()
    assert body["events"][0]["quantity_delta"] == "1.000000000000000000"
    quote = body["events"][0]["observed_quote"]
    assert quote["reference_price"] == "100.000000000000000000"
    assert quote["fee_bps"] == 10
    assert quote["slippage_bps"] == 5
    assert quote["spread_bps"] is None
    position = body["positions"][0]
    assert position["realized_pnl_usdt"] == "-13.000000000000000000"
    assert position["gross_realized_pnl_usdt"] == "-10.000000000000000000"


def test_paper_read_requires_database_and_configured_token() -> None:
    with TestClient(create_app(config=ApiConfig(database_url=None, admin_token=TOKEN))) as client:
        assert client.get("/api/v1/paper/spot", headers={"X-Admin-Token": TOKEN}).status_code == 503
    with TestClient(create_app(config=ApiConfig(database_url=None, admin_token=None))) as client:
        assert client.get("/api/v1/paper/spot").status_code == 503
