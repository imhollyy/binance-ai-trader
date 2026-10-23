"""Disposable PostgreSQL tests for durable, inert PAPER entry coordination."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.models import (
    TradingSettingsRow,
)
from binance_ai_trader.paper_entry_coordinator import (
    EntryCoordinatorRejected,
    PaperEntryIntent,
    assess_cross_store_evidence,
    commit_risk_reserved_for_isolated_test,
    create_evidenced_intent_for_test,
    create_intent,
    get_intent,
    list_recovery_candidates,
    list_status_events,
    record_verified_risk_reservation,
    recover_committed_entry,
    transition_intent,
)
from binance_ai_trader.paper_entry_evidence import (
    EntryEvidenceRejected,
    capture_paper_account_evidence,
    collect_pre_entry_cost_evidence,
)
from binance_ai_trader.paper_entry_recovery import RecoveryFinding
from binance_ai_trader.paper_execution import close_paper_spot
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_spot import PaperCosts
from binance_ai_trader.public_spot_cost_review import PublicSpotCostClient
from binance_ai_trader.public_spot_depth_review import PublicSpotDepthClient
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_guard import RiskProfile, RiskRequest
from binance_ai_trader.risk_store import (
    RiskStoreRejected,
    evaluate_and_reserve,
    install_profile,
    record_account_snapshot,
    set_bot_state,
)

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


def _entry_observation_clients(
    delay_ms: int = 2,
) -> tuple[PublicSpotObservationClient, PublicSpotCostClient, PublicSpotDepthClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if delay_ms:
            time.sleep(delay_ms / 1_000)
        clock_ms = time.time_ns() // 1_000_000
        if path == "/api/v3/klines":
            last_open_ms = (clock_ms // 60_000) * 60_000 - 60_000
            candles = []
            for index in range(60):
                open_ms = last_open_ms - (59 - index) * 60_000
                candles.append(
                    [
                        open_ms,
                        "100",
                        "101",
                        "99",
                        "100",
                        "20",
                        open_ms + 59_999,
                        "0",
                        1,
                        "0",
                        "0",
                        "0",
                    ]
                )
            return httpx.Response(200, json=candles)
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100.05"})
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "isSpotTradingAllowed": True,
                            "orderTypes": ["MARKET", "LIMIT"],
                            "permissionSets": [["SPOT"]],
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "minPrice": "0.01",
                                    "maxPrice": "1000000",
                                    "tickSize": "0.01",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.01",
                                    "maxQty": "10",
                                    "stepSize": "0.01",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.01",
                                    "maxQty": "4",
                                    "stepSize": "0.01",
                                },
                                {
                                    "filterType": "NOTIONAL",
                                    "minNotional": "10",
                                    "applyMinToMarket": True,
                                    "maxNotional": "10000",
                                    "applyMaxToMarket": True,
                                    "avgPriceMins": 0,
                                },
                            ],
                        }
                    ]
                },
            )
        if path == "/api/v3/ticker/bookTicker":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "bidPrice": "99.90",
                    "askPrice": "100.10",
                    "bidQty": "3",
                    "askQty": "3",
                },
            )
        if path == "/api/v3/depth":
            return httpx.Response(
                200,
                json={
                    "lastUpdateId": 101,
                    "bids": [["99.90", "3"], ["99.80", "2"]],
                    "asks": [["100.10", "3"], ["100.20", "2"]],
                },
            )
        raise AssertionError(path)

    transport = httpx.MockTransport(handler)

    def clock_ms() -> int:
        # Use observation-time wall clock, not a timestamp captured when the
        # test client was constructed. CI scheduling delays must not turn fresh
        # mock responses into stale evidence before the DB write.
        return time.time_ns() // 1_000_000

    return (
        PublicSpotObservationClient(transport=transport, clock_ms=clock_ms),
        PublicSpotCostClient(transport=transport, clock_ms=clock_ms),
        PublicSpotDepthClient(transport=transport, clock_ms=clock_ms),
    )


def _costed_intent() -> PaperEntryIntent:
    return replace(
        _intent(),
        idempotency_key=f"observed_{uuid4().hex}",
        source_reference="observed-preentry-01",
        risk_profile_version=1,
        quantity=Decimal("1"),
        reference_price=Decimal("100.05"),
        order_notional_usdt=Decimal("100.05"),
        risk_amount_usdt=Decimal("1"),
        fee_cost_usdt=Decimal("0.10005"),
        spread_cost_usdt=Decimal("0.2001"),
        slippage_cost_usdt=Decimal("0.050025"),
        expected_net_advantage_usdt=Decimal("1"),
    )


def test_intent_create_retry_conflict_transition_and_audit(engine: Engine) -> None:
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
                match="pre_entry_evidence_missing",
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
    with pytest.raises(
        EntryCoordinatorRejected, match="ledger_commit_requires_coordinator_receipt"
    ):
        transition_intent(
            engine,
            request_id=first.request_id,
            expected_state="RISK_RESERVED",
            new_state="LEDGER_COMMITTED",
            reason_code="forged_ledger_commit",
            evidence_reference="f" * 64,
        )
    with pytest.raises(
        EntryCoordinatorRejected, match="completion_requires_receipt_reconciliation"
    ):
        transition_intent(
            engine,
            request_id=first.request_id,
            expected_state="LEDGER_COMMITTED",
            new_state="COMPLETED",
            reason_code="forged_completion",
            evidence_reference="f" * 64,
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

    conflicting_key = f"race_conflict_{uuid4().hex}"
    payloads = (
        replace(_intent(conflicting_key), quantity=Decimal("0.125")),
        replace(_intent(conflicting_key), quantity=Decimal("0.25")),
    )
    conflict_barrier = Barrier(2)

    def conflicting_request(payload: PaperEntryIntent) -> tuple[str, str]:
        conflict_barrier.wait()
        try:
            result = create_intent(engine, payload)
            return "created", result.request_id
        except EntryCoordinatorRejected as exc:
            return "rejected", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        conflict_results = list(pool.map(conflicting_request, payloads))
    assert sorted(result[0] for result in conflict_results) == ["created", "rejected"]
    assert next(result[1] for result in conflict_results if result[0] == "rejected") == (
        "idempotency_key_payload_conflict"
    )


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


def test_account_evidence_is_reconciled_and_bound_to_settings_and_journal(
    engine: Engine,
) -> None:
    evidence = capture_paper_account_evidence(engine, account_id=1, expected_settings_version=7)
    assert evidence.account_id == 1
    assert evidence.settings_version == 7
    assert evidence.journal_count >= 1
    assert evidence.journal_head_id == evidence.payload["journal_head_id"]
    assert evidence.snapshot_sha256 != "a" * 64
    assert evidence.payload["paid_commissions_usdt"] == "0"

    with pytest.raises(EntryEvidenceRejected, match="stale_paper_settings_version"):
        capture_paper_account_evidence(engine, account_id=1, expected_settings_version=6)


def test_pre_entry_cost_evidence_uses_public_closed_data_and_rejects_bad_costs() -> None:
    candle_client, book_client, depth_client = _entry_observation_clients()
    intent = _costed_intent()
    cost = collect_pre_entry_cost_evidence(
        intent,
        candle_client=candle_client,
        book_client=book_client,
        depth_client=depth_client,
    )
    assert cost.evidence_sha256 != "b" * 64
    assert cost.spread_bps > 0
    assert cost.payload["trade_authorized"] is False
    assert cost.payload["fee_assumption_version"]
    assert cost.payload["slippage_assumption_version"]

    stale_clients = _entry_observation_clients()
    stale_intent = replace(intent, fee_cost_usdt=Decimal("0.50"))
    with pytest.raises(EntryEvidenceRejected, match="intent_costs_or_price"):
        collect_pre_entry_cost_evidence(
            stale_intent,
            candle_client=stale_clients[0],
            book_client=stale_clients[1],
            depth_client=stale_clients[2],
        )
    stale_clients = _entry_observation_clients(delay_ms=4_000)
    with pytest.raises(EntryEvidenceRejected, match="pre_entry_market_observation_unavailable"):
        collect_pre_entry_cost_evidence(
            intent,
            candle_client=stale_clients[0],
            book_client=stale_clients[1],
            depth_client=stale_clients[2],
        )
    for bad_cost in (
        replace(intent, spread_cost_usdt=Decimal("0")),
        replace(intent, slippage_cost_usdt=Decimal("0")),
    ):
        bad_clients = _entry_observation_clients()
        with pytest.raises(EntryEvidenceRejected, match="intent_costs_or_price"):
            collect_pre_entry_cost_evidence(
                bad_cost,
                candle_client=bad_clients[0],
                book_client=bad_clients[1],
                depth_client=bad_clients[2],
            )


def test_evidenced_intent_persists_real_snapshot_and_cost_receipts_without_buy(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _ledger_counts(engine)
    with pytest.raises(EntryCoordinatorRejected, match="isolated_dual_postgresql_test_required"):
        commit_risk_reserved_for_isolated_test(engine, engine, request_id=str(uuid4()))
    monkeypatch.setenv("PAPER_ENTRY_E3_TEST_ONLY", "1")
    monkeypatch.setenv("PAPER_ENTRY_COORDINATOR_TEST_ISOLATED", "1")
    monkeypatch.setenv("TRADEPILOT_PAPER_ENTRY_RISK_TEST_DB", "1")
    monkeypatch.setenv("PAPER_LEDGER_TEST_ISOLATED", "1")
    isolated_risk_engine = create_engine(os.environ["RISK_TEST_POSTGRES_URL"])
    with pytest.raises(EntryCoordinatorRejected, match="entry_intent_not_found"):
        commit_risk_reserved_for_isolated_test(
            engine, isolated_risk_engine, request_id=str(uuid4())
        )
    isolated_risk_engine.dispose()
    assert _ledger_counts(engine) == before
    clients = _entry_observation_clients()
    monkeypatch.setenv("PAPER_ENTRY_EVIDENCE_TEST_ONLY", "0")
    with pytest.raises(EntryCoordinatorRejected, match="isolated_evidence_test_database_required"):
        create_evidenced_intent_for_test(
            engine,
            _costed_intent(),
            candle_client=clients[0],
            book_client=clients[1],
            depth_client=clients[2],
        )
    monkeypatch.setenv("PAPER_ENTRY_EVIDENCE_TEST_ONLY", "1")
    record = create_evidenced_intent_for_test(
        engine,
        _costed_intent(),
        candle_client=clients[0],
        book_client=clients[1],
        depth_client=clients[2],
    )
    competing_clients = _entry_observation_clients()
    competing_record = create_evidenced_intent_for_test(
        engine,
        _costed_intent(),
        candle_client=competing_clients[0],
        book_client=competing_clients[1],
        depth_client=competing_clients[2],
    )
    with engine.connect() as connection:
        account = (
            connection.execute(
                text(
                    "SELECT account_id,settings_version,journal_head_id,journal_count,"
                    "snapshot_sha256 "
                    "FROM paper_entry_account_evidence WHERE request_id=:id"
                ),
                {"id": record.request_id},
            )
            .mappings()
            .one()
        )
        market = (
            connection.execute(
                text(
                    "SELECT symbol,source,reference_price,bid_price,ask_price,spread_bps,"
                    "fee_assumption_version,slippage_assumption_version,"
                    "cost_evidence_sha256 "
                    "FROM paper_entry_market_evidence WHERE request_id=:id"
                ),
                {"id": record.request_id},
            )
            .mappings()
            .one()
        )
    assert account["account_id"] == 1
    assert account["settings_version"] == 7
    assert account["journal_count"] >= 1
    assert len(account["snapshot_sha256"]) == 64
    assert market["source"] == "binance_public_spot_coherence"
    assert market["symbol"] == "BTCUSDT"
    assert market["ask_price"] > market["bid_price"]
    assert market["spread_bps"] > 0
    assert market["fee_assumption_version"]
    assert market["slippage_assumption_version"]
    assert len(market["cost_evidence_sha256"]) == 64
    assert record.current_state == "PREPARED"
    assert _ledger_counts(engine) == before

    risk_url = os.environ["RISK_TEST_POSTGRES_URL"]
    risk_engine = create_engine(risk_url, pool_pre_ping=True)
    try:
        ai_intent = replace(
            _costed_intent(),
            idempotency_key=f"unapproved_ai_{uuid4().hex}",
            source_type="AI",
            model_version="research-only-test-model",
        )
        ai_record = create_intent(engine, ai_intent)
        with pytest.raises(EntryCoordinatorRejected, match="unapproved_ai_model_cannot_reserve"):
            record_verified_risk_reservation(engine, risk_engine, request_id=ai_record.request_id)
        assert _ledger_counts(engine) == before

        profile = RiskProfile(
            "1.0",
            1,
            1,
            3,
            Decimal("1"),
            Decimal("300"),
            Decimal("300"),
            Decimal("10"),
            Decimal("20"),
            3,
            (("confirmed_test_cap", Decimal("300")),),
        )
        assert (
            install_profile(risk_engine, profile, user_confirmed=True, expected_runtime_revision=0)
            == 1
        )
        assert set_bot_state(risk_engine, state=BotState.PAUSED, expected_revision=1) == 2
        assert set_bot_state(risk_engine, state=BotState.RUNNING, expected_revision=2) == 3
        snapshot = capture_paper_account_evidence(engine, account_id=1, expected_settings_version=7)
        competing_snapshot = capture_paper_account_evidence(
            engine, account_id=1, expected_settings_version=7
        )
        assert competing_snapshot.snapshot_sha256 == snapshot.snapshot_sha256
        record_account_snapshot(
            risk_engine,
            snapshot_id=record.request_id,
            equity=Decimal(snapshot.payload["risk_equity_usdt"]),
            day_start_equity=Decimal(snapshot.payload["day_start_equity_usdt"]),
            peak_equity=Decimal(snapshot.payload["peak_equity_usdt"]),
            open_capital=Decimal(snapshot.payload["open_capital_usdt"]),
            open_positions=int(snapshot.payload["open_positions"]),
            consecutive_losses=int(snapshot.payload["consecutive_losses"]),
            source_sha256=snapshot.snapshot_sha256,
        )
        record_account_snapshot(
            risk_engine,
            snapshot_id=competing_record.request_id,
            equity=Decimal(competing_snapshot.payload["risk_equity_usdt"]),
            day_start_equity=Decimal(competing_snapshot.payload["day_start_equity_usdt"]),
            peak_equity=Decimal(competing_snapshot.payload["peak_equity_usdt"]),
            open_capital=Decimal(competing_snapshot.payload["open_capital_usdt"]),
            open_positions=int(competing_snapshot.payload["open_positions"]),
            consecutive_losses=int(competing_snapshot.payload["consecutive_losses"]),
            source_sha256=competing_snapshot.snapshot_sha256,
        )
        net_advantage = Decimal("1")
        fee_bps = Decimal("10")
        slippage_bps = Decimal("5")
        reference = Decimal("100.05")
        spread_bps = Decimal(market["spread_bps"])
        gross_edge = net_advantage * Decimal(10_000) / Decimal("100.05")
        gross_edge += fee_bps * 2 + spread_bps + slippage_bps
        risk_request = RiskRequest(
            settings_version=1,
            bot_state=BotState.RUNNING,
            proposed_notional=Decimal("100.05"),
            stop_loss_price=Decimal("99.05"),
            take_profit_price=Decimal("102"),
            reference_price=reference,
            fee_bps_round_trip=fee_bps * 2,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            expected_gross_edge_bps=gross_edge,
            min_net_edge_bps=Decimal(0),
            current_open_capital=Decimal(0),
            open_positions=0,
            equity=Decimal(snapshot.payload["risk_equity_usdt"]),
            day_start_equity=Decimal(snapshot.payload["day_start_equity_usdt"]),
            peak_equity=Decimal(snapshot.payload["peak_equity_usdt"]),
            consecutive_losses=0,
        )
        with pytest.raises(RiskStoreRejected, match="paper_snapshot_hash_not_in_risk_store"):
            evaluate_and_reserve(
                risk_engine,
                request_id=record.request_id,
                signal_id=record.request_id,
                profile_version=1,
                request=risk_request,
                intent_sha256=record.intent_sha256,
                paper_snapshot_sha256="f" * 64,
            )
        decision = evaluate_and_reserve(
            risk_engine,
            request_id=record.request_id,
            signal_id=record.request_id,
            profile_version=1,
            request=risk_request,
            intent_sha256=record.intent_sha256,
            paper_snapshot_sha256=snapshot.snapshot_sha256,
        )
        assert decision.allowed
        competing_decision = evaluate_and_reserve(
            risk_engine,
            request_id=competing_record.request_id,
            signal_id=competing_record.request_id,
            profile_version=1,
            request=risk_request,
            intent_sha256=competing_record.intent_sha256,
            paper_snapshot_sha256=competing_snapshot.snapshot_sha256,
        )
        assert competing_decision.allowed
        reservation_app = make_url(os.environ["TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_risk_reserved_app_{uuid4().hex[:8]}"}
        )
        reservation_risk = make_url(os.environ["RISK_TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_risk_reserved_risk_{uuid4().hex[:8]}"}
        )
        reservation_marker = tmp_path / "risk-reserved-event-committed"
        reservation_code = (
            "import os,time; from pathlib import Path; from sqlalchemy import create_engine; "
            "from binance_ai_trader.paper_entry_coordinator import "
            "record_verified_risk_reservation; "
            "app=create_engine(os.environ['TEST_POSTGRES_URL']); "
            "risk=create_engine(os.environ['RISK_TEST_POSTGRES_URL']); "
            "record_verified_risk_reservation(app,risk,request_id=os.environ['E3T3_REQUEST']); "
            "Path(os.environ['E3T3_MARKER']).write_text('RISK_RESERVED'); time.sleep(60)"
        )
        reservation_child = subprocess.Popen(
            [sys.executable, "-c", reservation_code],
            env={
                **os.environ,
                "TEST_POSTGRES_URL": reservation_app.render_as_string(hide_password=False),
                "RISK_TEST_POSTGRES_URL": reservation_risk.render_as_string(hide_password=False),
                "E3T3_REQUEST": record.request_id,
                "E3T3_MARKER": str(reservation_marker),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_marker(reservation_child, reservation_marker) == "RISK_RESERVED"
            _kill_process(reservation_child)
        finally:
            if reservation_child.poll() is None:
                _kill_process(reservation_child)
        reserved = get_intent(engine, record.request_id)
        assert reserved is not None and reserved.current_state == "RISK_RESERVED"
        competing_reserved = record_verified_risk_reservation(
            engine, risk_engine, request_id=competing_record.request_id
        )
        assert competing_reserved.current_state == "RISK_RESERVED"
        assert _ledger_counts(engine) == before

        # Block inside the real application transaction at the journal INSERT,
        # kill the coordinator process, and verify PostgreSQL rolls back the
        # uncommitted position/account/journal changes while retaining the risk
        # reservation. The trigger and advisory lock exist only in this exact
        # disposable test database and are removed in finally.
        crash_counts = _ledger_counts(engine)
        with engine.connect() as connection:
            cash_before_crash: Decimal = connection.execute(
                text("SELECT cash_usdt FROM paper_spot_accounts WHERE id=1")
            ).scalar_one()
        lock_key = 270027027
        app_name = f"e3t3_ledger_{uuid4().hex[:12]}"
        blocker = engine.connect()
        blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION bt027_e3t3_wait_journal_insert() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    "PERFORM pg_advisory_xact_lock(270027027); RETURN NEW; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER bt027_e3t3_wait_journal_insert "
                    "BEFORE INSERT ON paper_spot_journal FOR EACH ROW "
                    "EXECUTE FUNCTION bt027_e3t3_wait_journal_insert()"
                )
            )
        child_app_url = make_url(os.environ["TEST_POSTGRES_URL"]).set(
            query={"application_name": app_name}
        )
        child_risk_url = make_url(os.environ["RISK_TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_risk_{uuid4().hex[:10]}"}
        )
        child_code = (
            "import os; from sqlalchemy import create_engine; "
            "from binance_ai_trader.paper_entry_coordinator import "
            "commit_risk_reserved_for_isolated_test; "
            "app=create_engine(os.environ['TEST_POSTGRES_URL']); "
            "risk=create_engine(os.environ['RISK_TEST_POSTGRES_URL']); "
            "commit_risk_reserved_for_isolated_test(app,risk,"
            "request_id=os.environ['E3T3_BLOCK_REQUEST'])"
        )
        blocked_child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            env={
                **os.environ,
                "TEST_POSTGRES_URL": child_app_url.render_as_string(hide_password=False),
                "RISK_TEST_POSTGRES_URL": child_risk_url.render_as_string(hide_password=False),
                "E3T3_BLOCK_REQUEST": record.request_id,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            blocked_in_trigger = False
            while time.monotonic() < deadline:
                with engine.connect() as connection:
                    blocked_in_trigger = bool(
                        connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name=:name AND wait_event_type='Lock'"
                            ),
                            {"name": app_name},
                        ).scalar_one()
                    )
                if blocked_in_trigger or blocked_child.poll() is not None:
                    break
                time.sleep(0.01)
            if not blocked_in_trigger:
                stdout, stderr = blocked_child.communicate(timeout=5)
                raise AssertionError(
                    f"coordinator did not block at journal INSERT: {stdout!r}; {stderr!r}"
                )
            _kill_process(blocked_child)
        finally:
            if blocked_child.poll() is None:
                _kill_process(blocked_child)
            blocker.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
            blocker.close()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DROP TRIGGER IF EXISTS bt027_e3t3_wait_journal_insert "
                        "ON paper_spot_journal"
                    )
                )
                connection.execute(text("DROP FUNCTION IF EXISTS bt027_e3t3_wait_journal_insert()"))
        with engine.connect() as connection:
            assert _ledger_counts(engine) == crash_counts
            assert (
                connection.execute(
                    text("SELECT cash_usdt FROM paper_spot_accounts WHERE id=1")
                ).scalar_one()
                == cash_before_crash
            )
        assert get_intent(engine, record.request_id).current_state == "RISK_RESERVED"  # type: ignore[union-attr]
        with risk_engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id=:id AND event_type='RESERVE'"
                    ),
                    {"id": record.request_id},
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id=:id AND event_type='RELEASE'"
                    ),
                    {"id": record.request_id},
                ).scalar_one()
                == 0
            )
        with pytest.raises(
            EntryCoordinatorRejected, match="ledger_commit_requires_coordinator_receipt"
        ):
            transition_intent(
                engine,
                request_id=record.request_id,
                expected_state="RISK_RESERVED",
                new_state="LEDGER_COMMITTED",
                reason_code="fake_ledger_receipt",
                evidence_reference="f" * 64,
            )
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO paper_entry_intent_events "
                    "(event_id,request_id,sequence,from_state,to_state,reason_code,"
                    "evidence_reference) VALUES (:event,:request,3,'RISK_RESERVED',"
                    "'LEDGER_COMMITTED','fake_ledger_receipt',:evidence)"
                ),
                {"event": str(uuid4()), "request": record.request_id, "evidence": "f" * 64},
            )
        assert set_bot_state(risk_engine, state=BotState.PAUSED, expected_revision=3) == 4
        with pytest.raises(
            EntryCoordinatorRejected, match="independent_risk_reservation_unverified"
        ):
            record_verified_risk_reservation(engine, risk_engine, request_id=record.request_id)
        assert set_bot_state(risk_engine, state=BotState.RUNNING, expected_revision=4) == 5
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE paper_spot_accounts SET cash_usdt=cash_usdt + 1 WHERE id=1")
            )
        try:
            with pytest.raises(
                EntryCoordinatorRejected, match="paper_ledger_reconciliation_failed"
            ):
                record_verified_risk_reservation(engine, risk_engine, request_id=record.request_id)
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE paper_spot_accounts SET cash_usdt=cash_usdt - 1 WHERE id=1")
                )
        assert get_intent(engine, record.request_id).current_state == "RISK_RESERVED"  # type: ignore[union-attr]

        # Execute the same fully verified reservation only in this exact dual-DB
        # disposable fixture. Production open_paper_spot remains fail-closed.
        monkeypatch.setenv("PAPER_ENTRY_E3_TEST_ONLY", "1")
        monkeypatch.setenv("PAPER_LEDGER_TEST_ISOLATED", "1")
        before_commit = _ledger_counts(engine)
        risk_engine.dispose()
        with engine.begin() as connection:
            connection.execute(
                text("ALTER DATABASE trader_paper_entry_risk_test WITH ALLOW_CONNECTIONS false")
            )
        unavailable_risk_engine = create_engine(risk_url, pool_pre_ping=True)
        try:
            with pytest.raises(DBAPIError):
                commit_risk_reserved_for_isolated_test(
                    engine, unavailable_risk_engine, request_id=record.request_id
                )
            assert _ledger_counts(engine) == before_commit
            assert get_intent(engine, record.request_id).current_state == "RISK_RESERVED"  # type: ignore[union-attr]
        finally:
            unavailable_risk_engine.dispose()
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER DATABASE trader_paper_entry_risk_test WITH ALLOW_CONNECTIONS true")
                )
        risk_engine = create_engine(risk_url, pool_pre_ping=True)
        engine.dispose()
        restarted_engine = create_engine(URL, pool_pre_ping=True)
        assert get_intent(restarted_engine, record.request_id).current_state == "RISK_RESERVED"  # type: ignore[union-attr]
        for crash_point, expected_message in (
            ("before_ledger", "simulated_crash_before_ledger"),
            ("inside_ledger", "simulated_crash_inside_ledger_transaction"),
        ):
            with pytest.raises(RuntimeError, match=expected_message):
                commit_risk_reserved_for_isolated_test(
                    engine,
                    risk_engine,
                    request_id=record.request_id,
                    _simulate_crash_at=crash_point,
                )
            assert get_intent(engine, record.request_id).current_state == "RISK_RESERVED"  # type: ignore[union-attr]
            assert _ledger_counts(engine) == before_commit
        buy_barrier = Barrier(3)

        def concurrent_buy(request_id: str) -> tuple[str, str]:
            try:
                result = commit_risk_reserved_for_isolated_test(
                    engine,
                    risk_engine,
                    request_id=request_id,
                    _simulate_crash_at="after_ledger_commit",
                    _test_start_barrier=buy_barrier,
                )
                return "committed", str(result.trade_id)
            except (EntryCoordinatorRejected, EntryEvidenceRejected) as exc:
                return "rejected", str(exc)

        with ThreadPoolExecutor(max_workers=3) as pool:
            concurrent_results = list(
                pool.map(
                    concurrent_buy,
                    (record.request_id, record.request_id, competing_record.request_id),
                )
            )
        successful_trade_ids = {
            trade_id for status, trade_id in concurrent_results if status == "committed"
        }
        assert len(successful_trade_ids) == 1
        active_request_id = successful_trade_ids.pop()
        assert get_intent(engine, active_request_id).current_state == "LEDGER_COMMITTED"  # type: ignore[union-attr]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM paper_entry_ledger_commits WHERE request_id=:id"),
                    {"id": active_request_id},
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM paper_entry_ledger_commits")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM paper_spot_journal WHERE event_type='spot_buy'")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(text("SELECT count(*) FROM paper_spot_positions")).scalar_one()
                == 1
            )
        with risk_engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id IN (:first,:second) AND event_type='RESERVE'"
                    ),
                    {"first": record.request_id, "second": competing_record.request_id},
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id IN (:first,:second) AND event_type='RELEASE'"
                    ),
                    {"first": record.request_id, "second": competing_record.request_id},
                ).scalar_one()
                == 0
            )
        with pytest.raises(
            EntryCoordinatorRejected, match="completion_requires_receipt_reconciliation"
        ):
            transition_intent(
                engine,
                request_id=active_request_id,
                expected_state="LEDGER_COMMITTED",
                new_state="COMPLETED",
                reason_code="fake_completion",
                evidence_reference="f" * 64,
            )
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO paper_entry_intent_events "
                    "(event_id,request_id,sequence,from_state,to_state,reason_code,"
                    "evidence_reference) VALUES (:event,:request,4,'LEDGER_COMMITTED',"
                    "'COMPLETED','fake_completion',:evidence)"
                ),
                {"event": str(uuid4()), "request": active_request_id, "evidence": "f" * 64},
            )
        # Simulate process restart: rebuild the SQLAlchemy engine and reconcile
        # the committed app receipt without touching RiskGuard or replaying BUY.
        engine.dispose()
        restarted_engine = create_engine(URL, pool_pre_ping=True)
        assert (
            recover_committed_entry(restarted_engine, request_id=active_request_id).current_state
            == "COMPLETED"
        )
        assert get_intent(engine, active_request_id).current_state == "COMPLETED"  # type: ignore[union-attr]
        after_commit = _ledger_counts(engine)
        assert after_commit[1] == before_commit[1] + 1
        assert after_commit[2] == before_commit[2] + 1
        replayed = commit_risk_reserved_for_isolated_test(
            engine, risk_engine, request_id=active_request_id
        )
        assert replayed.current_state == "COMPLETED"
        assert _ledger_counts(engine) == after_commit
        completed_app_url = make_url(os.environ["TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_completed_{uuid4().hex[:8]}"}
        )
        completed_risk_url = make_url(os.environ["RISK_TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_completed_risk_{uuid4().hex[:8]}"}
        )
        completed_marker = tmp_path / "completed-response-lost"
        completed_code = (
            "import os,time; from pathlib import Path; from sqlalchemy import create_engine; "
            "from binance_ai_trader.paper_entry_coordinator import "
            "commit_risk_reserved_for_isolated_test; "
            "app=create_engine(os.environ['TEST_POSTGRES_URL']); "
            "risk=create_engine(os.environ['RISK_TEST_POSTGRES_URL']); "
            "result=commit_risk_reserved_for_isolated_test(app,risk,"
            "request_id=os.environ['E3T3_REQUEST']); "
            "Path(os.environ['E3T3_MARKER']).write_text(result.current_state); time.sleep(60)"
        )
        completed_child = subprocess.Popen(
            [sys.executable, "-c", completed_code],
            env={
                **os.environ,
                "TEST_POSTGRES_URL": completed_app_url.render_as_string(hide_password=False),
                "RISK_TEST_POSTGRES_URL": completed_risk_url.render_as_string(hide_password=False),
                "E3T3_REQUEST": active_request_id,
                "E3T3_MARKER": str(completed_marker),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_marker(completed_child, completed_marker) == "COMPLETED"
            _kill_process(completed_child)
        finally:
            if completed_child.poll() is None:
                _kill_process(completed_child)
        engine.dispose()
        restarted_engine = create_engine(URL, pool_pre_ping=True)
        assert _ledger_counts(restarted_engine) == after_commit
        assert get_intent(restarted_engine, active_request_id).current_state == "COMPLETED"  # type: ignore[union-attr]
        with pytest.raises(RuntimeError, match="simulated_crash_after_completed"):
            commit_risk_reserved_for_isolated_test(
                engine,
                risk_engine,
                request_id=active_request_id,
                _simulate_crash_at="after_completed",
            )
        assert (
            recover_committed_entry(engine, request_id=active_request_id).current_state
            == "COMPLETED"
        )
        with Session(engine) as session:
            close_paper_spot(
                session,
                active_request_id,
                Decimal("101"),
                f"close_{uuid4().hex}",
                explicit_approval=True,
                costs=PaperCosts(),
            )
        closed_replay = commit_risk_reserved_for_isolated_test(
            engine, risk_engine, request_id=active_request_id
        )
        assert closed_replay.current_state == "COMPLETED"
        with engine.connect() as connection:
            position_status: str = connection.execute(
                text("SELECT status FROM paper_spot_positions WHERE trade_id=:id"),
                {"id": active_request_id},
            ).scalar_one()
        assert position_status == "closed"
        assert _ledger_counts(engine)[2] == after_commit[2] + 1

        # Run a new, small fixture intent in a child process. The coordinator
        # returns only after the PAPER transaction committed, but the explicit
        # test crash point leaves the durable state LEDGER_COMMITTED. Kill that
        # process before any recovery/completion call, then recover with a new
        # process engine and prove exactly one BUY exists.
        process_intent = replace(
            _costed_intent(),
            idempotency_key=f"process_commit_{uuid4().hex}",
            source_reference="process-crash-after-ledger-commit",
            quantity=Decimal("0.1"),
            order_notional_usdt=Decimal("10.005"),
            risk_amount_usdt=Decimal("0.1"),
            fee_cost_usdt=Decimal("0.010005"),
            spread_cost_usdt=Decimal("0.02001"),
            slippage_cost_usdt=Decimal("0.0050025"),
        )
        process_snapshot = capture_paper_account_evidence(
            engine, account_id=1, expected_settings_version=7
        )
        record_account_snapshot(
            risk_engine,
            snapshot_id=f"e3t3_snapshot_{uuid4().hex}",
            equity=Decimal(process_snapshot.payload["risk_equity_usdt"]),
            day_start_equity=Decimal(process_snapshot.payload["day_start_equity_usdt"]),
            peak_equity=Decimal(process_snapshot.payload["peak_equity_usdt"]),
            open_capital=Decimal(process_snapshot.payload["open_capital_usdt"]),
            open_positions=int(process_snapshot.payload["open_positions"]),
            consecutive_losses=int(process_snapshot.payload["consecutive_losses"]),
            source_sha256=process_snapshot.snapshot_sha256,
        )
        process_clients = _entry_observation_clients()
        process_record = create_evidenced_intent_for_test(
            engine,
            process_intent,
            candle_client=process_clients[0],
            book_client=process_clients[1],
            depth_client=process_clients[2],
        )
        assert process_record.current_state == "PREPARED"
        with engine.connect() as connection:
            process_spread_bps = Decimal(
                connection.execute(
                    text("SELECT spread_bps FROM paper_entry_market_evidence WHERE request_id=:id"),
                    {"id": process_record.request_id},
                ).scalar_one()
            )
        process_gross_edge = (
            Decimal("1") * Decimal(10_000) / Decimal("10.005")
            + Decimal("20")
            + process_spread_bps
            + Decimal("5")
        )
        process_risk_request = RiskRequest(
            settings_version=1,
            bot_state=BotState.RUNNING,
            proposed_notional=Decimal("10.005"),
            stop_loss_price=Decimal("99.05"),
            take_profit_price=Decimal("102"),
            reference_price=Decimal("100.05"),
            fee_bps_round_trip=Decimal("20"),
            spread_bps=process_spread_bps,
            slippage_bps=Decimal("5"),
            expected_gross_edge_bps=process_gross_edge,
            min_net_edge_bps=Decimal("0"),
            current_open_capital=Decimal(process_snapshot.payload["open_capital_usdt"]),
            open_positions=int(process_snapshot.payload["open_positions"]),
            equity=Decimal(process_snapshot.payload["risk_equity_usdt"]),
            day_start_equity=Decimal(process_snapshot.payload["day_start_equity_usdt"]),
            peak_equity=Decimal(process_snapshot.payload["peak_equity_usdt"]),
            consecutive_losses=int(process_snapshot.payload["consecutive_losses"]),
        )
        process_decision = evaluate_and_reserve(
            risk_engine,
            request_id=process_record.request_id,
            signal_id=process_record.request_id,
            profile_version=1,
            request=process_risk_request,
            intent_sha256=process_record.intent_sha256,
            paper_snapshot_sha256=process_snapshot.snapshot_sha256,
        )
        assert process_decision.allowed
        assert (
            record_verified_risk_reservation(
                engine, risk_engine, request_id=process_record.request_id
            ).current_state
            == "RISK_RESERVED"
        )
        child_app_url = make_url(os.environ["TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_commit_{uuid4().hex[:10]}"}
        )
        child_risk_url = make_url(os.environ["RISK_TEST_POSTGRES_URL"]).set(
            query={"application_name": f"e3t3_risk_{uuid4().hex[:10]}"}
        )
        child_marker = tmp_path / f"{uuid4().hex}.receipt"
        child_code = (
            "import os,time; from pathlib import Path; from sqlalchemy import create_engine; "
            "from binance_ai_trader.paper_entry_coordinator import "
            "commit_risk_reserved_for_isolated_test; "
            "app=create_engine(os.environ['TEST_POSTGRES_URL']); "
            "risk=create_engine(os.environ['RISK_TEST_POSTGRES_URL']); "
            "result=commit_risk_reserved_for_isolated_test(app,risk,"
            "request_id=os.environ['E3T3_COMMIT_REQUEST'],"
            "_simulate_crash_at='after_ledger_commit'); "
            "Path(os.environ['E3T3_COMMIT_MARKER']).write_text('LEDGER_COMMITTED'); "
            "time.sleep(60)"
        )
        commit_env = {
            **os.environ,
            "TEST_POSTGRES_URL": child_app_url.render_as_string(hide_password=False),
            "RISK_TEST_POSTGRES_URL": child_risk_url.render_as_string(hide_password=False),
            "E3T3_COMMIT_REQUEST": process_record.request_id,
            "E3T3_COMMIT_MARKER": str(child_marker),
        }
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            env=commit_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_marker(child, child_marker) == "LEDGER_COMMITTED"
            _kill_process(child)
        finally:
            if child.poll() is None:
                _kill_process(child)
        engine.dispose()
        process_restart = create_engine(URL, pool_pre_ping=True)
        try:
            committed_counts = _ledger_counts(process_restart)
            crash_restored = get_intent(process_restart, process_record.request_id)
            assert crash_restored is not None and crash_restored.current_state == "LEDGER_COMMITTED"
            assert (
                recover_committed_entry(
                    process_restart, request_id=process_record.request_id
                ).current_state
                == "COMPLETED"
            )
            assert _ledger_counts(process_restart) == committed_counts
            assert (
                commit_risk_reserved_for_isolated_test(
                    process_restart, risk_engine, request_id=process_record.request_id
                ).current_state
                == "COMPLETED"
            )
            assert _ledger_counts(process_restart) == committed_counts
            with Session(process_restart) as session:
                close_paper_spot(
                    session,
                    process_record.request_id,
                    Decimal("101"),
                    f"close_{uuid4().hex}",
                    explicit_approval=True,
                    costs=PaperCosts(),
                )
        finally:
            process_restart.dispose()

        for statement in (
            "UPDATE paper_entry_account_evidence SET snapshot_sha256='"
            + "f" * 64
            + f"' WHERE request_id='{record.request_id}'",
            f"DELETE FROM paper_entry_account_evidence WHERE request_id='{record.request_id}'",
            "TRUNCATE paper_entry_account_evidence",
            "UPDATE paper_entry_market_evidence SET cost_evidence_sha256='"
            + "f" * 64
            + f"' WHERE request_id='{record.request_id}'",
            f"DELETE FROM paper_entry_market_evidence WHERE request_id='{record.request_id}'",
            "TRUNCATE paper_entry_market_evidence",
            "UPDATE paper_entry_ledger_commits SET commit_sha256='"
            + "f" * 64
            + f"' WHERE request_id='{active_request_id}'",
            f"DELETE FROM paper_entry_ledger_commits WHERE request_id='{active_request_id}'",
            "TRUNCATE paper_entry_ledger_commits",
        ):
            with pytest.raises(DBAPIError), engine.begin() as connection:
                connection.execute(text(statement))
    finally:
        risk_engine.dispose()


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


def _wait_for_marker(process: subprocess.Popen[bytes], marker: Path) -> str:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if marker.exists():
            return marker.read_text(encoding="utf-8")
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"child exited before checkpoint: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(f"child checkpoint timed out: stdout={stdout!r}; stderr={stderr!r}")


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    # Popen.kill is SIGKILL on POSIX and TerminateProcess on Windows.
    process.kill()
    process.wait(timeout=10)
    assert process.returncode not in (None, 0)


def test_real_process_death_before_and_after_prepared_keeps_ledger_unchanged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _ledger_counts(engine)
    never_created_key = f"e3t3_before_{uuid4().hex}"
    marker = tmp_path / "before-intent.ready"
    code = (
        "import os,time; from pathlib import Path; "
        "Path(os.environ['E3T3_MARKER']).write_text(os.environ['E3T3_NEVER_CREATED_KEY']); "
        "time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "E3T3_MARKER": str(marker),
            "E3T3_NEVER_CREATED_KEY": never_created_key,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait_for_marker(child, marker) == never_created_key
    _kill_process(child)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM paper_entry_intents WHERE idempotency_key=:key"),
                {"key": never_created_key},
            ).scalar_one()
            == 0
        )

    worker_record = create_intent(
        engine,
        replace(
            _intent(f"e3t3_worker_{uuid4().hex}"),
            worker_id="worker-e3t3-test",
            worker_generation=7,
        ),
    )
    monkeypatch.setenv("PAPER_ENTRY_E3_TEST_ONLY", "1")
    monkeypatch.setenv("PAPER_ENTRY_COORDINATOR_TEST_ISOLATED", "1")
    monkeypatch.setenv("TRADEPILOT_PAPER_ENTRY_RISK_TEST_DB", "1")
    worker_risk_engine = create_engine(os.environ["RISK_TEST_POSTGRES_URL"])
    try:
        with pytest.raises(EntryCoordinatorRejected, match="worker_fencing_validation_unavailable"):
            commit_risk_reserved_for_isolated_test(
                engine, worker_risk_engine, request_id=worker_record.request_id
            )
    finally:
        worker_risk_engine.dispose()
    assert _ledger_counts(engine) == before

    payload = {
        "idempotency_key": f"e3t3_prepared_{uuid4().hex}",
        "source_type": "MANUAL",
        "source_reference": "process-crash-fixture",
        "risk_profile_version": 1,
        "paper_account_id": 1,
        "paper_settings_version": 7,
        "symbol": "BTCUSDT",
        "quantity": "0.125",
        "reference_price": "64000",
        "order_notional_usdt": "8000",
        "risk_amount_usdt": "80",
        "fee_cost_usdt": "8",
        "spread_cost_usdt": "2",
        "slippage_cost_usdt": "1",
        "expected_net_advantage_usdt": "12",
        "price_observed_at_ms": 1_800_000_000_000,
        "claimed_ledger_snapshot_sha256": "a" * 64,
        "claimed_cost_evidence_sha256": "b" * 64,
    }
    marker = tmp_path / "prepared.ready"
    code = (
        "import json,os,time; from decimal import Decimal; from pathlib import Path; "
        "from sqlalchemy import create_engine; "
        "from binance_ai_trader.paper_entry_coordinator import PaperEntryIntent,create_intent; "
        "data=json.loads(os.environ['E3T3_INTENT']); "
        "[data.__setitem__(key,Decimal(data[key])) for key in "
        "('quantity','reference_price','order_notional_usdt','risk_amount_usdt',"
        "'fee_cost_usdt','spread_cost_usdt','slippage_cost_usdt','expected_net_advantage_usdt')]; "
        "engine=create_engine(os.environ['TEST_POSTGRES_URL']); "
        "record=create_intent(engine,PaperEntryIntent(**data)); "
        "Path(os.environ['E3T3_MARKER']).write_text(record.request_id); "
        "time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "E3T3_INTENT": json.dumps(payload),
            "E3T3_MARKER": str(marker),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    request_id = _wait_for_marker(child, marker)
    _kill_process(child)
    engine.dispose()
    restarted = create_engine(URL, pool_pre_ping=True)
    try:
        restored = get_intent(restarted, request_id)
        assert restored is not None and restored.current_state == "PREPARED"
        assert [event.to_state for event in list_status_events(restarted, request_id)] == [
            "PREPARED"
        ]
        assert _ledger_counts(restarted) == before
    finally:
        restarted.dispose()


def test_real_risk_process_death_and_multiprocess_idempotency(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAPER_ENTRY_E3_TEST_ONLY", "1")
    monkeypatch.setenv("PAPER_ENTRY_COORDINATOR_TEST_ISOLATED", "1")
    monkeypatch.setenv("TRADEPILOT_PAPER_ENTRY_RISK_TEST_DB", "1")
    monkeypatch.setenv("PAPER_ENTRY_EVIDENCE_TEST_ONLY", "1")
    risk_engine = create_engine(os.environ["RISK_TEST_POSTGRES_URL"], pool_pre_ping=True)

    # Install a deliberately isolated fixture profile with room for multiple
    # crash/retry cases. This is test data in the disposable RiskGuard database,
    # not a user-selected or production risk setting.
    with risk_engine.connect() as connection:
        initial_runtime = (
            connection.execute(
                text(
                    "SELECT active_profile_version,revision,bot_state "
                    "FROM ai_risk_runtime WHERE id=1"
                )
            )
            .mappings()
            .one()
        )
    assert initial_runtime["bot_state"] == BotState.RUNNING.value
    stopped_revision = set_bot_state(
        risk_engine,
        state=BotState.STOPPED,
        expected_revision=int(initial_runtime["revision"]),
    )
    test_profile = RiskProfile(
        "1.0",
        int(initial_runtime["active_profile_version"]) + 1,
        1,
        10,
        Decimal("0.25"),
        Decimal("1000"),
        Decimal("1000"),
        Decimal("10"),
        Decimal("20"),
        10,
        (("fixture_test_cap", Decimal("1000")),),
    )
    assert (
        install_profile(
            risk_engine,
            test_profile,
            user_confirmed=True,
            expected_runtime_revision=stopped_revision,
        )
        == stopped_revision + 1
    )
    assert (
        set_bot_state(risk_engine, state=BotState.PAUSED, expected_revision=stopped_revision + 1)
        == stopped_revision + 2
    )
    assert (
        set_bot_state(risk_engine, state=BotState.RUNNING, expected_revision=stopped_revision + 2)
        == stopped_revision + 3
    )

    def save_real_account_snapshot(snapshot_id: str) -> tuple[str, dict[str, Any]]:
        evidence = capture_paper_account_evidence(engine, account_id=1, expected_settings_version=7)
        payload = evidence.payload
        record_account_snapshot(
            risk_engine,
            snapshot_id=snapshot_id,
            equity=Decimal(str(payload["risk_equity_usdt"])),
            day_start_equity=Decimal(str(payload["day_start_equity_usdt"])),
            peak_equity=Decimal(str(payload["peak_equity_usdt"])),
            open_capital=Decimal(str(payload["open_capital_usdt"])),
            open_positions=int(payload["open_positions"]),
            consecutive_losses=int(payload["consecutive_losses"]),
            source_sha256=evidence.snapshot_sha256,
        )
        return evidence.snapshot_sha256, payload

    with risk_engine.connect() as connection:
        runtime = (
            connection.execute(
                text(
                    "SELECT active_profile_version,revision,bot_state "
                    "FROM ai_risk_runtime WHERE id=1"
                )
            )
            .mappings()
            .one()
        )
    profile_version = int(runtime["active_profile_version"])
    assert runtime["bot_state"] == BotState.RUNNING.value

    def child_env(
        request_id: str,
        intent_sha256: str,
        snapshot_sha256: str,
        payload: dict[str, Any],
        marker: Path,
        application_name: str,
    ) -> dict[str, str]:
        return {
            **os.environ,
            "E3T3_REQUEST_ID": request_id,
            "E3T3_INTENT_SHA": intent_sha256,
            "E3T3_SNAPSHOT_SHA": snapshot_sha256,
            "E3T3_PROFILE_VERSION": str(profile_version),
            "E3T3_EQUITY": str(payload["risk_equity_usdt"]),
            "E3T3_DAY_EQUITY": str(payload["day_start_equity_usdt"]),
            "E3T3_PEAK_EQUITY": str(payload["peak_equity_usdt"]),
            "E3T3_OPEN_CAPITAL": str(payload["open_capital_usdt"]),
            "E3T3_OPEN_POSITIONS": str(payload["open_positions"]),
            "E3T3_LOSSES": str(payload["consecutive_losses"]),
            "E3T3_MARKER": str(marker),
            "E3T3_APP_NAME": application_name,
        }

    worker_code = """
