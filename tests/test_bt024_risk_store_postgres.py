# ruff: noqa: E501
from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_shadow_preflight import ShadowPreflightResult
from binance_ai_trader.ai_trainable_model import ModelAction, ResearchPrediction
from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.paper_supervisor import claim_worker, release_worker
from binance_ai_trader.risk_guard import RiskProfile, RiskRequest
from binance_ai_trader.risk_store import (
    RiskStoreRejected,
    evaluate_and_reserve,
    install_profile,
    record_account_snapshot,
    record_shadow_preflight,
    set_bot_state,
)

URL = os.environ.get("DATABASE_URL", "")
ISOLATED = (
    os.environ.get("TRADEPILOT_BT023_TEST_DB") == "1"
    and bool(URL)
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).port == 55444
    and make_url(URL).database == "trader_ai_bt023_test"
)
pytestmark = pytest.mark.skipif(
    not ISOLATED, reason="requires isolated PostgreSQL on localhost:55444"
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    db = create_engine(URL, pool_pre_ping=True)
    yield db
    db.dispose()


def _profile(version: int = 1) -> RiskProfile:
    return RiskProfile(
        "1.0",
        version,
        1,
        1,
        Decimal("1"),
        Decimal("300"),
        Decimal("300"),
        Decimal("3"),
        Decimal("10"),
        3,
        (("user_cap", Decimal("300")),),
    )


def _request(signal: str, *, settings_version: int = 1) -> RiskRequest:
    return RiskRequest(
        settings_version,
        BotState.RUNNING,
        Decimal("200"),
        Decimal("99"),
        Decimal("102"),
        Decimal("100"),
        Decimal("5"),
        Decimal("1"),
        Decimal("1"),
        Decimal("20"),
        Decimal("5"),
        Decimal("0"),
        0,
        Decimal("1000"),
        Decimal("1000"),
        Decimal("1000"),
        0,
    )


def test_confirmed_profile_atomic_concurrency_audit_and_restart(engine: Engine) -> None:
    with pytest.raises(RiskStoreRejected, match="initial_profile_must_start_stopped"):
        install_profile(
            engine,
            _profile(),
            user_confirmed=True,
            expected_runtime_revision=0,
            state=BotState.RUNNING,
        )
    assert (
        install_profile(engine, _profile(), user_confirmed=True, expected_runtime_revision=0) == 1
    )
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT bot_state FROM ai_risk_runtime WHERE id=1")).scalar_one()
            == "STOPPED"
        )
    with pytest.raises(RiskStoreRejected, match="invalid_or_stale_bot_transition"):
        set_bot_state(engine, state=BotState.RUNNING, expected_revision=1)
    assert set_bot_state(engine, state=BotState.PAUSED, expected_revision=1) == 2
    assert set_bot_state(engine, state=BotState.RUNNING, expected_revision=2) == 3
    record_account_snapshot(
        engine,
        snapshot_id="snapshot_0001",
        equity=Decimal("1000"),
        day_start_equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
        open_capital=Decimal(0),
        open_positions=0,
        consecutive_losses=0,
        source_sha256="c" * 64,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(
            pool.map(
                lambda n: evaluate_and_reserve(
                    engine,
                    request_id=f"risk_request_{n:04d}",
                    signal_id=f"risk_signal_{n:04d}",
                    profile_version=1,
                    request=_request(f"risk_signal_{n:04d}"),
                ),
                (1, 2),
            )
        )
    assert sum(decision.allowed for decision in decisions) == 1
    stale = evaluate_and_reserve(
        engine,
        request_id="risk_stale_request_01",
        signal_id="risk_stale_signal_01",
        profile_version=1,
        request=_request("risk_stale_signal_01", settings_version=0),
    )
    assert not stale.allowed
    assert stale.reason.value == "STALE_CONFIGURATION"
    with engine.connect() as conn:
        persisted_request_version: str = conn.execute(
            text(
                "SELECT decision_json->'request'->>'settings_version' FROM ai_risk_audit WHERE event_id=:id"
            ),
            {"id": "risk_stale_request_01"},
        ).scalar_one()
        assert persisted_request_version == "0"
    expected_states = [
        BotState.PAUSED,
        BotState.EMERGENCY_FLATTEN,
        BotState.STOPPED,
        BotState.PAUSED,
        BotState.STOPPED,
        BotState.PAUSED,
        BotState.RUNNING,
        BotState.STOPPED,
        BotState.PAUSED,
        BotState.RUNNING,
        BotState.EMERGENCY_FLATTEN,
        BotState.STOPPED,
    ]
    revision = 3
    for next_state in expected_states:
        revision = set_bot_state(engine, state=next_state, expected_revision=revision)
    assert revision == 15
    assert (
        install_profile(engine, _profile(2), user_confirmed=True, expected_runtime_revision=15)
        == 16
    )
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT bot_state FROM ai_risk_runtime WHERE id=1")).scalar_one()
            == "STOPPED"
        )
        transitions = conn.execute(
            text(
                "SELECT bot_revision, decision_json->>'state' FROM ai_risk_audit WHERE event_id LIKE 'botstate_%' ORDER BY bot_revision"
            )
        ).all()
        runtime_revision: int = conn.execute(
            text("SELECT revision FROM ai_risk_runtime WHERE id=1")
        ).scalar_one()
        expected_audit_states = [BotState.PAUSED, BotState.RUNNING, *expected_states]
        assert transitions == [
            (index + 2, state.value) for index, state in enumerate(expected_audit_states)
        ]
        assert runtime_revision == 16
    model_prediction = ResearchPrediction(
        ModelAction.BUY,
        "shadow_db_signal_001",
        "model-v1",
        "1.0",
        "BTCUSDT",
        "1m",
        60_000,
        960_000,
        None,
        False,
        "UNCALIBRATED_RESEARCH_ONLY",
        "d" * 64,
        "e" * 64,
    )
    record_shadow_preflight(
        engine,
        event_id="shadow_audit_0001",
        prediction=model_prediction,
        result=ShadowPreflightResult(
            "BLOCKED",
            "MODEL_CALIBRATION_UNVERIFIED",
            model_prediction.signal_id,
            decisions[0],
            False,
        ),
    )
    restarted = create_engine(URL, pool_pre_ping=True)
    try:
        denied = evaluate_and_reserve(
            restarted,
            request_id="risk_after_restart_0001",
            signal_id="risk_after_restart_s1",
            profile_version=1,
            request=_request("after"),
        )
        assert not denied.allowed
        with restarted.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM ai_risk_audit")).scalar_one() == 21
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events WHERE event_type='RESERVE'"
                    )
                ).scalar_one()
                == 1
            )
    finally:
        restarted.dispose()
    with pytest.raises(RiskStoreRejected):
        install_profile(engine, _profile(3), user_confirmed=False, expected_runtime_revision=16)


