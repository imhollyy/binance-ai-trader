"""Authenticated PAPER dashboard reads account, historical quote and last worker attempt only."""

from __future__ import annotations

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
    PaperWorkerGuardRow,
    TradingSettingsRow,
)

TOKEN = "paper-dashboard-local-admin-only-token-long"


@pytest.fixture
def read_session() -> Iterator[tuple[TestClient, Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with (
        TestClient(
            create_app(config=ApiConfig(database_url=None, admin_token=TOKEN), engine=engine)
        ) as client,
        Session(engine) as session,
    ):
        yield client, session
    engine.dispose()


def _get(client: TestClient, *, token: str | None = TOKEN) -> object:
    headers = {"X-Admin-Token": token} if token is not None else {}
    return client.get("/api/v1/paper/dashboard", headers=headers)


def test_empty_dashboard_and_auth_are_read_only(
    read_session: tuple[TestClient, Session],
) -> None:
    client, session = read_session
    uri = "/api/v1/paper/dashboard"
    assert client.get(uri).status_code == 401
    assert client.get(uri, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert (
        client.get(
            uri, headers={"X-Admin-Token": TOKEN, "Origin": "https://malicious.example"}
        ).status_code
        == 403
    )
    assert (
        client.get(uri, headers={"X-Admin-Token": TOKEN, "Host": "untrusted.example"}).status_code
        == 400
    )
    response = client.get(uri, headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert body["mode"] == "paper"
    assert body["source"] == "persisted_paper_spot"
    assert body["initialized"] is False
    assert body["account"] is None
    assert body["events"] == body["positions"] == []
    assert body["worker"] == {
        "requested_enabled": None,
        "process_verified_running": False,
        "last_status": None,
        "last_attempt_at_ms": None,
        "next_allowed_at_ms": None,
        "consecutive_failures": None,
    }
    assert client.get(uri + "?limit=31", headers={"X-Admin-Token": TOKEN}).status_code == 422
    assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0


def test_verified_last_attempt_is_not_a_live_process_claim(
    read_session: tuple[TestClient, Session],
) -> None:
    client, session = read_session
    session.add(
        TradingSettingsRow(
            id=1,
            version=1,
            data={"bot_enabled": True},
        )
    )
    session.add(
        PaperWorkerGuardRow(
            id=1,
            last_attempt_at_ms=1_800_000_000_100,
            next_allowed_at_ms=1_800_000_060_100,
            consecutive_failures=2,
            last_status="rate_limited",
        )
    )
    session.commit()
    response = client.get("/api/v1/paper/dashboard", headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    worker = response.json()["worker"]
    assert worker["requested_enabled"] is True
    assert worker["last_status"] == "rate_limited"
    assert worker["last_attempt_at_ms"] == 1_800_000_000_100
    assert worker["process_verified_running"] is False
    assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0


def test_realized_pnl_open_position_and_attached_quote_are_exact_strings(
    read_session: tuple[TestClient, Session],
) -> None:
    client, session = read_session
    now = datetime.now(UTC)
    account = PaperSpotAccountRow(
        id=1,
        settings_version=1,
        initial_capital_usdt=Decimal("1000"),
        cash_usdt=Decimal("890.25"),
        realized_pnl_usdt=Decimal("-2.5"),
        paid_fees_usdt=Decimal("0.15"),
    )
    session.add(account)
    session.flush()
    position = PaperSpotPositionRow(
        id=1,
        account_id=1,
        trade_id="test_trade_1",
        idempotency_key="test_trade_unique_1",
        symbol="BTCUSDT",
        status="open",
        quantity=Decimal("1"),
        entry_fill_price=Decimal("100.5"),
        entry_notional_usdt=Decimal("100.5"),
        entry_fee_usdt=Decimal("0.1"),
        stop_price=Decimal("98"),
        estimated_stop_loss_usdt=Decimal("2.6"),
        opened_at=now,
    )
    session.add(position)
    session.flush()
    session.add(
        PaperSpotJournalRow(
            id=1,
            account_id=1,
            position_id=None,
            event_id="account_opened_a",
            idempotency_key="open_account_a",
            event_type="account_opened",
            cash_delta_usdt=Decimal("1000"),
            balance_after_usdt=Decimal("1000"),
            fee_usdt=Decimal("0"),
            realized_pnl_delta_usdt=Decimal("0"),
            quantity_delta=Decimal("0"),
            created_at=now,
        )
    )
    journal = PaperSpotJournalRow(
        id=2,
        account_id=1,
        position_id=1,
        event_id="spot_buy_a",
        idempotency_key="spot_buy_unique_a",
        event_type="spot_buy",
        cash_delta_usdt=Decimal("-109.75"),
        balance_after_usdt=Decimal("890.25"),
        fee_usdt=Decimal("0.1"),
        realized_pnl_delta_usdt=Decimal("0"),
        quantity_delta=Decimal("1"),
        created_at=now,
    )
    session.add(journal)
    session.flush()
    session.add(
        PaperSpotObservedQuoteRow(
            id=1,
            journal_id=2,
            symbol="BTCUSDT",
            interval="1m",
            source="binance_public_spot_ticker_price",
            signal_close_time_ms=1_800_000_000_000,
            requested_at_ms=1_800_000_000_100,
            received_at_ms=1_800_000_000_150,
            reference_price=Decimal("100.45"),
            fee_bps=10,
            slippage_bps=5,
        )
    )
    session.commit()
    original = (
        session.scalar(select(func.count()).select_from(PaperSpotJournalRow)),
        session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)),
    )
    response = client.get("/api/v1/paper/dashboard?limit=10", headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True
    assert body["open_positions"] == body["total_positions"] == 1
    assert body["total_events"] == 2
    assert Decimal(body["account"]["cash_usdt"]) == Decimal("890.25")
    assert Decimal(body["account"]["realized_pnl_usdt"]) == Decimal("-2.5")
    assert body["positions"][0]["status"] == "open"
    assert body["events"][0]["event_type"] == "spot_buy"
    quote = body["events"][0]["quote"]
    assert Decimal(quote["reference_price"]) == Decimal("100.45")
    assert quote["observed_at_ms"] == 1_800_000_000_150
    assert quote["fee_bps"] == 10 and quote["slippage_bps"] == 5
    assert body["events"][1]["quote"] is None
    assert body["worker"]["process_verified_running"] is False
    for forbidden in ("equity", "unrealized_pnl", "live_order", "exchange_balance"):
        assert forbidden not in body
    assert original == (
        session.scalar(select(func.count()).select_from(PaperSpotJournalRow)),
        session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)),
    )


def test_missing_database_and_admin_token_denies_dashboard() -> None:
    with TestClient(create_app(config=ApiConfig(database_url=None, admin_token=TOKEN))) as c:
        assert c.get("/api/v1/paper/dashboard", headers={"X-Admin-Token": TOKEN}).status_code == 503
    with TestClient(create_app(config=ApiConfig(database_url=None, admin_token=None))) as c:
        assert c.get("/api/v1/paper/dashboard").status_code == 503
