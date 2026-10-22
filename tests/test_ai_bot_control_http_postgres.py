"""BT-017: operator cookie/Origin/CSRF to durable PAPER-control CAS.

Full HTTP -> verified session -> PostgreSQL transaction -> audit, in an
isolated test DB only. No actual worker/order, no real money or owner PAPER.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.ai_paper_bot_controls_test import (
    initialize_disposable_bot_control,
    read_disposable_bot_control,
)
from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import (
    PaperBotControlEventRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_bot_control_test_api import EXPLICIT_TEST_APPROVAL
from binance_ai_trader.risk_schemas import RiskSettings
from binance_ai_trader.staging_auth import COOKIE, password_hash

URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("BOT_CONTROLS_HTTP_TEST_ISOLATED") == "1"
SAFE = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_bot_controls_http_test"
)
pytestmark = pytest.mark.skipif(
    not (SAFE and ISOLATED),
    reason="Dedicated disposable localhost trader_bot_controls_http_test only",
)
ORIGIN = "https://app.staging.example"
ROOT = "https://api.staging.example"
VIEW_PASS = "bt017-only-viewer-passphrase-2026"
OP_PASS = "bt017-only-operator-passphrase-2026"
URL_PATH = "/api/v1/paper/bot-control"
HEADERS = {"Origin": ORIGIN}
APPROVED = {
    "Origin": ORIGIN,
    "X-Paper-Bot-Control-Approval": EXPLICIT_TEST_APPROVAL,
}


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and SAFE and ISOLATED
    engine = create_engine(URL, pool_pre_ping=True)
    with Session(engine) as session:
        for model in (PaperBotControlEventRow, PaperSpotAccountRow, PaperSpotJournalRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield engine
    engine.dispose()


def config() -> ApiConfig:
    assert URL is not None
    return ApiConfig(
        database_url=URL,
        admin_token=None,
        auth_mode="staging",
        staging_origin=ORIGIN,
        staging_host="api.staging.example",
        viewer_password_hash=password_hash(VIEW_PASS),
        operator_password_hash=password_hash(OP_PASS),
    )


def change(
    *,
    expected: int,
    state: str,
    stop: str = "MONITOR_ONLY",
    confirmed: bool = False,
) -> dict[str, object]:
    return {
        "expected_revision": expected,
        "next_state": state,
        "stop_behavior": stop,
        "confirm_reduce_only_review": confirmed,
    }


def test_real_https_session_csrf_approval_and_cas_reboot(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=7,
                data=RiskSettings(
                    bot_enabled=True,
                    manual_approval=False,
                    spot_capital_usdt=1000,
                ).model_dump(mode="json"),
            )
        )
    initial = initialize_disposable_bot_control(engine)
    assert initial.state.value == "STOPPED" and initial.revision == 1
    cfg = config()
    # Even with staging configured, a disabled flag means NO endpoint at all.
    monkeypatch.delenv("BOT_CONTROLS_HTTP_TEST_ISOLATED")
    with TestClient(create_app(config=cfg, engine=engine), base_url=ROOT) as absent:
        assert absent.get(URL_PATH).status_code == 404
        assert absent.put(URL_PATH, json=change(expected=1, state="PAUSED")).status_code == 404
    monkeypatch.setenv("BOT_CONTROLS_HTTP_TEST_ISOLATED", "1")

    with TestClient(create_app(config=cfg, engine=engine), base_url=ROOT) as op, TestClient(
        create_app(config=cfg, engine=engine), base_url=ROOT
    ) as viewer:
        # Route auth is cookie-backed; a fabricated session hash is NOT accepted.
        assert op.get(URL_PATH, headers=HEADERS).status_code == 401
        assert op.put(
            URL_PATH, headers=APPROVED,
            json={**change(expected=1, state="PAUSED"), "actor_session_hash": "a" * 64},
        ).status_code == 422
        viewer_login = viewer.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username": "viewer", "password": VIEW_PASS},
        )
        assert viewer_login.status_code == 200
        viewer_csrf = viewer_login.json()["csrf_token"]
        assert viewer.get(URL_PATH, headers=HEADERS).status_code == 200
        assert viewer.put(
            URL_PATH, headers={**APPROVED, "X-CSRF-Token": viewer_csrf},
            json=change(expected=1, state="PAUSED"),
        ).status_code == 403

        login = op.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username": "operator", "password": OP_PASS},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        session_cookie = op.cookies.get(COOKIE)
        assert session_cookie and len(session_cookie) == 64
        base_headers = {**APPROVED, "X-CSRF-Token": csrf}
        with Session(engine) as session:
            live = session.scalar(
                select(StagingSessionRow).where(StagingSessionRow.role == "operator")
            )
            assert live is not None and live.token_hash != session_cookie
        denied = (
            ({"Origin": "https://evil.example", "X-CSRF-Token": csrf}, 403),
            ({"X-CSRF-Token": csrf}, 403),
            ({"Origin": ORIGIN}, 403),
            ({**APPROVED, "X-CSRF-Token": "f" * 64}, 403),
            ({**base_headers, "X-Paper-Bot-Control-Approval": "PAPER_SETTINGS_ONLY"}, 403),
            ({**base_headers, "X-Admin-Token": "z" * 64}, 403),
        )
        for headers, status in denied:
            r = op.put(URL_PATH, headers=headers, json=change(expected=1, state="PAUSED"))
            assert r.status_code == status, r.text
        assert read_disposable_bot_control(engine) == initial
        assert op.get(URL_PATH, headers=HEADERS).json()["state"] == "STOPPED"
        assert op.put(
            URL_PATH, headers=base_headers, json=change(expected=1, state="RUNNING")
        ).status_code == 409
        assert op.put(
            URL_PATH, headers=base_headers,
            json={**change(expected=1, state="PAUSED"), "surprise_field": 1},
        ).status_code == 422
        assert op.put(
            URL_PATH, headers=base_headers,
            json=change(expected=1, state="PAUSED", confirmed=True),
        ).status_code == 409
        assert op.get(URL_PATH, headers=HEADERS).json()["revision"] == 1

        paused = op.put(
            URL_PATH, headers=base_headers, json=change(expected=1, state="PAUSED")
        )
        assert paused.status_code == 200 and paused.json()["revision"] == 2
        assert op.put(
            URL_PATH, headers=base_headers, json=change(expected=1, state="STOPPED")
        ).status_code == 409
        review = op.put(
            URL_PATH, headers=base_headers,
            json=change(expected=2, state="PAUSED", stop="REVIEW_REDUCE_ONLY", confirmed=True),
        )
        assert review.status_code == 200 and review.json()["revision"] == 3
        assert review.json()["operator_confirmed_reduce_only"]
        assert review.json()["state"] == "PAUSED"
        assert op.put(
            URL_PATH, headers=base_headers,
            json=change(expected=3, state="RUNNING", stop="REVIEW_REDUCE_ONLY", confirmed=True),
        ).status_code == 200
        assert op.get(URL_PATH, headers=HEADERS).json()["state"] == "RUNNING"
        assert op.get(URL_PATH, headers=HEADERS).headers["cache-control"] == "no-store, private"

        # A second API process sees the same revision, without creating a new bot.
        assert URL is not None
        reopened = create_engine(URL, pool_pre_ping=True)
        try:
            with TestClient(create_app(config=cfg, engine=reopened), base_url=ROOT) as other:
                assert other.get(
                    URL_PATH,
                    headers={**HEADERS, "Cookie": f"{COOKIE}={session_cookie}"},
                ).json()["revision"] == 4
        finally:
            reopened.dispose()

        with Session(engine) as session:
            audits = list(session.scalars(select(PaperBotControlEventRow)))
            assert [a.revision for a in audits] == [2, 3, 4]
            assert all(a.actor_session_hash != live.token_hash for a in audits)
            assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
            assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert op.post(
            "/api/v1/auth/logout", headers={**HEADERS, "X-CSRF-Token": csrf}
        ).status_code == 200
        assert op.put(
            URL_PATH, headers=base_headers, json=change(expected=4, state="STOPPED")
        ).status_code == 401

        second = op.post(
            "/api/v1/auth/login", headers=HEADERS,
            json={"username": "operator", "password": OP_PASS},
        )
        assert second.status_code == 200
        csrf2 = second.json()["csrf_token"]
        with Session(engine) as session, session.begin():
            now_ms = time.time_ns() // 1_000_000
            session.execute(
                update(StagingSessionRow)
                .where(StagingSessionRow.role == "operator", StagingSessionRow.revoked.is_(False))
                .values(expires_at_ms=now_ms - 1)
            )
        assert op.put(
            URL_PATH, headers={**APPROVED, "X-CSRF-Token": csrf2},
            json=change(expected=4, state="STOPPED"),
        ).status_code == 401

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaperBotControlEventRow)) == 3
    assert read_disposable_bot_control(engine) is not None


def test_local_and_non_disposable_staging_do_not_register_bot_controls(engine: Engine) -> None:
    cfg = ApiConfig(database_url=URL, admin_token="z" * 48)
    with TestClient(create_app(config=cfg, engine=engine)) as client:
        assert client.get(URL_PATH, headers={"X-Admin-Token": "z" * 48}).status_code == 404
