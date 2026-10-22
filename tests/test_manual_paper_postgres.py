"""Disposable PostgreSQL manual PAPER quote, replay and position settlement.

Never run against a user's database or the real Binance API. Only isolated
localhost trader_manual_test is eligible. All public HTTP is mocked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from binance_ai_trader.manual_paper_execution import (
    ManualPaperRequest,
    run_manual_paper_once,
)
from binance_ai_trader.market_data import MarketDataError
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
URL = os.environ.get("TEST_POSTGRES_URL")
SAFE = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_manual_test"
)
pytestmark = pytest.mark.skipif(
    not SAFE or os.environ.get("PAPER_LEDGER_TEST_ISOLATED") != "1",
    reason="Only a fresh migrated disposable localhost trader_manual_test DB",
)
BUY_ID = UUID("00000000-0000-4000-8000-000000000011")
SELL_ID = UUID("00000000-0000-4000-8000-000000000012")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and SAFE
    db = create_engine(URL, pool_pre_ping=True)
    with Session(db) as session:
        for model in (TradingSettingsRow, PaperSpotAccountRow, PaperSpotJournalRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield db
    db.dispose()


def _mock_observer(
    *, now: int, ticker: str = "100", status: int = 200
) -> PublicSpotObservationClient:
    start = now // 60_000 * 60_000
    klines = [
        [
            start - (60 - i) * 60_000,
            "100",
            "101",
            "99",
            "100",
            "4",
            start - (60 - i) * 60_000 + 59_999,
            "0",
            1,
            "0",
            "0",
            "0",
        ]
        for i in range(60)
    ]

    def reply(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "data-api.binance.vision"
        assert req.method == "GET"
        assert "X-MBX-APIKEY" not in req.headers
        if req.url.path == "/api/v3/klines":
            return httpx.Response(200, json=klines)
        assert req.url.path == "/api/v3/ticker/price"
        return httpx.Response(
            status,
            json={"symbol": req.url.params["symbol"], "price": ticker},
            headers={"Retry-After": "30"},
        )

    return PublicSpotObservationClient(transport=httpx.MockTransport(reply), clock_ms=lambda: now)


def _request(
    action: str = "buy",
    request_id: UUID = BUY_ID,
    *,
    budget: str | None = "100",
    stop: str | None = "2",
) -> ManualPaperRequest:
    return ManualPaperRequest.model_validate(
        {
            "action": action,
            "symbol": "BTCUSDT",
            "request_id": str(request_id),
            "confirmation": "CONFIRM_MANUAL_PAPER_ONLY",
            "budget_usdt": budget,
            "stop_distance_pct": stop,
        }
    )


def _totals(db: Engine) -> tuple[int, int, int, D]:
    with Session(db) as s:
        account = s.get(PaperSpotAccountRow, 1)
        assert account is not None
        return (
            s.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            s.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            s.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
            account.cash_usdt,
        )


def test_manual_paper_account_consent_replay_quote_audit_and_close(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings().model_dump(mode="json"),
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    assert _totals(engine) == (0, 1, 0, D("1000"))

    # A non-price request failure, fake/outlier or Binance public 429 must not
    # create a position, journal row or a misleading quote audit.
    now = 1_800_000_000_200
    for price, status in (("200", 200), ("100", 429)):
        with pytest.raises((MarketDataError, ValueError)):
            run_manual_paper_once(
                engine, _request(), observer=_mock_observer(now=now, ticker=price, status=status)
            )
    assert _totals(engine) == (0, 1, 0, D("1000"))

    observer = _mock_observer(now=now)
    original = run_manual_paper_once(engine, _request(), observer=observer)
    assert original.receipt.event_type == "spot_buy"
    assert original.receipt.cash_after_usdt >= D("900")
    assert original.receipt.cash_after_usdt < D("901")
    assert original.reference_price == D("100")
    assert _totals(engine)[:3] == (1, 2, 1)
    with Session(engine) as session:
        stored = session.scalar(select(PaperSpotPositionRow))
        assert stored is not None and stored.status == "open"
        assert stored.quantity > 0 and stored.stop_price == D("98")
        assert session.scalar(select(PaperSpotObservedQuoteRow)) is not None

    # A restarted SQLAlchemy engine replays the exact persisted receipt without
    # fetching a new market sample (observer omitted intentionally).
    assert URL is not None
    reopened = create_engine(URL, pool_pre_ping=True)
    try:
        replay = run_manual_paper_once(reopened, _request())
        assert replay == original
    finally:
        reopened.dispose()
    assert _totals(engine)[:3] == (1, 2, 1)

    with pytest.raises(PaperExecutionRejected, match="idempotency_conflict"):
        run_manual_paper_once(engine, _request(budget="50"), observer=observer)
    with pytest.raises(PaperExecutionRejected, match="manual_position_already_open"):
        run_manual_paper_once(
            engine,
            _request(request_id=UUID("00000000-0000-4000-8000-000000000013")),
            observer=observer,
        )
    assert _totals(engine)[:3] == (1, 2, 1)

    after = _mock_observer(now=now + 60_000, ticker="100")
    closed = run_manual_paper_once(
        engine, _request("sell", SELL_ID, budget=None, stop=None), observer=after
    )
    assert closed.receipt.event_type == "spot_sell"
    assert closed.receipt.realized_pnl_delta_usdt < 0
    assert _totals(engine)[:3] == (1, 3, 2)
    assert (
        run_manual_paper_once(engine, _request("sell", SELL_ID, budget=None, stop=None)) == closed
    )
    assert _totals(engine)[:3] == (1, 3, 2)
    with Session(engine) as session:
        pos = session.scalar(select(PaperSpotPositionRow))
        assert pos is not None and pos.status == "closed"
        row = session.get(TradingSettingsRow, 1)
        assert row is not None
        settings = RiskSettings.model_validate(row.data)
        assert settings.bot_enabled is False


def test_preview_risk_change_ledger_consistency_and_subsequent_paper_trades(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh staging roles and PAPER journal must survive approved risk edit."""
    from fastapi.testclient import TestClient

    from binance_ai_trader.api import create_app
    from binance_ai_trader.api_config import ApiConfig
    from binance_ai_trader.models import AuditRow
    from binance_ai_trader.staging_auth import password_hash

    # Only a disposable migrated localhost trader_manual_test DB is accepted
    # by this module's fixture; NEVER point this test at an owner's preview DB.
    assert SAFE and URL is not None
    monkeypatch.setenv("PRIVATE_PREVIEW_OPT_IN", "isolated-paper-only")
    origin = "https://preview.staging.example"
    view_password = "synthetic-viewer-password-for-bt010b"
    operator_password = "synthetic-operator-password-for-bt010b"
    app = create_app(
        config=ApiConfig(
            database_url=URL,
            admin_token=None,
            auth_mode="staging",
            staging_origin=origin,
            staging_host="preview.staging.example",
            viewer_password_hash=password_hash(view_password, salt=b"paper-view-010bx"),
            operator_password_hash=password_hash(operator_password, salt=b"paper-oper-010bx"),
        ),
        engine=engine,
    )
    before = _totals(engine)
    assert before[:3] == (1, 3, 2)
    with Session(engine) as db:
        account = db.get(PaperSpotAccountRow, 1)
        assert account is not None and account.settings_version == 1

    with (
        TestClient(app, base_url=origin) as viewer,
        TestClient(app, base_url=origin) as operator,
    ):
        headers = {"Origin": origin}
        viewer_login = viewer.post(
            "/api/v1/auth/login",
            headers=headers,
            json={"username": "viewer", "password": view_password},
        )
        assert viewer_login.status_code == 200
        view_csrf = viewer_login.json()["csrf_token"]
        oper_login = operator.post(
            "/api/v1/auth/login",
            headers=headers,
            json={"username": "operator", "password": operator_password},
        )
        assert oper_login.status_code == 200
        oper_csrf = oper_login.json()["csrf_token"]
        approval = {
            **headers,
            "X-CSRF-Token": oper_csrf,
            "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
        }
        view_approval = {**approval, "X-CSRF-Token": view_csrf}
        settings_snapshot = operator.get("/api/v1/settings", headers=headers).json()
        assert settings_snapshot["version"] == 1
        data = settings_snapshot["settings"]
        desired = {**data, "risk_per_trade_pct": 0.35}
        request = {"expected_version": 1, "settings": desired}

        assert (
            viewer.put("/api/v1/settings", headers=view_approval, json=request).status_code == 403
        )
        assert (
            operator.put(
                "/api/v1/settings",
                headers={**approval, "Origin": "https://evil.example"},
                json=request,
            ).status_code
            == 403
        )
        assert operator.put("/api/v1/settings", headers=headers, json=request).status_code == 403
        assert (
            operator.put(
                "/api/v1/settings",
                headers=approval,
                json={**request, "settings": {**desired, "bot_enabled": True}},
            ).status_code
            == 409
        )
        assert (
            operator.put(
                "/api/v1/settings",
                headers=approval,
                json={**request, "settings": {**desired, "spot_capital_usdt": 2000}},
            ).status_code
            == 409
        )
        assert _totals(engine) == before

        saved = operator.put("/api/v1/settings", headers=approval, json=request)
        assert saved.status_code == 200
        assert saved.json()["version"] == 2
        assert saved.json()["settings"]["bot_enabled"] is False
        assert operator.put("/api/v1/settings", headers=approval, json=request).status_code == 409

        with Session(engine) as db:
            account = db.get(PaperSpotAccountRow, 1)
            assert account is not None and account.settings_version == 2
            assert account.cash_usdt == before[3]
            assert db.scalar(select(func.count()).select_from(AuditRow)) == 1
        assert _totals(engine) == before

        # The next real PAPER request against a fresh mocked public quote works,
        # rather than failing because settings version drifted from the ledger.
        now = 1_800_000_600_200
        newer = _request(request_id=UUID("00000000-0000-4000-8000-000000000014"), budget="50")
        created = run_manual_paper_once(engine, newer, observer=_mock_observer(now=now))
        assert created.receipt.event_type == "spot_buy"
        assert created.receipt.cash_after_usdt < before[3]
        assert _totals(engine)[:3] == (2, 4, 3)

        # Do not silently change risk while an open PAPER position exists.
        proposed = {**desired, "risk_per_trade_pct": 0.4}
        assert (
            operator.put(
                "/api/v1/settings",
                headers=approval,
                json={"expected_version": 2, "settings": proposed},
            ).status_code
            == 409
        )
        with Session(engine) as db:
            account = db.get(PaperSpotAccountRow, 1)
            assert account is not None and account.settings_version == 2

        closed = run_manual_paper_once(
            engine,
            _request("sell", UUID("00000000-0000-4000-8000-000000000015"), budget=None, stop=None),
            observer=_mock_observer(now=now + 60_000),
        )
        assert closed.receipt.event_type == "spot_sell"
        assert _totals(engine)[:3] == (2, 5, 4)

    # Independent engine restart persists both ledger and settings version.
    fresh = create_engine(URL, pool_pre_ping=True)
    try:
        with Session(fresh) as db:
            account = db.get(PaperSpotAccountRow, 1)
            assert account is not None
            assert account.settings_version == 2
            assert account.cash_usdt == closed.receipt.cash_after_usdt
            assert db.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 5
    finally:
        fresh.dispose()