import os,time
from decimal import Decimal
from pathlib import Path
from sqlalchemy import create_engine
from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.risk_guard import RiskRequest
from binance_ai_trader.risk_store import evaluate_and_reserve
engine=create_engine(os.environ['RISK_TEST_POSTGRES_URL'],connect_args={'application_name':os.environ['E3T3_APP_NAME']})
request=RiskRequest(settings_version=int(os.environ['E3T3_PROFILE_VERSION']),bot_state=BotState.RUNNING,
 proposed_notional=Decimal(os.environ.get('E3T3_NOTIONAL','10.005')),stop_loss_price=Decimal('99.05'),take_profit_price=Decimal('102'),
 reference_price=Decimal('100.05'),fee_bps_round_trip=Decimal('20'),spread_bps=Decimal('20'),
 slippage_bps=Decimal('10'),expected_gross_edge_bps=Decimal('100'),min_net_edge_bps=Decimal('0'),
 current_open_capital=Decimal(os.environ['E3T3_OPEN_CAPITAL']),open_positions=int(os.environ['E3T3_OPEN_POSITIONS']),
 equity=Decimal(os.environ['E3T3_EQUITY']),day_start_equity=Decimal(os.environ['E3T3_DAY_EQUITY']),
 peak_equity=Decimal(os.environ['E3T3_PEAK_EQUITY']),consecutive_losses=int(os.environ['E3T3_LOSSES']))
