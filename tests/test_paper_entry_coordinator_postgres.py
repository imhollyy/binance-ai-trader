"""Disposable PostgreSQL tests for durable, inert PAPER entry coordination."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
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
    clock = [time.time_ns() // 1_000_000]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        clock[0] += delay_ms
        if path == "/api/v3/klines":
            last_open_ms = (clock[0] // 60_000) * 60_000 - 60_000
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
        return clock[0]

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
    engine: Engine, monkeypatch: pytest.MonkeyPatch
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
        reserved = record_verified_risk_reservation(
            engine, risk_engine, request_id=record.request_id
        )
        assert reserved.current_state == "RISK_RESERVED"
        competing_reserved = record_verified_risk_reservation(
            engine, risk_engine, request_id=competing_record.request_id
        )
        assert competing_reserved.current_state == "RISK_RESERVED"
        assert _ledger_counts(engine) == before
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
            + f"' WHERE request_id='{record.request_id}'",
            f"DELETE FROM paper_entry_ledger_commits WHERE request_id='{record.request_id}'",
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
