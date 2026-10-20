"""FastAPI contract: safe status, authentication and optimistic settings updates."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import Base

TOKEN = "this-is-a-dev-test-token-of-40-characters"


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(config=ApiConfig(database_url=None, admin_token=TOKEN), engine=engine)
    with TestClient(app) as c:
        yield c
    engine.dispose()


def test_health_live_and_ready(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_no_database_not_ready() -> None:
    app = create_app(config=ApiConfig(database_url=None, admin_token=TOKEN))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_no_admin_token_fails_closed(client: TestClient) -> None:
    assert client.get("/api/v1/settings").status_code == 401
    assert client.put("/api/v1/settings", json={}).status_code == 401
    assert client.get("/api/v1/settings", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_public_origin_cannot_write(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings",
        headers={"X-Admin-Token": TOKEN, "Origin": "https://evil.example"},
        json={"expected_version": 0, "settings": {}},
    )
    assert response.status_code == 403


def test_status_never_claims_live_execution(client: TestClient) -> None:
    response = client.get("/api/v1/status", headers={"X-Admin-Token": TOKEN})
    assert response.status_code == 200
    assert response.json()["live_orders_supported"] is False
    assert response.json()["worker_active"] is False


def test_settings_persist_and_stale_update_rejected(client: TestClient) -> None:
    h = {"X-Admin-Token": TOKEN}
    baseline = client.get("/api/v1/settings", headers=h)
    assert baseline.status_code == 200
    assert baseline.json()["version"] == 0
    settings = baseline.json()["settings"]
    settings["spot_capital_usdt"] = 250
    created = client.put(
        "/api/v1/settings", headers=h, json={"expected_version": 0, "settings": settings}
    )
    assert created.status_code == 200
    assert created.json()["version"] == 1
    assert client.get("/api/v1/settings", headers=h).json()["settings"]["spot_capital_usdt"] == 250
    assert (
        client.put(
            "/api/v1/settings", headers=h, json={"expected_version": 0, "settings": settings}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "live"),
        ("risk_per_trade_pct", 20),
        ("daily_loss_limit_pct", 0),
        ("stop_loss_required", False),
        ("max_leverage", 100),
    ],
)
def test_unsafe_settings_rejected(client: TestClient, field: str, value: object) -> None:
    h = {"X-Admin-Token": TOKEN}
    settings = client.get("/api/v1/settings", headers=h).json()["settings"]
    settings[field] = value
    resp = client.put(
        "/api/v1/settings", headers=h, json={"expected_version": 0, "settings": settings}
    )
    assert resp.status_code == 422


def test_audit_written(client: TestClient) -> None:
    h = {"X-Admin-Token": TOKEN}
    settings = client.get("/api/v1/settings", headers=h).json()["settings"]
    resp = client.put(
        "/api/v1/settings", headers=h, json={"expected_version": 0, "settings": settings}
    )
    assert resp.status_code == 200
