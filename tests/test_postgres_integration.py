"""Real PostgreSQL smoke tests run by the dedicated CI integration job."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import AuditRow

DB_URL = os.environ.get("TEST_POSTGRES_URL")
TOKEN = "integration-test-only-admin-token-12345"
HEADERS = {"X-Admin-Token": TOKEN}

pytestmark = pytest.mark.skipif(DB_URL is None, reason="Requires CI PostgreSQL service")


@pytest.fixture
def postgres_engine() -> Engine:
    """Migrations are applied by CI, never create_all for the production schema."""

    assert DB_URL is not None
    engine = create_engine(DB_URL, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE settings_audit, trading_settings RESTART IDENTITY"))
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
        assert client.get("/api/v1/audit", headers=HEADERS).json()["events"][0]["version"] == 1

    # New Engine + new app simulates an application/container restart.
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
            assert client.put(
                "/api/v1/settings",
                headers=HEADERS,
                json={"expected_version": 0, "settings": settings},
            ).status_code == 409
            assert client.get("/api/v1/audit", headers=HEADERS).json()["events"][0]["version"] == 1
            updated = dict(settings)
            updated["spot_capital_usdt"] = 1500
            assert client.put(
                "/api/v1/settings",
                headers=HEADERS,
                json={"expected_version": 1, "settings": updated},
            ).status_code == 200
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
        assert client.put(
            "/api/v1/settings",
            headers=HEADERS,
            json={"expected_version": 0, "settings": data},
        ).status_code == 422
        assert client.get("/api/v1/audit", headers=HEADERS).json()["events"] == []
        assert client.get("/api/v1/settings", headers=HEADERS).json()["version"] == 0
