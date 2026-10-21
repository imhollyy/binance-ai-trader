"""Disposable PostgreSQL-only HTTPS session restart/expiry/revocation contract."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    StagingLoginGateRow,
    StagingSessionRow,
)
from binance_ai_trader.staging_auth import COOKIE, SESSION_LIFETIME_MS, password_hash
import binance_ai_trader.staging_auth as staging_auth

DB_URL = os.environ.get("TEST_POSTGRES_URL")
SAFE = bool(
    DB_URL
    and make_url(DB_URL).get_backend_name() == "postgresql"
    and make_url(DB_URL).host in ("127.0.0.1", "localhost")
    and make_url(DB_URL).database == "trader_auth_test"
)
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
pytestmark = pytest.mark.skipif(
    not SAFE or not ISOLATED,
    reason="Only migrated, empty, disposable localhost trader_auth_test",
)
ORIGIN = "https://app.staging.example"
ROOT = "https://api.staging.example"
HEADERS = {"Origin": ORIGIN}
PASS = "synthetic-only-auth-postgres-passphrase-2026"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert SAFE and ISOLATED and DB_URL is not None
    db = create_engine(DB_URL, pool_pre_ping=True)
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        gate = session.get(StagingLoginGateRow, 1)
        assert gate is not None
        assert gate.failures == 0 and gate.locked_until_ms == 0
    yield db
    db.dispose()


def _config() -> ApiConfig:
    assert DB_URL is not None
    return ApiConfig(
        database_url=DB_URL,
        admin_token=None,
        auth_mode="staging",
        staging_origin=ORIGIN,
        staging_host="api.staging.example",
        viewer_password_hash=password_hash(PASS, salt=b"view-auth-pg-tst1"),
        operator_password_hash=password_hash(PASS + "-different", salt=b"oper-auth-pg-tst1"),
    )


def test_stage_session_survives_restart_but_not_logout_or_expiry(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    with TestClient(create_app(config=config, engine=engine), base_url=ROOT) as client:
        result = client.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username":"viewer","password":PASS},
        )
        assert result.status_code == 200
        csrf = result.json()["csrf_token"]
        raw = client.cookies.get(COOKIE)
        assert raw is not None and len(raw) == 64
        with Session(engine) as session:
            row = session.scalar(select(StagingSessionRow))
            assert row is not None and row.token_hash != raw
            assert row.csrf_hash != csrf
            created = row.issued_at_ms
            assert row.expires_at_ms == created + SESSION_LIFETIME_MS

        # Separate Engine simulates API process restart using same migrated PG.
        assert DB_URL is not None
        reopened = create_engine(DB_URL, pool_pre_ping=True)
        try:
            with TestClient(create_app(config=config, engine=reopened), base_url=ROOT) as other:
                cookie = {"Cookie": f"{COOKIE}={raw}"}
                assert other.get(
                    "/api/v1/paper/dashboard", headers={**HEADERS, **cookie}
                ).status_code == 200
                assert client.post(
                    "/api/v1/auth/logout", headers={**HEADERS, "X-CSRF-Token":csrf}
                ).status_code == 200
                assert other.get(
                    "/api/v1/paper/dashboard", headers={**HEADERS, **cookie}
                ).status_code == 401
        finally:
            reopened.dispose()

        # New session expires at absolute UTC expiry; not extended by requests.
        logged = client.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username":"viewer","password":PASS},
        )
        assert logged.status_code == 200
        cookie2 = client.cookies.get(COOKIE)
        assert cookie2 is not None
        monkeypatch.setattr(staging_auth, "_now_ms", lambda: created + SESSION_LIFETIME_MS + 60_000)
        assert client.get("/api/v1/settings",headers=HEADERS).status_code == 401

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StagingSessionRow)) == 2
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