def test_user_confirmed_profile_api_persists_version_and_rejects_stale_revision(
    engine: Engine,
) -> None:
    token = "r" * 40
    app = create_app(
        config=ApiConfig(database_url=None, admin_token=token), engine=engine, risk_engine=engine
    )
    payload = {
        "expected_revision": 16,
        "risk_level": 5,
        "max_positions": 1,
        "risk_per_trade_pct": "0.5",
        "max_open_capital": "250",
        "spot_budget": "300",
        "daily_loss_pct": "1",
        "max_drawdown_pct": "5",
        "max_consecutive_losses": 2,
        "user_hard_limits": {"manual_cap": "250"},
        "user_confirmed": True,
    }
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/risk/profiles")
        before = client.get("/api/v1/risk/profiles", headers={"X-Admin-Token": token})
        no_approval = client.put(
            "/api/v1/risk/profiles", json=payload, headers={"X-Admin-Token": token}
        )
        stale = client.put(
            "/api/v1/risk/profiles",
            json={**payload, "expected_revision": 15},
            headers={
                "X-Admin-Token": token,
                "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
            },
        )
        active_worker = str(uuid4())
        claim_worker(engine, active_worker)
        worker_running = client.put(
            "/api/v1/risk/profiles",
            json=payload,
            headers={
                "X-Admin-Token": token,
                "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
            },
        )
        release_worker(engine, active_worker)
        updated = client.put(
            "/api/v1/risk/profiles",
            json=payload,
            headers={
                "X-Admin-Token": token,
                "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
            },
        )
        concurrent_payload = {**payload, "expected_revision": 17, "risk_level": 4}
        concurrent_headers = {
            "X-Admin-Token": token,
            "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
        }
        barrier = Barrier(2)

        def concurrent_update() -> int:
            barrier.wait()
            return int(
                client.put(
                    "/api/v1/risk/profiles",
                    json=concurrent_payload,
                    headers=concurrent_headers,
                ).status_code
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_statuses = list(pool.map(lambda _index: concurrent_update(), range(2)))
    assert unauthorized.status_code == 401
    assert before.json()["active_profile_version"] == 2
    assert before.json()["bot_state"] == "STOPPED"
    assert no_approval.status_code == 403
    assert stale.status_code == 409
    assert worker_running.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["active_risk_level"] == 5
    assert updated.json()["active_profile_version"] == 3
    assert updated.json()["bot_state"] == "STOPPED"
    assert sorted(concurrent_statuses) == [200, 409]
    with engine.connect() as conn:
        audit: str = conn.execute(
            text("SELECT decision_json->>'action' FROM ai_risk_audit WHERE event_id='profile_5_3'")
        ).scalar_one()
        assert audit == "PROFILE_INSTALLED"
        actor: str = conn.execute(
            text(
                "SELECT decision_json->>'actor_sha256' FROM ai_risk_audit "
                "WHERE event_id='profile_5_3'"
            )
        ).scalar_one()
        assert actor != token and len(actor) == 64


def test_append_only_risk_tables_reject_update_delete_and_truncate(engine: Engine) -> None:
    tables = (
        "ai_risk_profiles",
        "ai_risk_reservation_events",
        "ai_risk_account_snapshots",
        "ai_risk_audit",
    )
    profile_json = '{"schema_version":"1.0","user_hard_limits":[]}'
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_risk_profiles(risk_level,profile_version,profile_json,user_confirmed) VALUES(5,99,CAST(:data AS json),true)"
            ),
            {"data": profile_json},
        )
        conn.execute(
            text(
                "INSERT INTO ai_risk_reservation_events(event_id,reservation_id,event_type,amount_usdt,profile_version) VALUES('append_only_res_01','append_only_reservation_01','RESERVE',1,99)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ai_risk_account_snapshots(snapshot_id,equity_usdt,day_start_equity_usdt,peak_equity_usdt,open_capital_usdt,open_positions,consecutive_losses,source_sha256) VALUES('append_only_snapshot_01',1,1,1,0,0,0,:hash)"
            ),
            {"hash": "f" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO ai_risk_audit(event_id,signal_id,profile_version,bot_revision,decision_json) VALUES('append_only_audit_01','append_only_signal_01',99,0,'{}')"
            )
        )
    # Each destructive statement runs in its own transaction so the expected trigger
    # exception is isolated and the inserted evidence remains available for other cases.
    for table in tables:
        for statement in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}", f"TRUNCATE {table}"):
            with pytest.raises(DBAPIError, match="append only"), engine.begin() as conn:
                conn.execute(text(statement))


def test_migration_downgrade_preserves_append_only_history(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_risk_audit(event_id,signal_id,profile_version,bot_revision,decision_json) "
                "VALUES('downgrade_audit_01','downgrade_signal_01',1,1,'{}')"
            )
        )

    with pytest.raises(RuntimeError, match="worker supervisor history must not be dropped"):
        command.downgrade(Config("alembic.ini"), "0015_bt024_risk_store")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0018_bt027_worker_supervisor"
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM ai_risk_audit WHERE event_id='downgrade_audit_01'")
            ).scalar_one()
            == 1
        )
        guards: int = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
                "('bt023_prediction_no_truncate','bt023_outcome_no_truncate',"
                "'bt024_profile_no_truncate','bt024_reservation_no_truncate',"
                "'bt024_snapshot_no_truncate','bt024_audit_no_truncate',"
                "'bt027_worker_events_no_truncate')"
            )
        ).scalar_one()
        assert guards == 7
