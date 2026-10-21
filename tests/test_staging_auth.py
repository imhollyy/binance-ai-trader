"""HTTPS staging security: access isolation, revocation, RBAC and CSRF."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import (
    Base,
    StagingLoginGateRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.staging_auth import COOKIE, password_hash

WEB = "https://app.staging.example"
BASE = "https://api.staging.example"
HEADERS = {"Origin": WEB}
VIEW_PASS = "dummy-viewer-only-synthetic-2026"
OPER_PASS = "dummy-operator-only-synthetic-2026"


def config() -> ApiConfig:
    return ApiConfig(
        database_url="sqlite+pysqlite:///:memory:",
        admin_token=None,
        auth_mode="staging",
        staging_origin=WEB,
        staging_host="api.staging.example",
        viewer_password_hash=password_hash(VIEW_PASS, salt=b"viewer-stg-test1"),
        operator_password_hash=password_hash(OPER_PASS, salt=b"operatr-stg-tst1"),
    )


@pytest.fixture
def staging() -> Iterator[tuple[TestClient, object]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(StagingLoginGateRow(id=1, failures=0, locked_until_ms=0))
    with TestClient(create_app(config=config(), engine=engine), base_url=BASE) as client:
        yield client, engine
    engine.dispose()


def login(client: TestClient, role: str = "viewer") -> str:
    passwd = VIEW_PASS if role == "viewer" else OPER_PASS
    result = client.post(
        "/api/v1/auth/login", headers=HEADERS,
        json={"username": role, "password": passwd},
    )
    assert result.status_code == 200, result.text
    assert result.json()["role"] == role
    assert "secure" in result.headers["set-cookie"].lower()
    assert "httponly" in result.headers["set-cookie"].lower()
    assert "samesite=strict" in result.headers["set-cookie"].lower()
    assert "__Host-tradepilot=" in result.headers["set-cookie"]
    return str(result.json()["csrf_token"])


def test_staging_requires_cookie_and_never_accepts_dev_token(
    staging: tuple[TestClient, object],
) -> None:
    client, engine = staging
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get(
        "/api/v1/settings", headers={"X-Admin-Token": "local-secret"}
    ).status_code == 403
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/v1/settings", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/v1/settings", headers={
        "Origin": "https://evil.example"
    }).status_code == 403
    assert client.post(
        "/api/v1/auth/login", json={"username": "viewer", "password": VIEW_PASS}
    ).status_code == 403
    assert client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example"},
        json={"username": "viewer", "password": VIEW_PASS},
    ).status_code == 403
    csrf = login(client)
    assert len(csrf) == 64
    assert client.get("/api/v1/auth/session", headers=HEADERS).json()["role"] == "viewer"
    assert client.get("/api/v1/status", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/paper/dashboard", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/paper/spot", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/market/candles", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/audit", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/settings", headers={
        "Origin": WEB, "X-Admin-Token": "fallback"
    }).status_code == 403
    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 0


def test_viewer_cannot_write_even_with_valid_csrf_and_approval(
    staging: tuple[TestClient, object],
) -> None:
    client, _ = staging
    csrf = login(client)
    snapshot = client.get("/api/v1/settings", headers=HEADERS).json()
    payload = {"expected_version": 0, "settings": snapshot["settings"]}
    assert client.put("/api/v1/settings", headers=HEADERS, json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers={
        **HEADERS, "X-CSRF-Token": csrf, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"
    }, json=payload).status_code == 403
    assert client.post("/api/v1/auth/logout", headers=HEADERS).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={
        **HEADERS, "X-CSRF-Token": "deadbeef"
    }).status_code == 403
    response = client.post("/api/v1/auth/logout", headers={
        **HEADERS, "X-CSRF-Token": csrf
    })
    assert response.status_code == 200
    assert client.get("/api/v1/settings", headers=HEADERS).status_code == 401


def test_operator_requires_csrf_explicit_approval_and_same_origin(
    staging: tuple[TestClient, object],
) -> None:
    client, engine = staging
    csrf = login(client, "operator")
    stored_cookie = client.cookies.get(COOKIE)
    snapshot = client.get("/api/v1/settings", headers=HEADERS).json()
    payload = {"expected_version": 0, "settings": snapshot["settings"]}
    h = {**HEADERS, "X-CSRF-Token": csrf, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"}
    assert client.put("/api/v1/settings", json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers=HEADERS, json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers={
        **HEADERS, "X-CSRF-Token": "x" * 64,
        "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }, json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers={
        **HEADERS, "X-CSRF-Token": csrf
    }, json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers={
        **h, "Origin": "https://evil.example"
    }, json=payload).status_code == 403
    assert client.put("/api/v1/settings", headers=h, json=payload).status_code == 200
    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 1
    assert client.post("/api/v1/auth/logout", headers={
        **HEADERS, "X-CSRF-Token": csrf
    }).status_code == 200
    assert stored_cookie is not None
    assert client.get(
        "/api/v1/settings", headers={**HEADERS, "Cookie": f"{COOKIE}={stored_cookie}"}
    ).status_code == 401
    # Old CSRF and cookie cannot authorize a replayed settings mutation.
    assert client.put("/api/v1/settings", headers={
        **h, "Cookie": f"{COOKIE}={stored_cookie}"
    }, json=payload).status_code == 401


def test_login_bruteforce_has_durable_shared_lockout(
    staging: tuple[TestClient, object],
) -> None:
    client, engine = staging
    for _ in range(5):
        assert client.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username": "viewer", "password": "incorrect"},
        ).status_code == 401
    assert client.post(
        "/api/v1/auth/login", headers=HEADERS,
        json={"username": "viewer", "password": VIEW_PASS},
    ).status_code == 429
    with Session(engine) as session:  # type: ignore[arg-type]
        gate = session.get(StagingLoginGateRow, 1)
        assert gate is not None
        assert gate.failures == 5 and gate.locked_until_ms > 0
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0


def test_config_fail_closed_and_session_credential_format() -> None:
    assert ApiConfig(database_url=None, admin_token=None).auth_mode == "local"
    with pytest.raises(ValueError):
        ApiConfig.from_env({
            "TRADING_MODE": "paper", "API_AUTH_MODE": "staging",
            "ADMIN_TOKEN": "a" * 64,
        })
    with pytest.raises(ValueError):
        ApiConfig.from_env({"TRADING_MODE": "paper", "API_AUTH_MODE": "untrusted"})
    with pytest.raises(ValueError):
        ApiConfig(
            database_url="sqlite://", admin_token=None, auth_mode="staging",
            staging_origin="http://app.staging.example",
            staging_host="api.staging.example",
            viewer_password_hash="bad", operator_password_hash="bad",
        )
