# ruff: noqa: E501
from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_shadow_preflight import ShadowPreflightResult
from binance_ai_trader.ai_trainable_model import ModelAction, ResearchPrediction
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
    not ISOLATED, reason="requires ephemeral tmpfs PostgreSQL on localhost:55444"
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    db = create_engine(URL, pool_pre_ping=True)
    yield db
    db.dispose()


def _profile() -> RiskProfile:
    return RiskProfile(
        "1.0",
        1,
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


def _request(signal: str) -> RiskRequest:
    return RiskRequest(
        1,
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
    assert (
        install_profile(engine, _profile(), user_confirmed=True, expected_runtime_revision=0) == 1
    )
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
            assert conn.execute(text("SELECT count(*) FROM ai_risk_audit")).scalar_one() == 6
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
        install_profile(engine, _profile(), user_confirmed=False, expected_runtime_revision=3)
