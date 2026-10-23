"""Disposable PostgreSQL tests for durable, inert PAPER entry coordination."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from binance_ai_trader.models import TradingSettingsRow
from binance_ai_trader.paper_entry_coordinator import (
    EntryCoordinatorRejected,
    PaperEntryIntent,
    assess_cross_store_evidence,
    create_intent,
    get_intent,
    list_recovery_candidates,
    list_status_events,
    record_verified_risk_reservation,
    transition_intent,
)
from binance_ai_trader.paper_entry_recovery import RecoveryFinding
from binance_ai_trader.paper_ledger import ensure_paper_spot_account

URL = os.environ.get("TEST_POSTGRES_URL", "")
ISOLATED = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).port == 55444
    and make_url(URL).database == "trader_paper_entry_coordinator_test"
    and os.environ.get("PAPER_ENTRY_COORDINATOR_TEST_ISOLATED") == "1"
)
pytestmark = pytest.mark.skipif(
    not ISOLATED,
    reason="requires isolated trader_paper_entry_coordinator_test on localhost:55444",
)


def _intent(key: str | None = None) -> PaperEntryIntent:
    return PaperEntryIntent(
        idempotency_key=key or f"intent_{uuid4().hex}",
        source_type="MANUAL",
        source_reference="manual-request-0001",
        risk_profile_version=3,
        paper_account_id=1,
        paper_settings_version=7,
        symbol="BTCUSDT",
        quantity=Decimal("0.125"),
        reference_price=Decimal("64000"),
        order_notional_usdt=Decimal("8000"),
        risk_amount_usdt=Decimal("80"),
        fee_cost_usdt=Decimal("8"),
        spread_cost_usdt=Decimal("2"),
        slippage_cost_usdt=Decimal("1"),
        expected_net_advantage_usdt=Decimal("12"),
        price_observed_at_ms=1_800_000_000_000,
        claimed_ledger_snapshot_sha256="a" * 64,
        claimed_cost_evidence_sha256="b" * 64,
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert ISOLATED and URL
    database = create_engine(URL, pool_pre_ping=True)
    with database.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM paper_entry_intents")).scalar_one() == 0
        )
        assert (
            connection.execute(text("SELECT count(*) FROM paper_entry_intent_events")).scalar_one()
            == 0
        )
    with Session(database) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=7,
                data={
                    "mode": "paper",
                    "market_type": "spot",
                    "bot_enabled": False,
                    "spot_capital_usdt": 10000,
                    "futures_capital_usdt": 0,
                    "risk_per_trade_pct": 0.25,
                    "daily_loss_limit_pct": 1.5,
                    "max_drawdown_pct": 5,
                    "max_open_positions": 3,
                    "max_leverage": 1,
                    "stop_loss_required": True,
                    "manual_approval": True,
                    "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
                },
            )
        )
    with Session(database) as session:
        ensure_paper_spot_account(session)
    yield database
    database.dispose()


def _ledger_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return tuple(
            int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in (
                "paper_spot_accounts",
                "paper_spot_positions",
                "paper_spot_journal",
            )
        )  # type: ignore[return-value]


def test_intent_create_retry_conflict_transition_and_audit(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _intent()
    with pytest.raises(EntryCoordinatorRejected, match="stale_paper_settings_version"):
        create_intent(
            engine,
            replace(
                _intent(),
                idempotency_key=f"stale_{uuid4().hex}",
                paper_settings_version=6,
            ),
        )
    first = create_intent(engine, payload)
    retry = create_intent(engine, payload)
    assert retry.request_id == first.request_id
    assert retry.intent_sha256 == first.intent_sha256
    assert first.current_state == "PREPARED"
    risk_url = os.environ.get("RISK_TEST_POSTGRES_URL")
    if risk_url:
        risk_engine = create_engine(risk_url, pool_pre_ping=True)
        try:
            with pytest.raises(
                EntryCoordinatorRejected,
                match="independent_risk_reservation_unverified",
            ):
                record_verified_risk_reservation(engine, risk_engine, request_id=first.request_id)
            assert get_intent(engine, first.request_id).current_state == "PREPARED"  # type: ignore[union-attr]
            # Even an otherwise valid risk-store receipt cannot promote E1's
            # unverified caller-supplied ledger/cost hashes to trusted evidence.
            from binance_ai_trader import risk_store

            monkeypatch.setattr(
                risk_store,
                "get_verified_reservation",
                lambda *_args, **_kwargs: risk_store.VerifiedReservation(
                    first.request_id,
                    first.request_id,
                    first.risk_profile_version,
                    Decimal("8000"),
                    "c" * 64,
                    first.intent_sha256,
                    first.request_id,
                    first.request_id + "_reserve",
                ),
            )
            with pytest.raises(
                EntryCoordinatorRejected,
                match="paper_snapshot_and_cost_provenance_unverified",
            ):
                record_verified_risk_reservation(engine, risk_engine, request_id=first.request_id)
            assert get_intent(engine, first.request_id).current_state == "PREPARED"  # type: ignore[union-attr]
        finally:
            risk_engine.dispose()

    with pytest.raises(EntryCoordinatorRejected, match="idempotency_key_payload_conflict"):
        create_intent(engine, replace(payload, quantity=Decimal("0.25")))
    with pytest.raises(EntryCoordinatorRejected, match="invalid_entry_state_transition"):
        transition_intent(
            engine,
            request_id=first.request_id,
            expected_state="PREPARED",
            new_state="COMPLETED",
            reason_code="skip_stages",
        )
    with pytest.raises(
        EntryCoordinatorRejected, match="risk_reservation_requires_independent_store_verification"
    ):
        transition_intent(
            engine,
            request_id=first.request_id,
            expected_state="PREPARED",
            new_state="RISK_RESERVED",
            reason_code="missing_risk_receipt",
            evidence_reference="forged-receipt-reference",
        )

    events = list_status_events(engine, first.request_id)
    assert [event.to_state for event in events] == ["PREPARED"]


def test_concurrent_same_key_is_idempotent_and_different_keys_are_distinct(
    engine: Engine,
) -> None:
    barrier = Barrier(2)
    payload = _intent()

    def same_request() -> str:
        barrier.wait()
        return create_intent(engine, payload).request_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_ids = list(pool.map(lambda _index: same_request(), range(2)))
    assert same_ids[0] == same_ids[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        distinct_ids = list(
            pool.map(lambda index: create_intent(engine, _intent()).request_id, range(2))
        )
    assert distinct_ids[0] != distinct_ids[1]


def test_idempotent_replay_survives_later_paper_settings_revision(engine: Engine) -> None:
    payload = _intent()
    original = create_intent(engine, payload)
    with engine.begin() as connection:
        connection.execute(text("UPDATE paper_spot_accounts SET settings_version=8 WHERE id=1"))
    try:
        retry = create_intent(engine, payload)
        assert retry.request_id == original.request_id
        assert retry.intent_sha256 == original.intent_sha256
    finally:
        with engine.begin() as connection:
            connection.execute(text("UPDATE paper_spot_accounts SET settings_version=7 WHERE id=1"))


def test_database_trigger_rejects_invalid_transition_and_rolls_back(engine: Engine) -> None:
    record = create_intent(engine, _intent())
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO paper_entry_intent_events "
                "(event_id,request_id,sequence,from_state,to_state,reason_code) "
                "VALUES (:event,:request,2,'PREPARED','COMPLETED','invalid_shortcut')"
            ),
            {"event": str(uuid4()), "request": record.request_id},
        )
    assert [event.to_state for event in list_status_events(engine, record.request_id)] == [
        "PREPARED"
    ]


def test_state_event_transaction_rollback_preserves_prior_history(engine: Engine) -> None:
    record = create_intent(engine, _intent())
    with (
        pytest.raises(RuntimeError, match="simulated_coordinator_crash"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO paper_entry_intent_events "
                "(event_id,request_id,sequence,from_state,to_state,reason_code,"
                "evidence_reference) VALUES (:event,:request,2,'PREPARED','RISK_RESERVED',"
                "'risk_hold_recorded','risk-receipt-0001')"
            ),
            {"event": str(uuid4()), "request": record.request_id},
        )
        raise RuntimeError("simulated_coordinator_crash")
    assert [event.to_state for event in list_status_events(engine, record.request_id)] == [
        "PREPARED"
    ]


def test_restart_recovery_classification_and_ledger_immutability(engine: Engine) -> None:
    record = create_intent(engine, _intent())
    ambiguous = transition_intent(
        engine,
        request_id=record.request_id,
        expected_state="PREPARED",
        new_state="RECONCILIATION_REQUIRED",
        reason_code="reservation_outcome_uncertain",
    )
    assert ambiguous.current_state == "RECONCILIATION_REQUIRED"
    before = _ledger_counts(engine)
    engine.dispose()
    restarted = create_engine(URL, pool_pre_ping=True)
    try:
        restored = get_intent(restarted, record.request_id)
        assert restored is not None and restored.intent_sha256 == record.intent_sha256
        candidates = list_recovery_candidates(restarted)
        assert any(candidate.intent.request_id == record.request_id for candidate in candidates)
        candidate = next(
            candidate
            for candidate in candidates
            if candidate.intent.request_id == record.request_id
        )
        assert candidate.latest_event.to_state == "RECONCILIATION_REQUIRED"
        assert candidate.new_entry_authorized is False
        assert candidate.automatic_reservation_release_allowed is False
        assert candidate.manual_reconciliation_required is True
        assert (
            assess_cross_store_evidence(
                request_id=record.request_id, risk=None, ledger=None
            ).finding
            == RecoveryFinding.NO_EVIDENCE
        )
        assert _ledger_counts(restarted) == before
    finally:
        restarted.dispose()


def test_intent_and_status_history_reject_update_delete_and_truncate(engine: Engine) -> None:
    record = create_intent(engine, _intent())
    statements = (
        (
            "UPDATE paper_entry_intent_events SET reason_code='tampered' "
            f"WHERE request_id='{record.request_id}'"
        ),
        f"DELETE FROM paper_entry_intent_events WHERE request_id='{record.request_id}'",
        "TRUNCATE paper_entry_intent_events",
        "UPDATE paper_entry_intents SET intent_sha256='{}' WHERE request_id='{}'".format(
            "f" * 64, record.request_id
        ),
        f"DELETE FROM paper_entry_intents WHERE request_id='{record.request_id}'",
        "TRUNCATE paper_entry_intents",
    )
    for statement in statements:
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(statement))
