"""Staging operator PAPER route denied to viewers, invalid CSRF and generic dev."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import Base, StagingLoginGateRow
from binance_ai_trader.staging_auth import password_hash

URL = "postgresql+psycopg://trader_preview:synthetic@preview-db:5432/trader_preview"
ORIGIN = "https://preview.staging.example"
HOST = "preview.staging.example"
VIEWER = "synthetic-manual-viewer-passphrase"
OPERATOR = "synthetic-manual-operator-passphrase"
V_HASH = password_hash(VIEWER, salt=b"manual-view-0001")
O_HASH = password_hash(OPERATOR, salt=b"manual-oper-0001")
GOOD = {
    "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
    "TRADING_MODE": "paper",
    "API_AUTH_MODE": "staging",
    "DATABASE_URL": URL,
    "STAGING_WEB_ORIGIN": ORIGIN,
    "STAGING_API_HOST": HOST,
    "STAGING_VIEWER_PASSWORD_HASH": V_HASH,
    "STAGING_OPERATOR_PASSWORD_HASH": O_HASH,
}
REQUEST = {
    "action": "buy",
    "symbol": "BTCUSDT",
    "request_id": str(UUID("00000000-0000-4000-8000-000000000015")),
    "budget_usdt": "50",
    "stop_distance_pct": "2",
    "confirmation": "CONFIRM_MANUAL_PAPER_ONLY",
}


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for name, value in GOOD.items():
        monkeypatch.setenv(name, value)
    for name in ("PAPER_WORKER_ENABLED", "PAPER_SCHEDULER_OPT_IN", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    db = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(db)
    with Session(db) as s, s.begin():
        s.add(StagingLoginGateRow(id=1, failures=0, locked_until_ms=0))
    cfg = ApiConfig(
        auth_mode="staging",
        database_url=URL,
        admin_token=None,
        staging_origin=ORIGIN,
        staging_host=HOST,
        viewer_password_hash=V_HASH,
        operator_password_hash=O_HASH,
    )
    with TestClient(create_app(config=cfg, engine=db), base_url=ORIGIN) as client:
        yield client
    db.dispose()


def _login(client: TestClient, user: str) -> str:
    password = VIEWER if user == "viewer" else OPERATOR
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": user, "password": password},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def test_manual_route_requires_operator_cookie_csrf_and_separate_confirmation(
    stage: TestClient,
) -> None:
    endpoint = "/api/v1/paper/manual"
    headers = {"Origin": ORIGIN}
    assert stage.post(endpoint, headers=headers, json=REQUEST).status_code == 401
    viewer_csrf = _login(stage, "viewer")
    elevated = {
        **headers,
        "X-CSRF-Token": viewer_csrf,
        "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }
    assert stage.post(endpoint, headers=elevated, json=REQUEST).status_code == 403
    stage.cookies.clear()
    operator_csrf = _login(stage, "operator")
    elevated["X-CSRF-Token"] = operator_csrf
    for bad in (
        {**elevated, "Origin": "https://evil.example"},
        {**elevated, "X-CSRF-Token": "x" * 64},
        {k: v for k, v in elevated.items() if k != "X-Paper-Settings-Approval"},
        {**elevated, "X-Paper-Settings-Approval": "WRONG"},
        {**elevated, "X-Admin-Token": "local-secret"},
    ):
        assert stage.post(endpoint, headers=bad, json=REQUEST).status_code == 403
    invalid = {**REQUEST, "confirmation": "CONFIRM_REAL_ORDER"}
    assert stage.post(endpoint, headers=elevated, json=invalid).status_code == 422
    tampered = {**REQUEST, "reference_price": "100"}
    assert stage.post(endpoint, headers=elevated, json=tampered).status_code == 422
    assert stage.post(endpoint, headers=elevated, json=REQUEST).status_code == 409
    # The injected SQLite DB is rejected by the PostgreSQL-only executor.
    # No simulated trade, worker, or Binance call can occur in this test.
    assert stage.get("/api/v1/paper/dashboard", headers=headers).json()["total_events"] == 0


def test_manual_endpoint_not_exposed_without_explicit_preview_flag(
    stage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIVATE_PREVIEW_OPT_IN")
    db = stage.app.state  # Existing stage fixture is separate from a newly created app.
    assert db is not None
    config = ApiConfig(
        auth_mode="staging", database_url=URL, admin_token=None,
        staging_origin=ORIGIN, staging_host=HOST,
        viewer_password_hash=V_HASH, operator_password_hash=O_HASH,
    )
    new_app = create_app(config=config, engine=None)
    with TestClient(new_app, base_url=ORIGIN) as client:
        response = client.post("/api/v1/paper/manual", headers={"Origin": ORIGIN}, json=REQUEST)
        assert response.status_code == 404
