"""HTTPS staging auth: isolation, session revocation, CSRF and RBAC."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, select
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
def staging() -> Iterator[tuple[TestClient, Engine]]:
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


def code(
    client: TestClient,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> int:
    return client.request(method, path, headers=headers, json=payload).status_code


def login(client: TestClient, role: str = "viewer") -> str:
    passwd = VIEW_PASS if role == "viewer" else OPER_PASS
    result = client.post(
        "/api/v1/auth/login",
        headers=HEADERS,
        json={"username": role, "password": passwd},
    )
    assert result.status_code == 200, result.text
    assert result.json()["role"] == role
    cookie = result.headers["set-cookie"].lower()
    assert all(x in cookie for x in ("secure", "httponly", "samesite=strict", "__host-"))
    return str(result.json()["csrf_token"])


def test_stage_no_local_admin_token_or_untrusted_https(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    assert code(client, "/api/v1/settings") == 401
    assert code(client, "/api/v1/settings", headers={"X-Admin-Token": "legacy"}) == 403
    assert code(client, "/docs") == 404
    assert code(client, "/openapi.json") == 404
    assert code(client, "/api/v1/settings", headers={"Host": "evil.example"}) == 400
    assert code(client, "/api/v1/settings", headers={"Origin": "https://evil.example"}) == 403
    payload = {"username": "viewer", "password": VIEW_PASS}
    assert code(client, "/api/v1/auth/login", method="POST", payload=payload) == 403
    assert code(
        client, "/api/v1/auth/login", method="POST", payload=payload,
        headers={"Origin": "https://evil.example"},
    ) == 403
    csrf = login(client)
    assert len(csrf) == 64
    assert client.get("/api/v1/auth/session", headers=HEADERS).json()["role"] == "viewer"
    for path in (
        "/api/v1/status", "/api/v1/settings", "/api/v1/paper/dashboard",
        "/api/v1/paper/spot", "/api/v1/market/candles", "/api/v1/audit",
    ):
        assert code(client, path, headers=HEADERS) == 200
    assert code(
        client, "/api/v1/settings", headers={**HEADERS, "X-Admin-Token": "legacy"}
    ) == 403
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 0


def test_viewer_denied_settings_and_csrf_logout_replay(
    staging: tuple[TestClient, Engine],
) -> None:
    client, _ = staging
    csrf = login(client)
    baseline = client.get("/api/v1/settings", headers=HEADERS).json()
    payload = {"expected_version": 0, "settings": baseline["settings"]}
    privileged = {
        **HEADERS, "X-CSRF-Token": csrf, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }
    assert code(client, "/api/v1/settings", method="PUT", headers=HEADERS, payload=payload) == 403
    assert code(
        client, "/api/v1/settings", method="PUT", headers=privileged, payload=payload
    ) == 403
    assert code(client, "/api/v1/auth/logout", method="POST", headers=HEADERS) == 403
    assert code(
        client, "/api/v1/auth/logout", method="POST",
        headers={**HEADERS, "X-CSRF-Token": "bad"},
    ) == 403
    assert code(
        client, "/api/v1/auth/logout", method="POST",
        headers={**HEADERS, "X-CSRF-Token": csrf},
    ) == 200
    assert code(client, "/api/v1/settings", headers=HEADERS) == 401


def test_operator_requires_origin_csrf_elevation_and_revokes_on_logout(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    csrf = login(client, "operator")
    cookie = client.cookies.get(COOKIE)
    baseline = client.get("/api/v1/settings", headers=HEADERS).json()
    payload = {"expected_version": 0, "settings": baseline["settings"]}
    privilege = {
        **HEADERS, "X-CSRF-Token": csrf, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }
    for headers in (
        HEADERS,
        {**HEADERS, "X-CSRF-Token": "x" * 64, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"},
        {**HEADERS, "X-CSRF-Token": csrf},
        {**privilege, "Origin": "https://evil.example"},
    ):
        assert code(
            client, "/api/v1/settings", method="PUT", headers=headers, payload=payload
        ) == 403
    assert code(
        client, "/api/v1/settings", method="PUT", headers=privilege, payload=payload
    ) == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 1
    assert code(
        client, "/api/v1/auth/logout", method="POST",
        headers={**HEADERS, "X-CSRF-Token": csrf},
    ) == 200
    assert cookie is not None
    old = {**privilege, "Cookie": f"{COOKIE}={cookie}"}
    assert code(client, "/api/v1/settings", headers=old) == 401
    assert code(client, "/api/v1/settings", method="PUT", headers=old, payload=payload) == 401


def test_five_failed_logins_block_even_correct_password(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    for _ in range(5):
        assert code(
            client, "/api/v1/auth/login", method="POST", headers=HEADERS,
            payload={"username": "viewer", "password": "incorrect"},
        ) == 401
    assert code(
        client, "/api/v1/auth/login", method="POST", headers=HEADERS,
        payload={"username": "viewer", "password": VIEW_PASS},
    ) == 429
    with Session(engine) as session:
        gate = session.get(StagingLoginGateRow, 1)
        assert gate is not None and gate.failures == 5 and gate.locked_until_ms > 0
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0


def test_stage_config_never_falls_back_to_legacy_admin() -> None:
    assert ApiConfig(database_url=None, admin_token=None).auth_mode == "local"
    with pytest.raises(ValueError):
        ApiConfig.from_env({
            "TRADING_MODE": "paper", "API_AUTH_MODE": "staging", "ADMIN_TOKEN": "a" * 64,
        })
    with pytest.raises(ValueError):
        ApiConfig.from_env({"TRADING_MODE": "paper", "API_AUTH_MODE": "untrusted"})
    with pytest.raises(ValueError):
        ApiConfig(
            database_url="sqlite://", admin_token=None, auth_mode="staging",
            staging_origin="http://app.staging.example", staging_host="api.staging.example",
            viewer_password_hash="bad", operator_password_hash="bad",
        )
