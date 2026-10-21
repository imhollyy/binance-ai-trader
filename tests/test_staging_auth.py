"""HTTPS staging auth: isolation, session revocation, CSRF and RBAC."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import replace

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
    payload: Mapping[str, object] | None = None,
) -> int:
    return int(client.request(method, path, headers=headers, json=payload).status_code)


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
    assert (
        code(
            client,
            "/api/v1/auth/login",
            method="POST",
            payload=payload,
            headers={"Origin": "https://evil.example"},
        )
        == 403
    )
    csrf = login(client)
    assert len(csrf) == 64
    assert client.get("/api/v1/auth/session", headers=HEADERS).json()["role"] == "viewer"
    for path in (
        "/api/v1/status",
        "/api/v1/settings",
        "/api/v1/paper/dashboard",
        "/api/v1/paper/spot",
        "/api/v1/market/candles",
        "/api/v1/audit",
    ):
        assert code(client, path, headers=HEADERS) == 200
    assert code(client, "/api/v1/settings", headers={**HEADERS, "X-Admin-Token": "legacy"}) == 403
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
        **HEADERS,
        "X-CSRF-Token": csrf,
        "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }
    assert code(client, "/api/v1/settings", method="PUT", headers=HEADERS, payload=payload) == 403
    assert (
        code(client, "/api/v1/settings", method="PUT", headers=privileged, payload=payload) == 403
    )
    assert code(client, "/api/v1/auth/logout", method="POST", headers=HEADERS) == 403
    assert (
        code(
            client,
            "/api/v1/auth/logout",
            method="POST",
            headers={**HEADERS, "X-CSRF-Token": "bad"},
        )
        == 403
    )
    assert (
        code(
            client,
            "/api/v1/auth/logout",
            method="POST",
            headers={**HEADERS, "X-CSRF-Token": csrf},
        )
        == 200
    )
    assert code(client, "/api/v1/settings", headers=HEADERS) == 401


def test_expired_session_logout_clears_stale_cookie_without_weakening_csrf(
    staging: tuple[TestClient, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = staging
    csrf = login(client)
    old_cookie = client.cookies.get(COOKIE)
    assert old_cookie is not None
    with Session(engine) as session:
        row = session.scalar(select(StagingSessionRow))
        assert row is not None
        expired_after = row.expires_at_ms + 1

    # Simulate an elapsed 15-minute staging session without waiting.
    monkeypatch.setattr("binance_ai_trader.staging_auth._now_ms", lambda: expired_after)
    assert code(client, "/api/v1/auth/session", headers=HEADERS) == 401
    invalid_origin = client.post(
        "/api/v1/auth/logout",
        headers={**HEADERS, "Origin": "https://evil.example", "X-CSRF-Token": csrf},
    )
    assert invalid_origin.status_code == 403
    assert client.cookies.get(COOKIE) == old_cookie

    result = client.post("/api/v1/auth/logout", headers={**HEADERS, "X-CSRF-Token": csrf})
    assert result.status_code == 200
    assert result.json() == {"status": "signed_out"}
    assert "max-age=0" in result.headers["set-cookie"].lower()
    assert client.cookies.get(COOKIE) is None
    assert code(client, "/api/v1/auth/session", headers=HEADERS) == 401

    # Already signed out is safe/idempotent, but missing or wrong Origin
    # remains forbidden even without a valid cookie.
    assert code(client, "/api/v1/auth/logout", method="POST", headers=HEADERS) == 200
    assert code(client, "/api/v1/auth/logout", method="POST", headers={}) == 403


def test_operator_requires_origin_csrf_elevation_and_revokes_on_logout(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    csrf = login(client, "operator")
    cookie = client.cookies.get(COOKIE)
    baseline = client.get("/api/v1/settings", headers=HEADERS).json()
    payload = {"expected_version": 0, "settings": baseline["settings"]}
    privilege = {
        **HEADERS,
        "X-CSRF-Token": csrf,
        "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
    }
    for headers in (
        HEADERS,
        {**HEADERS, "X-CSRF-Token": "x" * 64, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"},
        {**HEADERS, "X-CSRF-Token": csrf},
        {**privilege, "Origin": "https://evil.example"},
    ):
        assert (
            code(client, "/api/v1/settings", method="PUT", headers=headers, payload=payload) == 403
        )
    assert code(client, "/api/v1/settings", method="PUT", headers=privilege, payload=payload) == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TradingSettingsRow)) == 1
    assert (
        code(
            client,
            "/api/v1/auth/logout",
            method="POST",
            headers={**HEADERS, "X-CSRF-Token": csrf},
        )
        == 200
    )
    assert cookie is not None
    old = {**privilege, "Cookie": f"{COOKIE}={cookie}"}
    assert code(client, "/api/v1/settings", headers=old) == 401
    assert code(client, "/api/v1/settings", method="PUT", headers=old, payload=payload) == 401


def test_five_failed_logins_block_even_correct_password(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    for _ in range(5):
        assert (
            code(
                client,
                "/api/v1/auth/login",
                method="POST",
                headers=HEADERS,
                payload={"username": "viewer", "password": "incorrect"},
            )
            == 401
        )
    assert (
        code(
            client,
            "/api/v1/auth/login",
            method="POST",
            headers=HEADERS,
            payload={"username": "viewer", "password": VIEW_PASS},
        )
        == 429
    )
    with Session(engine) as session:
        gate = session.get(StagingLoginGateRow, 1)
        assert gate is not None and gate.failures == 5 and gate.locked_until_ms > 0
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0


def test_stage_config_never_falls_back_to_legacy_admin() -> None:
    assert ApiConfig(database_url=None, admin_token=None).auth_mode == "local"
    with pytest.raises(ValueError):
        ApiConfig.from_env(
            {
                "TRADING_MODE": "paper",
                "API_AUTH_MODE": "staging",
                "ADMIN_TOKEN": "a" * 64,
            }
        )
    with pytest.raises(ValueError):
        ApiConfig.from_env({"TRADING_MODE": "paper", "API_AUTH_MODE": "untrusted"})
    with pytest.raises(ValueError):
        ApiConfig(
            database_url="sqlite://",
            admin_token=None,
            auth_mode="staging",
            staging_origin="http://app.staging.example",
            staging_host="api.staging.example",
            viewer_password_hash="bad",
            operator_password_hash="bad",
        )


@pytest.mark.parametrize(
    "bad_host",
    (
        "*.staging.example",
        "api..staging.example",
        "api.staging.example.",
        "-api.staging.example",
        "api-.staging.example",
        "bad_host.staging.example",
        "API.staging.example",
        "api staging.example",
        "192.0.2.12",
        "a" * 64 + ".staging.example",
    ),
)
def test_staging_rejects_non_exact_api_dns_names(bad_host: str) -> None:
    good = config()
    with pytest.raises(ValueError, match="Staging origin and API host"):
        replace(good, staging_host=bad_host)


@pytest.mark.parametrize(
    "bad_origin",
    (
        "https://*.staging.example",
        "https://app..staging.example",
        "https://app.staging.example.",
        "https://bad_host.staging.example",
        "https://APP.staging.example",
        "https://büro.staging.example",
        "https://192.0.2.12",
    ),
)
def test_staging_rejects_non_exact_origin_dns_names(bad_origin: str) -> None:
    good = config()
    with pytest.raises(ValueError, match="Staging origin and API host"):
        replace(good, staging_origin=bad_origin)


def test_staging_supports_a_single_strict_https_hostname() -> None:
    good = config()
    same_host = replace(good, staging_host="app.staging.example")
    assert same_host.staging_origin == "https://app.staging.example"


def test_client_forwarded_https_headers_cannot_upgrade_http_scheme(
    staging: tuple[TestClient, Engine],
) -> None:
    valid_https_client, engine = staging
    spoof_headers = {
        **HEADERS,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "api.staging.example",
        "Forwarded": "proto=https;host=api.staging.example",
    }
    with TestClient(valid_https_client.app, base_url="http://api.staging.example") as client:
        assert (
            code(
                client,
                "/api/v1/auth/login",
                method="POST",
                headers=spoof_headers,
                payload={"username": "viewer", "password": VIEW_PASS},
            )
            == 403
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0


def test_staging_rotation_invalidates_existing_role_cookie(
    staging: tuple[TestClient, Engine],
) -> None:
    client, engine = staging
    _ = login(client, "viewer")
    old_viewer = client.cookies.get(COOKIE)
    assert old_viewer is not None
    client.cookies.clear()

    _ = login(client, "operator")
    old_operator = client.cookies.get(COOKIE)
    assert old_operator is not None
    client.cookies.clear()

    rotated_secret = "synthetic-rotated-viewer-passphrase-2026"
    rotated = replace(
        config(),
        viewer_password_hash=password_hash(rotated_secret, salt=b"rotated-viewer-1"),
    )
    # A freshly configured API instance references the SAME persisted DB.
    with TestClient(create_app(config=rotated, engine=engine), base_url=BASE) as new_instance:
        viewer_cookie = {"Cookie": f"{COOKIE}={old_viewer}"}
        operator_cookie = {"Cookie": f"{COOKIE}={old_operator}"}
        assert (
            new_instance.get(
                "/api/v1/auth/session", headers={**HEADERS, **viewer_cookie}
            ).status_code
            == 401
        )
        assert (
            new_instance.get(
                "/api/v1/auth/session", headers={**HEADERS, **operator_cookie}
            ).status_code
            == 200
        )
        assert (
            new_instance.post(
                "/api/v1/auth/login",
                headers=HEADERS,
                json={"username": "viewer", "password": rotated_secret},
            ).status_code
            == 200
        )
        assert new_instance.get("/api/v1/auth/session", headers=HEADERS).status_code == 200

    # Invalidated rows remain as audit/revocation evidence rather than being
    # automatically deleted; expired session cleanup is a separate task.
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 3


def test_staging_safe_log_and_no_store_on_success_denial_and_validation(
    staging: tuple[TestClient, Engine], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = staging
    password_marker = "synthetic-secret-sentinel-" + "z" * 300
    query_marker = "QUERY-SENTINEL-NEVER-LOG"
    fake_cookie = "COOKIE-SENTINEL-NEVER-LOG"
    fake_csrf = "CSRF-SENTINEL-NEVER-LOG"

    with caplog.at_level(logging.INFO, logger="binance_ai_trader.staging_events"):
        denied = client.get(
            "/api/v1/settings?debug=" + query_marker,
            headers={**HEADERS, "Cookie": f"{COOKIE}={fake_cookie}", "X-CSRF-Token": fake_csrf},
        )
        validation = client.post(
            "/api/v1/auth/login",
            headers=HEADERS,
            json={"username": "viewer", "password": password_marker},
        )
        wrong_origin = client.get("/api/v1/status", headers={"Origin": "https://untrusted.example"})
        unknown = client.get("/anything-" + query_marker + "?q=" + fake_cookie)
        csrf = login(client)
        signed_in = client.get("/api/v1/auth/session", headers=HEADERS)

    assert (denied.status_code, validation.status_code, wrong_origin.status_code) == (401, 422, 403)
    assert unknown.status_code == 404
    assert signed_in.status_code == 200
    assert len(csrf) == 64
    for response in (denied, validation, wrong_origin, unknown, signed_in):
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert validation.json() == {"detail": "Invalid request"}

    safe_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "binance_ai_trader.staging_events"
    )
    assert "staging_http route=login method=POST status=4xx" in safe_logs
    assert "staging_http route=settings method=GET status=4xx" in safe_logs
    assert "staging_http route=other method=GET status=4xx" in safe_logs
    assert "staging_http route=session method=GET status=2xx" in safe_logs
    for secret in (password_marker, query_marker, fake_cookie, fake_csrf, csrf):
        assert secret not in safe_logs
    assert password_marker not in validation.text
