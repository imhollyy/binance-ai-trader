"""Real PostgreSQL smoke tests run by the dedicated CI integration job."""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.market_data import Candle
from binance_ai_trader.market_store import missing_stored_candles, save_closed_candles
from binance_ai_trader.models import AuditRow, SpotCandleRow

DB_URL = os.environ.get("TEST_POSTGRES_URL")
TOKEN = "integration-test-only-admin-token-12345"
HEADERS = {"X-Admin-Token": TOKEN}

pytestmark = pytest.mark.skipif(DB_URL is None, reason="Requires CI PostgreSQL service")


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Use Alembic-created tables from CI, not metadata.create_all."""

    assert DB_URL is not None
    engine = create_engine(DB_URL, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE settings_audit, trading_settings RESTART IDENTITY"))
        connection.execute(text("TRUNCATE TABLE spot_candles RESTART IDENTITY"))
    yield engine
    engine.dispose()


def test_postgres_api_restart_persists_settings_and_audit(postgres_engine: Engine) -> None:
    assert DB_URL is not None
    app = create_app(config=ApiConfig(database_url=None, admin_token=TOKEN), engine=postgres_engine)
    with TestClient(app) as client:
        default = client.get("/api/v1/settings", headers=HEADERS).json()
        assert default["version"] == 0
        settings = default["settings"]
        settings["spot_capital_usdt"] = 1250
        response = client.put(
            "/api/v1/settings",
            headers=HEADERS,
            json={"expected_version": 0, "settings": settings},
        )
        assert response.status_code == 200, response.text
        assert client.get("/health/ready").status_code == 200
        audit_response = client.get("/api/v1/audit", headers=HEADERS)
        assert audit_response.json()["events"][0]["version"] == 1

    # Create another engine and app; the rows must not disappear on restart.
    postgres_engine.dispose()
    restored_engine = create_engine(DB_URL, pool_pre_ping=True)
    try:
        restarted = create_app(
            config=ApiConfig(database_url=None, admin_token=TOKEN), engine=restored_engine
        )
        with TestClient(restarted) as client:
            saved = client.get("/api/v1/settings", headers=HEADERS).json()
            assert saved["version"] == 1
            assert saved["settings"]["spot_capital_usdt"] == 1250

            stale = client.put(
                "/api/v1/settings",
                headers=HEADERS,
                json={"expected_version": 0, "settings": settings},
            )
            assert stale.status_code == 409
            audit_response = client.get("/api/v1/audit", headers=HEADERS)
            assert audit_response.json()["events"][0]["version"] == 1

            updated = dict(settings)
            updated["spot_capital_usdt"] = 1500
            saved_again = client.put(
                "/api/v1/settings",
                headers=HEADERS,
                json={"expected_version": 1, "settings": updated},
            )
            assert saved_again.status_code == 200
        with Session(restored_engine) as session:
            entries = session.query(AuditRow).order_by(AuditRow.version).all()
            assert [entry.version for entry in entries] == [1, 2]
            assert [entry.data["spot_capital_usdt"] for entry in entries] == [1250, 1500]
    finally:
        restored_engine.dispose()


def test_failed_validation_does_not_change_audit(postgres_engine: Engine) -> None:
    app = create_app(config=ApiConfig(database_url=None, admin_token=TOKEN), engine=postgres_engine)
    with TestClient(app) as client:
        data = client.get("/api/v1/settings", headers=HEADERS).json()["settings"]
        data["mode"] = "live"
        rejected = client.put(
            "/api/v1/settings",
            headers=HEADERS,
            json={"expected_version": 0, "settings": data},
        )
        assert rejected.status_code == 422
        assert client.get("/api/v1/audit", headers=HEADERS).json()["events"] == []
        assert client.get("/api/v1/settings", headers=HEADERS).json()["version"] == 0


def test_postgres_closed_candle_upsert_and_gap_scan(postgres_engine: Engine) -> None:
    first = Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=0,
        close_time_ms=59_999,
        open=Decimal("100.123456789123456789"),
        high=Decimal("110.123456789123456789"),
        low=Decimal("90.000000000000000001"),
        close=Decimal("105.123456789123456789"),
        volume=Decimal("15.123456789123456789"),
    )
    third = Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=120_000,
        close_time_ms=179_999,
        open=Decimal("106"),
        high=Decimal("111"),
        low=Decimal("101"),
        close=Decimal("110"),
        volume=Decimal("20"),
    )
    with Session(postgres_engine) as session:
        with session.begin():
            assert save_closed_candles(session, [first, third], now_ms=180_000) == 2
            assert save_closed_candles(session, [first, third], now_ms=180_000) == 0
        gaps = missing_stored_candles(
            session, symbol="BTCUSDT", interval="1m", start_ms=0, end_ms=120_000
        )
        assert gaps == [60_000]
        stored = session.query(SpotCandleRow).order_by(SpotCandleRow.open_time_ms).all()
        assert len(stored) == 2
        assert stored[0].open == first.open
        assert stored[0].volume == first.volume

    assert DB_URL is not None
    postgres_engine.dispose()
    restored = create_engine(DB_URL, pool_pre_ping=True)
    try:
        with Session(restored) as session:
            assert session.query(SpotCandleRow).count() == 2
            assert missing_stored_candles(
                session, symbol="BTCUSDT", interval="1m", start_ms=0, end_ms=120_000
            ) == [60_000]
    finally:
        restored.dispose()


def test_candle_store_rejects_partial_candles(postgres_engine: Engine) -> None:
    incomplete = Candle(
        symbol="ETHUSDT",
        interval="1m",
        open_time_ms=120_000,
        close_time_ms=179_999,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("5"),
    )
    with Session(postgres_engine) as session:
        with pytest.raises(ValueError, match="fully closed"):
            save_closed_candles(session, [incomplete], now_ms=170_000)
        assert session.query(SpotCandleRow).count() == 0