if 'E3T3_READY' in os.environ:
 Path(os.environ['E3T3_READY']).write_text('ready')
 while not Path(os.environ['E3T3_GO']).exists(): time.sleep(.005)
try:
 decision=evaluate_and_reserve(engine,request_id=os.environ['E3T3_REQUEST_ID'],signal_id=os.environ['E3T3_REQUEST_ID'],
  profile_version=int(os.environ['E3T3_PROFILE_VERSION']),request=request,intent_sha256=os.environ['E3T3_INTENT_SHA'],
  paper_snapshot_sha256=os.environ['E3T3_SNAPSHOT_SHA'])
 Path(os.environ['E3T3_MARKER']).write_text(str(decision.allowed))
except Exception as exc:
 Path(os.environ['E3T3_MARKER']).write_text(type(exc).__name__+':'+str(exc))
 if os.environ.get('E3T3_EXPECT_CONFLICT') != '1': raise
time.sleep(60)
"""

    # A real process is killed while evaluate_and_reserve is blocked on the
    # independent store. PostgreSQL must roll back its open transaction.
    blocked_record = create_intent(engine, _intent(f"e3t3_during_{uuid4().hex}"))
    blocked_snapshot, blocked_payload = save_real_account_snapshot(blocked_record.request_id)
    blocker = risk_engine.connect()
    blocker_transaction = blocker.begin()
    blocker.execute(text("LOCK TABLE ai_risk_audit IN ACCESS EXCLUSIVE MODE"))
    app_name = f"e3t3_block_{uuid4().hex[:12]}"
    never_done = tmp_path / "risk-block-done"
    ready = tmp_path / "risk-block-ready"
    go = tmp_path / "risk-block-go"
    blocked_env = child_env(
        blocked_record.request_id,
        blocked_record.intent_sha256,
        blocked_snapshot,
        blocked_payload,
        never_done,
        app_name,
    )
    blocked_env.update({"E3T3_READY": str(ready), "E3T3_GO": str(go)})
    child = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        env=blocked_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(child, ready)
        go.write_text("go", encoding="utf-8")
        deadline = time.monotonic() + 15
        waiting = False
        while time.monotonic() < deadline:
            with risk_engine.connect() as connection:
                waiting = bool(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:name "
                            "AND wait_event_type='Lock'"
                        ),
                        {"name": app_name},
                    ).scalar_one()
                )
            if waiting:
                break
            if child.poll() is not None:
                break
            time.sleep(0.01)
        assert waiting, "child must be observed blocked inside the RiskGuard transaction"
        _kill_process(child)
    finally:
        blocker_transaction.rollback()
        blocker.close()
    with risk_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM ai_risk_audit WHERE event_id=:id"),
                {"id": blocked_record.request_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM ai_risk_reservation_events WHERE reservation_id=:id"),
                {"id": blocked_record.request_id},
            ).scalar_one()
            == 0
        )
    assert get_intent(engine, blocked_record.request_id).current_state == "PREPARED"  # type: ignore[union-attr]

    # A second child commits the RiskGuard audit and reservation, acknowledges
    # completion, then is killed before app-side RISK_RESERVED recording.
    held_record = create_intent(engine, _intent(f"e3t3_held_{uuid4().hex}"))
    held_snapshot, held_payload = save_real_account_snapshot(held_record.request_id)
    held_marker = tmp_path / "risk-reservation-committed"
    held_name = f"e3t3_held_{uuid4().hex[:12]}"
    held_child = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        env=child_env(
            held_record.request_id,
            held_record.intent_sha256,
            held_snapshot,
            held_payload,
            held_marker,
            held_name,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert _wait_for_marker(held_child, held_marker) == "True"
    _kill_process(held_child)

    risk_engine.dispose()
    restarted_risk = create_engine(os.environ["RISK_TEST_POSTGRES_URL"], pool_pre_ping=True)
    try:
        from binance_ai_trader.risk_store import get_verified_reservation

        verified = get_verified_reservation(
            restarted_risk,
            request_id=held_record.request_id,
            signal_id=held_record.request_id,
            profile_version=profile_version,
            intent_sha256=held_record.intent_sha256,
            paper_snapshot_sha256=held_snapshot,
        )
        assert verified.amount_usdt == Decimal("10.005")
        with restarted_risk.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id=:id AND event_type='RESERVE'"
                    ),
                    {"id": held_record.request_id},
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id=:id AND event_type='RELEASE'"
                    ),
                    {"id": held_record.request_id},
                ).scalar_one()
                == 0
            )
        assert get_intent(engine, held_record.request_id).current_state == "PREPARED"  # type: ignore[union-attr]
    finally:
        restarted_risk.dispose()

    # Multiple OS processes retry the same request after restart. The risk
    # store must return the original decision and preserve one audit/reserve.
    concurrency_ready = [tmp_path / f"risk-retry-{i}.ready" for i in range(2)]
    concurrency_done = [tmp_path / f"risk-retry-{i}.done" for i in range(2)]
    concurrency_go = tmp_path / "risk-retry.go"
    retry_processes: list[subprocess.Popen[bytes]] = []
    for index in range(2):
        env = child_env(
            held_record.request_id,
            held_record.intent_sha256,
            held_snapshot,
            held_payload,
            concurrency_done[index],
            f"e3t3_retry_{index}_{uuid4().hex[:8]}",
        )
        env["E3T3_READY"] = str(concurrency_ready[index])
        env["E3T3_GO"] = str(concurrency_go)
        retry_processes.append(
            subprocess.Popen(
                [sys.executable, "-c", worker_code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    try:
        for process, ready_marker in zip(retry_processes, concurrency_ready, strict=True):
            assert _wait_for_marker(process, ready_marker) == "ready"
        concurrency_go.write_text("go", encoding="utf-8")
        for process, done_marker in zip(retry_processes, concurrency_done, strict=True):
            assert _wait_for_marker(process, done_marker) == "True"
        for process in retry_processes:
            _kill_process(process)
    finally:
        for process in retry_processes:
            if process.poll() is None:
                _kill_process(process)

    conflict_marker = tmp_path / "risk-retry-conflict.done"
    conflict_env = child_env(
        held_record.request_id,
        held_record.intent_sha256,
        held_snapshot,
        held_payload,
        conflict_marker,
        f"e3t3_conflict_{uuid4().hex[:8]}",
    )
    conflict_env["E3T3_NOTIONAL"] = "11.005"
    conflict_env["E3T3_EXPECT_CONFLICT"] = "1"
    conflict_process = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        env=conflict_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert (
            _wait_for_marker(conflict_process, conflict_marker)
            == "RiskStoreRejected:reservation_id_payload_conflict"
        )
        _kill_process(conflict_process)
    finally:
        if conflict_process.poll() is None:
            _kill_process(conflict_process)

    # Refuse new application-DB connections, terminate existing app sessions,
    # and prove the orphaned pre-entry remains inert while the independent
    # RiskGuard hold is retained. The isolated test control database is the
    # only connection used to restore availability.
    admin_url = make_url(URL).set(database="trader_test")
    admin_engine = create_engine(admin_url)
    app_counts = _ledger_counts(engine)
    engine.dispose()
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER DATABASE trader_paper_entry_coordinator_test "
                    "WITH ALLOW_CONNECTIONS false"
                )
            )
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname='trader_paper_entry_coordinator_test' "
                    "AND pid<>pg_backend_pid()"
                )
            )
        unavailable_app = create_engine(URL, pool_pre_ping=True)
        try:
            with pytest.raises(DBAPIError), unavailable_app.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            unavailable_app.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER DATABASE trader_paper_entry_coordinator_test WITH ALLOW_CONNECTIONS true"
                )
            )
        admin_engine.dispose()
    app_restart = create_engine(URL, pool_pre_ping=True)
    assert _ledger_counts(app_restart) == app_counts
    assert get_intent(app_restart, held_record.request_id).current_state == "PREPARED"  # type: ignore[union-attr]
    app_restart.dispose()

    # Repeat with the independent RiskGuard database down. The outage is a real
    # PostgreSQL connection refusal; no release is issued and the reservation
    # is readable again after the isolated database is restored.
    risk_engine.dispose()
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                text("ALTER DATABASE trader_paper_entry_risk_test WITH ALLOW_CONNECTIONS false")
            )
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname='trader_paper_entry_risk_test' "
                    "AND pid<>pg_backend_pid()"
                )
            )
        unavailable_risk = create_engine(os.environ["RISK_TEST_POSTGRES_URL"])
        try:
            with pytest.raises(DBAPIError), unavailable_risk.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            unavailable_risk.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                text("ALTER DATABASE trader_paper_entry_risk_test WITH ALLOW_CONNECTIONS true")
            )
        admin_engine.dispose()
    risk_restart = create_engine(os.environ["RISK_TEST_POSTGRES_URL"], pool_pre_ping=True)
    try:
        from binance_ai_trader.risk_store import get_verified_reservation

        get_verified_reservation(
            risk_restart,
            request_id=held_record.request_id,
            signal_id=held_record.request_id,
            profile_version=profile_version,
            intent_sha256=held_record.intent_sha256,
            paper_snapshot_sha256=held_snapshot,
        )
        with risk_restart.connect() as connection:
            current_revision = int(
                connection.execute(
                    text("SELECT revision FROM ai_risk_runtime WHERE id=1")
                ).scalar_one()
            )
        paused_revision = set_bot_state(
            risk_restart, state=BotState.PAUSED, expected_revision=current_revision
        )
        with pytest.raises(RiskStoreRejected, match="reservation_profile_or_bot_state_stale"):
            get_verified_reservation(
                risk_restart,
                request_id=held_record.request_id,
                signal_id=held_record.request_id,
                profile_version=profile_version,
                intent_sha256=held_record.intent_sha256,
                paper_snapshot_sha256=held_snapshot,
            )
        stopped_again = set_bot_state(
            risk_restart, state=BotState.STOPPED, expected_revision=paused_revision
        )
        next_profile = replace(test_profile, profile_version=profile_version + 1)
        assert (
            install_profile(
                risk_restart,
                next_profile,
                user_confirmed=True,
                expected_runtime_revision=stopped_again,
            )
            == stopped_again + 1
        )
        paused_again = set_bot_state(
            risk_restart, state=BotState.PAUSED, expected_revision=stopped_again + 1
        )
        set_bot_state(risk_restart, state=BotState.RUNNING, expected_revision=paused_again)
        with pytest.raises(RiskStoreRejected, match="reservation_profile_or_bot_state_stale"):
            get_verified_reservation(
                risk_restart,
                request_id=held_record.request_id,
                signal_id=held_record.request_id,
                profile_version=profile_version,
                intent_sha256=held_record.intent_sha256,
                paper_snapshot_sha256=held_snapshot,
            )
        with risk_restart.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM ai_risk_reservation_events "
                        "WHERE reservation_id=:id AND event_type='RELEASE'"
                    ),
                    {"id": held_record.request_id},
                ).scalar_one()
                == 0
            )
    finally:
        risk_restart.dispose()