def test_autopilot_enable_disable_carries_unchanged_virtual_money(engine: Engine) -> None:
    """Separate user-consented PAPER pilot transition, never a Binance trade."""
    from binance_ai_trader.paper_scheduler import SchedulerStatus, run_paper_scheduler
    from binance_ai_trader.preview_autopilot import (
        PaperPilotRejected,
        change_paper_pilot_mode,
        paper_pilot_status,
    )

    from binance_ai_trader.preview_autopilot import paper_pilot_preflight

    before = _totals(engine)
    assert paper_pilot_preflight(engine) == "READY_MANUAL_PAPER_MODE"
    with Session(engine) as db:
        prior = db.get(PaperSpotAccountRow, 1)
        assert prior is not None and prior.settings_version == 2
    assert paper_pilot_status(engine) == "MANUAL_PAPER_ONLY_SCHEDULER_OFF"
    new_version = change_paper_pilot_mode(engine, enable=True)
    assert new_version == 3
    assert paper_pilot_status(engine) == "AUTO_PAPER_PERMITTED_NOT_RUNNING_VERIFIED"
    assert paper_pilot_preflight(engine) == "READY_AUTOMATIC_PAPER_PERMISSION"
    assert _totals(engine) == before
    # Persisted mode consent is NOT enough to start a process, HTTP, or trade.
    assert run_paper_scheduler(engine, enabled=False).status is SchedulerStatus.DISABLED
    assert _totals(engine) == before
    with pytest.raises(PaperPilotRejected, match="pilot_not_in_manual_off_state"):
        change_paper_pilot_mode(engine, enable=True)
    assert _totals(engine) == before

    # Emergency revert restores manual-only without resetting the journal.
    version = change_paper_pilot_mode(engine, enable=False)
    assert version == 4
    assert change_paper_pilot_mode(engine, enable=False) == 4
    assert paper_pilot_status(engine) == "MANUAL_PAPER_ONLY_SCHEDULER_OFF"
    assert paper_pilot_preflight(engine) == "READY_MANUAL_PAPER_MODE"
    with Session(engine) as db:
        settings = db.get(TradingSettingsRow, 1)
        account = db.get(PaperSpotAccountRow, 1)
        assert settings is not None and account is not None
        assert settings.version == account.settings_version == 4
        stored = RiskSettings.model_validate(settings.data)
        assert stored.bot_enabled is False and stored.manual_approval is True
        assert stored.spot_capital_usdt == 1000
        assert db.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 5
    assert _totals(engine) == before
