"""Durable PAPER entry intent journal; deliberately performs no trading writes.

Intent creation is inert. A separate explicit method can record a RISK_RESERVED
event only after read-back verification from the isolated RiskGuard database.
This module never mutates the PAPER ledger or authorizes a BUY.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from binance_ai_trader.paper_entry_recovery import (
    PaperLedgerEvidence,
    RecoveryAssessment,
    RiskReservationEvidence,
    assess_entry_recovery,
)

_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TYPES = frozenset({"MANUAL", "AI", "OBSERVED", "SIGNAL", "RESEARCH"})
_STATES = frozenset(
    {
        "PREPARED",
        "RISK_RESERVED",
        "LEDGER_COMMITTED",
        "RECONCILIATION_REQUIRED",
        "COMPLETED",
        "REJECTED",
    }
)
_TRANSITIONS: dict[str, frozenset[str]] = {
    "PREPARED": frozenset({"RISK_RESERVED", "RECONCILIATION_REQUIRED", "REJECTED"}),
    "RISK_RESERVED": frozenset({"LEDGER_COMMITTED", "RECONCILIATION_REQUIRED"}),
    "LEDGER_COMMITTED": frozenset({"COMPLETED", "RECONCILIATION_REQUIRED"}),
    # E1 does not attest cross-store recovery. Ambiguous intents remain blocked
    # until a later independently verified reconciliation contract exists.
    "RECONCILIATION_REQUIRED": frozenset(),
    "COMPLETED": frozenset(),
    "REJECTED": frozenset(),
}
_REASON = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class EntryCoordinatorRejected(ValueError):
    """An invalid intent or state transition was rejected."""


@dataclass(frozen=True, slots=True)
class PaperEntryIntent:
    idempotency_key: str
    source_type: str
    source_reference: str
    risk_profile_version: int
    paper_account_id: int
    paper_settings_version: int
    symbol: str
    quantity: Decimal
    reference_price: Decimal
    order_notional_usdt: Decimal
    risk_amount_usdt: Decimal
    fee_cost_usdt: Decimal
    spread_cost_usdt: Decimal
    slippage_cost_usdt: Decimal
    expected_net_advantage_usdt: Decimal
    price_observed_at_ms: int
    claimed_ledger_snapshot_sha256: str
    claimed_cost_evidence_sha256: str
    worker_id: str | None = None
    worker_generation: int | None = None
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class EntryIntentRecord:
    request_id: str
    idempotency_key: str
    intent_sha256: str
    current_state: str
    risk_profile_version: int
    paper_account_id: int
    paper_settings_version: int
    created_at: Any


@dataclass(frozen=True, slots=True)
class EntryStatusEvent:
    event_id: str
    request_id: str
    sequence: int
    from_state: str | None
    to_state: str
    reason_code: str
    evidence_reference: str | None
    recorded_at: Any


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    intent: EntryIntentRecord
    latest_event: EntryStatusEvent
    new_entry_authorized: bool = False
    automatic_reservation_release_allowed: bool = False
    manual_reconciliation_required: bool = True


def _decimal(
    value: Decimal, name: str, *, positive: bool = False, allow_negative: bool = False
) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise EntryCoordinatorRejected(f"invalid_{name}")
    if positive and value <= 0:
        raise EntryCoordinatorRejected(f"invalid_{name}")
    if not positive and not allow_negative and value < 0:
        raise EntryCoordinatorRejected(f"invalid_{name}")
    # NUMERIC(38,18) stores at most 20 integer and 18 fractional digits. Reject
    # excess precision rather than rounding financial evidence silently.
    normalized = value.normalize()
    integer_digits = max(normalized.adjusted() + 1, 0) if normalized else 0
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        raise EntryCoordinatorRejected(f"invalid_{name}")
    fractional_digits = max(-exponent, 0) if normalized else 0
    if integer_digits > 20 or fractional_digits > 18:
        raise EntryCoordinatorRejected(f"out_of_range_{name}")
    return "0" if value == 0 else format(normalized, "f")


def _validate(intent: PaperEntryIntent) -> dict[str, Any]:
    if _IDEMPOTENCY.fullmatch(intent.idempotency_key) is None:
        raise EntryCoordinatorRejected("invalid_idempotency_key")
    if intent.source_type not in _SOURCE_TYPES:
        raise EntryCoordinatorRejected("invalid_source_type")
    if not intent.source_reference or len(intent.source_reference) > 128:
        raise EntryCoordinatorRejected("invalid_source_reference")
    if (intent.source_type == "AI") != (intent.model_version is not None):
        raise EntryCoordinatorRejected("invalid_model_provenance")
    if intent.model_version is not None and (
        not intent.model_version or len(intent.model_version) > 128
    ):
        raise EntryCoordinatorRejected("invalid_model_version")
    if type(intent.risk_profile_version) is not int or intent.risk_profile_version < 1:
        raise EntryCoordinatorRejected("invalid_risk_profile_version")
    if type(intent.paper_account_id) is not int or intent.paper_account_id < 1:
        raise EntryCoordinatorRejected("invalid_paper_account_id")
    if type(intent.paper_settings_version) is not int or intent.paper_settings_version < 1:
        raise EntryCoordinatorRejected("invalid_paper_settings_version")
    if intent.symbol not in {"BTCUSDT", "ETHUSDT"}:
        raise EntryCoordinatorRejected("unsupported_symbol")
    if type(intent.price_observed_at_ms) is not int or intent.price_observed_at_ms <= 0:
        raise EntryCoordinatorRejected("invalid_price_time")
    for name in ("claimed_ledger_snapshot_sha256", "claimed_cost_evidence_sha256"):
        if _HASH.fullmatch(getattr(intent, name)) is None:
            raise EntryCoordinatorRejected(f"invalid_{name}")
    if intent.worker_id is None:
        if intent.worker_generation is not None:
            raise EntryCoordinatorRejected("invalid_worker_fence")
    elif (
        not intent.worker_id
        or len(intent.worker_id) > 36
        or type(intent.worker_generation) is not int
        or intent.worker_generation < 1
    ):
        raise EntryCoordinatorRejected("invalid_worker_fence")

    amounts = {
        "quantity": _decimal(intent.quantity, "quantity", positive=True),
        "reference_price": _decimal(intent.reference_price, "reference_price", positive=True),
        "order_notional_usdt": _decimal(
            intent.order_notional_usdt, "order_notional_usdt", positive=True
        ),
        "risk_amount_usdt": _decimal(intent.risk_amount_usdt, "risk_amount_usdt", positive=True),
        "fee_cost_usdt": _decimal(intent.fee_cost_usdt, "fee_cost_usdt"),
        "spread_cost_usdt": _decimal(intent.spread_cost_usdt, "spread_cost_usdt"),
        "slippage_cost_usdt": _decimal(intent.slippage_cost_usdt, "slippage_cost_usdt"),
        "expected_net_advantage_usdt": _decimal(
            intent.expected_net_advantage_usdt,
            "expected_net_advantage_usdt",
            allow_negative=True,
        ),
    }
    return {
        "idempotency_key": intent.idempotency_key,
        "source_type": intent.source_type,
        "source_reference": intent.source_reference,
        "model_version": intent.model_version,
        "risk_profile_version": intent.risk_profile_version,
        "paper_account_id": intent.paper_account_id,
        "paper_settings_version": intent.paper_settings_version,
        "symbol": intent.symbol,
        **amounts,
        "price_observed_at_ms": intent.price_observed_at_ms,
        "claimed_ledger_snapshot_sha256": intent.claimed_ledger_snapshot_sha256,
        "claimed_cost_evidence_sha256": intent.claimed_cost_evidence_sha256,
        "worker_id": intent.worker_id,
        "worker_generation": intent.worker_generation,
    }


def _require_postgresql(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise EntryCoordinatorRejected("postgresql_store_required")


def _latest_event(session: Session, request_id: str) -> EntryStatusEvent:
    row = (
        session.execute(
            text(
                "SELECT event_id,request_id,sequence,from_state,to_state,reason_code,"
                "evidence_reference,recorded_at FROM paper_entry_intent_events "
                "WHERE request_id=:request_id ORDER BY sequence DESC LIMIT 1"
            ),
            {"request_id": request_id},
        )
        .mappings()
        .one()
    )
    return EntryStatusEvent(**dict(row))


def _record(session: Session, request_id: str) -> EntryIntentRecord:
    row = (
        session.execute(
            text(
                "SELECT i.request_id,i.idempotency_key,i.intent_sha256,"
                "i.risk_profile_version,i.paper_account_id,i.paper_settings_version,"
                "i.created_at,e.to_state AS current_state "
                "FROM paper_entry_intents i JOIN paper_entry_intent_events e "
                "ON e.request_id=i.request_id WHERE i.request_id=:request_id "
                "AND e.sequence=(SELECT max(e2.sequence) FROM paper_entry_intent_events e2 "
                "WHERE e2.request_id=i.request_id)"
            ),
            {"request_id": request_id},
        )
        .mappings()
        .one()
    )
    return EntryIntentRecord(**dict(row))


def create_intent(
    engine: Engine,
    intent: PaperEntryIntent,
    *,
    _snapshot_evidence: Any | None = None,
    _cost_evidence: Any | None = None,
) -> EntryIntentRecord:
    """Persist immutable intent and PREPARED event idempotently; no side effects."""
    _require_postgresql(engine)
    normalized = _validate(intent)
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    request_id = str(uuid4())
    params = {
        **normalized,
        "request_id": request_id,
        "intent_sha256": digest,
        "intent_payload": serialized,
    }
    with Session(engine) as session, session.begin():
        existing = (
            session.execute(
                text(
                    "SELECT request_id,intent_sha256 FROM paper_entry_intents "
                    "WHERE idempotency_key=:key FOR UPDATE"
                ),
                {"key": intent.idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["intent_sha256"] != digest:
                raise EntryCoordinatorRejected("idempotency_key_payload_conflict")
            return _record(session, str(existing["request_id"]))

        account = (
            session.execute(
                text(
                    "SELECT mode,market_type,settings_version FROM paper_spot_accounts "
                    "WHERE id=:account_id FOR SHARE"
                ),
                {
                    "account_id": intent.paper_account_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if account is None or account["mode"] != "paper" or account["market_type"] != "spot":
            raise EntryCoordinatorRejected("paper_account_unavailable")
        if int(account["settings_version"]) != intent.paper_settings_version:
            raise EntryCoordinatorRejected("stale_paper_settings_version")
        if _snapshot_evidence is None and _cost_evidence is not None:
            raise EntryCoordinatorRejected("complete_pre_entry_evidence_required")
        if _snapshot_evidence is not None:
            if _cost_evidence is None:
                raise EntryCoordinatorRejected("complete_pre_entry_evidence_required")
            from binance_ai_trader.paper_entry_evidence import (
                EntryEvidenceRejected,
                paper_account_evidence_in_session,
            )

            try:
                current_evidence = paper_account_evidence_in_session(
                    session,
                    account_id=intent.paper_account_id,
                    expected_settings_version=intent.paper_settings_version,
                )
            except EntryEvidenceRejected as exc:
                raise EntryCoordinatorRejected(str(exc)) from exc
            if (
                current_evidence.snapshot_sha256 != intent.claimed_ledger_snapshot_sha256
                or current_evidence.snapshot_sha256 != _snapshot_evidence.snapshot_sha256
                or _cost_evidence.evidence_sha256 != intent.claimed_cost_evidence_sha256
            ):
                raise EntryCoordinatorRejected("pre_entry_evidence_hash_mismatch")
        inserted_request_id = session.execute(
            text(
                "INSERT INTO paper_entry_intents (request_id,idempotency_key,intent_sha256,"
                "intent_payload,source_type,source_reference,model_version,risk_profile_version,"
                "paper_account_id,paper_settings_version,symbol,quantity,reference_price,"
                "order_notional_usdt,risk_amount_usdt,fee_cost_usdt,spread_cost_usdt,"
                "slippage_cost_usdt,expected_net_advantage_usdt,price_observed_at_ms,"
                "claimed_ledger_snapshot_sha256,claimed_cost_evidence_sha256,"
                "worker_id,worker_generation) "
                "VALUES (:request_id,:idempotency_key,:intent_sha256,CAST(:intent_payload AS json),"
                ":source_type,:source_reference,:model_version,:risk_profile_version,"
                ":paper_account_id,:paper_settings_version,:symbol,:quantity,:reference_price,"
                ":order_notional_usdt,:risk_amount_usdt,:fee_cost_usdt,:spread_cost_usdt,"
                ":slippage_cost_usdt,:expected_net_advantage_usdt,:price_observed_at_ms,"
                ":claimed_ledger_snapshot_sha256,:claimed_cost_evidence_sha256,"
                ":worker_id,:worker_generation) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING request_id"
            ),
            params,
        ).scalar_one_or_none()
        if inserted_request_id is not None:
            session.execute(
                text(
                    "INSERT INTO paper_entry_intent_events "
                    "(event_id,request_id,sequence,from_state,to_state,reason_code) "
                    "VALUES (:event_id,:request_id,1,NULL,'PREPARED','intent_persisted')"
                ),
                {"event_id": str(uuid4()), "request_id": request_id},
            )
            if _snapshot_evidence is not None:
                from binance_ai_trader.models import (
                    PaperEntryAccountEvidenceRow,
                    PaperEntryMarketEvidenceRow,
                )

                server_now_ms = int(
                    session.execute(
                        text("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")
                    ).scalar_one()
                )
                if not 0 <= server_now_ms - _cost_evidence.observed_at_ms <= 3_000:
                    raise EntryCoordinatorRejected("stale_pre_entry_market_evidence")
                session.add(
                    PaperEntryAccountEvidenceRow(
                        request_id=request_id,
                        account_id=_snapshot_evidence.account_id,
                        settings_version=_snapshot_evidence.settings_version,
                        journal_head_id=_snapshot_evidence.journal_head_id,
                        journal_count=_snapshot_evidence.journal_count,
                        snapshot_sha256=_snapshot_evidence.snapshot_sha256,
                        evidence_payload=_snapshot_evidence.payload,
                    )
                )
                cost = _cost_evidence
                payload = cost.payload
                session.add(
                    PaperEntryMarketEvidenceRow(
                        request_id=request_id,
                        symbol=intent.symbol,
                        interval="1m",
                        source="binance_public_spot_coherence",
                        observed_at_ms=cost.observed_at_ms,
                        reference_price=cost.reference_price,
                        bid_price=cost.bid_price,
                        ask_price=cost.ask_price,
                        spread_bps=cost.spread_bps,
                        fee_assumption_bps=payload["fee_assumption_bps"],
                        fee_assumption_version=payload["fee_assumption_version"],
                        slippage_assumption_bps=payload["slippage_assumption_bps"],
                        slippage_assumption_version=payload["slippage_assumption_version"],
                        expected_net_advantage_usdt=intent.expected_net_advantage_usdt,
                        cost_evidence_sha256=cost.evidence_sha256,
                        evidence_payload=payload,
                    )
                )
                session.flush()
            return _record(session, request_id)

        existing = (
            session.execute(
                text(
                    "SELECT request_id,intent_sha256 FROM paper_entry_intents "
                    "WHERE idempotency_key=:key FOR UPDATE"
                ),
                {"key": intent.idempotency_key},
            )
            .mappings()
            .one()
        )
        if existing["intent_sha256"] != digest:
            raise EntryCoordinatorRejected("idempotency_key_payload_conflict")
        return _record(session, str(existing["request_id"]))


def create_evidenced_intent(
    engine: Engine,
    intent: PaperEntryIntent,
) -> EntryIntentRecord:
    """Create evidence only through default, server-owned public clients."""
    from binance_ai_trader.public_spot_cost_review import PublicSpotCostClient
    from binance_ai_trader.public_spot_depth_review import PublicSpotDepthClient
    from binance_ai_trader.public_spot_observation import PublicSpotObservationClient

    return _create_evidenced_intent(
        engine,
        intent,
        candle_client=PublicSpotObservationClient(),
        book_client=PublicSpotCostClient(),
        depth_client=PublicSpotDepthClient(),
    )


def create_evidenced_intent_for_test(
    engine: Engine,
    intent: PaperEntryIntent,
    *,
    candle_client: Any,
    book_client: Any,
    depth_client: Any,
) -> EntryIntentRecord:
    """Allow mock transports only against the explicitly isolated disposable DB."""
    test_url = os.environ.get("TEST_POSTGRES_URL", "")
    test_flag = os.environ.get("PAPER_ENTRY_EVIDENCE_TEST_ONLY") == "1"
    try:
        url = make_url(test_url)
        engine_url = make_url(engine.url)
    except (ValueError, TypeError) as exc:
        raise EntryCoordinatorRejected("isolated_evidence_test_database_required") from exc
    if not (
        test_flag
        and url.get_backend_name() == "postgresql"
        and url.host in {"127.0.0.1", "localhost"}
        and url.port == 55444
        and url.database == "trader_paper_entry_coordinator_test"
        and engine_url.host == url.host
        and engine_url.port == url.port
        and engine_url.database == url.database
    ):
        raise EntryCoordinatorRejected("isolated_evidence_test_database_required")
    return _create_evidenced_intent(
        engine,
        intent,
        candle_client=candle_client,
        book_client=book_client,
        depth_client=depth_client,
    )


def _create_evidenced_intent(
    engine: Engine,
    intent: PaperEntryIntent,
    *,
    candle_client: Any,
    book_client: Any,
    depth_client: Any,
) -> EntryIntentRecord:
    """Hash current ledger and public feeds; this never authorizes a trade.

    Only the default-client production entrypoint or test-gated fixture entrypoint
    can invoke this helper.
    """
    from binance_ai_trader.paper_entry_evidence import (
        EntryEvidenceRejected,
        capture_paper_account_evidence,
        collect_pre_entry_cost_evidence,
    )

    _require_postgresql(engine)
    try:
        cost = collect_pre_entry_cost_evidence(
            intent,
            candle_client=candle_client,
            book_client=book_client,
            depth_client=depth_client,
        )
        snapshot = capture_paper_account_evidence(
            engine,
            account_id=intent.paper_account_id,
            expected_settings_version=intent.paper_settings_version,
        )
    except EntryEvidenceRejected as exc:
        raise EntryCoordinatorRejected(str(exc)) from exc
    evidenced_intent = replace(
        intent,
        claimed_ledger_snapshot_sha256=snapshot.snapshot_sha256,
        claimed_cost_evidence_sha256=cost.evidence_sha256,
    )
    return create_intent(
        engine,
        evidenced_intent,
        _snapshot_evidence=snapshot,
        _cost_evidence=cost,
    )


def get_intent(engine: Engine, request_id: str) -> EntryIntentRecord | None:
    _require_postgresql(engine)
    with Session(engine) as session:
        exists = session.execute(
            text("SELECT 1 FROM paper_entry_intents WHERE request_id=:request_id"),
            {"request_id": request_id},
        ).scalar_one_or_none()
        return _record(session, request_id) if exists else None


def list_status_events(engine: Engine, request_id: str) -> tuple[EntryStatusEvent, ...]:
    _require_postgresql(engine)
    with Session(engine) as session:
        rows = (
            session.execute(
                text(
                    "SELECT event_id,request_id,sequence,from_state,to_state,reason_code,"
                    "evidence_reference,recorded_at FROM paper_entry_intent_events "
                    "WHERE request_id=:request_id ORDER BY sequence"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .all()
        )
    return tuple(EntryStatusEvent(**dict(row)) for row in rows)


def transition_intent(
    engine: Engine,
    *,
    request_id: str,
    expected_state: str,
    new_state: str,
    reason_code: str,
    evidence_reference: str | None = None,
) -> EntryIntentRecord:
    """Append one allowed state transition under an intent-row lock."""
    _require_postgresql(engine)
    if expected_state not in _STATES or new_state not in _STATES:
        raise EntryCoordinatorRejected("invalid_entry_state")
    if new_state not in _TRANSITIONS[expected_state]:
        raise EntryCoordinatorRejected("invalid_entry_state_transition")
    if new_state == "RISK_RESERVED":
        raise EntryCoordinatorRejected("risk_reservation_requires_independent_store_verification")
    if _REASON.fullmatch(reason_code) is None:
        raise EntryCoordinatorRejected("invalid_reason_code")
    if evidence_reference is not None and (not evidence_reference or len(evidence_reference) > 256):
        raise EntryCoordinatorRejected("invalid_evidence_reference")
    if new_state in {"RISK_RESERVED", "LEDGER_COMMITTED", "COMPLETED"} and (
        evidence_reference is None
    ):
        raise EntryCoordinatorRejected("state_transition_requires_evidence_reference")
    with Session(engine) as session, session.begin():
        intent_row = session.execute(
            text(
                "SELECT request_id FROM paper_entry_intents WHERE request_id=:request_id FOR UPDATE"
            ),
            {"request_id": request_id},
        ).one_or_none()
        if intent_row is None:
            raise EntryCoordinatorRejected("entry_intent_not_found")
        current = _latest_event(session, request_id)
        if current.to_state != expected_state:
            raise EntryCoordinatorRejected("stale_entry_intent_state")
        session.execute(
            text(
                "INSERT INTO paper_entry_intent_events "
                "(event_id,request_id,sequence,from_state,to_state,reason_code,evidence_reference) "
                "VALUES (:event_id,:request_id,:sequence,:from_state,:to_state,:reason,:evidence)"
            ),
            {
                "event_id": str(uuid4()),
                "request_id": request_id,
                "sequence": current.sequence + 1,
                "from_state": current.to_state,
                "to_state": new_state,
                "reason": reason_code,
                "evidence": evidence_reference,
            },
        )
        return _record(session, request_id)


def record_verified_risk_reservation(
    engine: Engine, risk_engine: Engine, *, request_id: str
) -> EntryIntentRecord:
    """Verify current ledger, stored public cost receipt and independent risk DB.

    This only records a RiskGuard reservation. It never creates or authorizes BUY.
    The app DB row locks remain held across the independent read, so ledger writes
    cannot race the snapshot check. A cross-DB failure leaves no app transition.
    """
    from binance_ai_trader.paper_entry_evidence import (
        EntryEvidenceRejected,
        paper_account_evidence_in_session,
    )
    from binance_ai_trader.risk_store import (
        RiskStoreRejected,
        get_verified_reservation,
    )

    _require_postgresql(engine)
    with Session(engine) as session, session.begin():
        row = (
            session.execute(
                text(
                    "SELECT i.intent_sha256,i.risk_profile_version,i.source_type,"
                    "i.paper_account_id,i.paper_settings_version,i.symbol,i.reference_price,"
                    "i.order_notional_usdt,i.risk_amount_usdt,i.fee_cost_usdt,"
                    "i.spread_cost_usdt,i.slippage_cost_usdt,i.expected_net_advantage_usdt,"
                    "i.claimed_ledger_snapshot_sha256,i.claimed_cost_evidence_sha256,"
                    "e.to_state FROM paper_entry_intents i JOIN paper_entry_intent_events e "
                    "ON e.request_id=i.request_id WHERE i.request_id=:id "
                    "AND e.sequence=(SELECT max(e2.sequence) FROM paper_entry_intent_events e2 "
                    "WHERE e2.request_id=i.request_id) FOR UPDATE OF i"
                ),
                {"id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise EntryCoordinatorRejected("entry_intent_not_found")
        if row["to_state"] not in {"PREPARED", "RISK_RESERVED"}:
            raise EntryCoordinatorRejected("stale_entry_intent_state")
        if row["source_type"] == "AI":
            raise EntryCoordinatorRejected("unapproved_ai_model_cannot_reserve")
        try:
            snapshot = paper_account_evidence_in_session(
                session,
                account_id=int(row["paper_account_id"]),
                expected_settings_version=int(row["paper_settings_version"]),
            )
        except EntryEvidenceRejected as exc:
            raise EntryCoordinatorRejected(str(exc)) from exc
        account_receipt = (
            session.execute(
                text(
                    "SELECT account_id,settings_version,journal_head_id,journal_count,"
                    "snapshot_sha256,evidence_payload FROM paper_entry_account_evidence "
                    "WHERE request_id=:id"
                ),
                {"id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        cost_receipt = (
            session.execute(
                text(
                    "SELECT symbol,interval,source,observed_at_ms,reference_price,bid_price,"
                    "ask_price,spread_bps,fee_assumption_bps,fee_assumption_version,"
                    "slippage_assumption_bps,slippage_assumption_version,"
                    "expected_net_advantage_usdt,cost_evidence_sha256,evidence_payload "
                    "FROM paper_entry_market_evidence WHERE request_id=:id"
                ),
                {"id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        if account_receipt is None or cost_receipt is None:
            raise EntryCoordinatorRejected("pre_entry_evidence_missing")
        snapshot_digest = hashlib.sha256(
            json.dumps(snapshot.payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        stored_snapshot_payload = account_receipt["evidence_payload"]
        if isinstance(stored_snapshot_payload, str):
            stored_snapshot_payload = json.loads(stored_snapshot_payload)
        if (
            snapshot_digest != row["claimed_ledger_snapshot_sha256"]
            or account_receipt["snapshot_sha256"] != snapshot_digest
            or account_receipt["account_id"] != row["paper_account_id"]
            or account_receipt["settings_version"] != row["paper_settings_version"]
            or account_receipt["journal_head_id"] != snapshot.journal_head_id
            or account_receipt["journal_count"] != snapshot.journal_count
            or stored_snapshot_payload != snapshot.payload
        ):
            raise EntryCoordinatorRejected("paper_account_snapshot_mismatch")
        market_payload = cost_receipt["evidence_payload"]
        if isinstance(market_payload, str):
            market_payload = json.loads(market_payload)
        market_digest = hashlib.sha256(
            json.dumps(market_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            fee_bps = Decimal(str(market_payload["fee_assumption_bps"]))
            slippage_bps = Decimal(str(market_payload["slippage_assumption_bps"]))
            notional = Decimal(str(market_payload["order_notional_usdt"]))
            payload_spread = Decimal(str(market_payload["spread_bps"]))
            payload_reference = Decimal(str(market_payload["reference_price"]))
            payload_bid = Decimal(str(market_payload["bid_price"]))
            payload_ask = Decimal(str(market_payload["ask_price"]))
            payload_fee_cost = Decimal(str(market_payload["fee_cost_usdt"]))
            payload_spread_cost = Decimal(str(market_payload["spread_cost_usdt"]))
            payload_slippage_cost = Decimal(str(market_payload["slippage_cost_usdt"]))
            payload_net_advantage = Decimal(str(market_payload["expected_net_advantage_usdt"]))
            payload_observed_at = max(
                int(market_payload["price_received_at_ms"]),
                int(market_payload["book_received_at_ms"]),
                int(market_payload["depth_received_at_ms"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise EntryCoordinatorRejected("paper_market_cost_payload_invalid") from exc
        server_now_ms = int(
            session.execute(
                text("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")
            ).scalar_one()
        )
        if (
            market_digest != row["claimed_cost_evidence_sha256"]
            or cost_receipt["cost_evidence_sha256"] != market_digest
            or cost_receipt["source"] != "binance_public_spot_coherence"
            or cost_receipt["symbol"] != row["symbol"]
            or cost_receipt["interval"] != "1m"
            or payload_observed_at != int(cost_receipt["observed_at_ms"])
            or not 0 <= server_now_ms - int(cost_receipt["observed_at_ms"]) <= 3_000
            or Decimal(cost_receipt["reference_price"]) != Decimal(row["reference_price"])
            or Decimal(cost_receipt["expected_net_advantage_usdt"])
            != Decimal(row["expected_net_advantage_usdt"])
            or Decimal(cost_receipt["bid_price"]) != payload_bid
            or Decimal(cost_receipt["ask_price"]) != payload_ask
            or Decimal(cost_receipt["spread_bps"]) != payload_spread
            or Decimal(cost_receipt["fee_assumption_bps"]) != fee_bps
            or cost_receipt["fee_assumption_version"]
            != market_payload.get("fee_assumption_version")
            or Decimal(cost_receipt["slippage_assumption_bps"]) != slippage_bps
            or cost_receipt["slippage_assumption_version"]
            != market_payload.get("slippage_assumption_version")
            or payload_bid <= 0
            or payload_ask <= payload_bid
            or payload_reference != Decimal(row["reference_price"])
            or notional != Decimal(row["order_notional_usdt"])
            or payload_fee_cost != notional * fee_bps / Decimal(10_000)
            or payload_spread_cost != notional * payload_spread / Decimal(10_000)
            or payload_slippage_cost != notional * slippage_bps / Decimal(10_000)
            or payload_fee_cost != Decimal(row["fee_cost_usdt"])
            or payload_spread_cost != Decimal(row["spread_cost_usdt"])
            or payload_slippage_cost != Decimal(row["slippage_cost_usdt"])
            or payload_net_advantage != Decimal(row["expected_net_advantage_usdt"])
            or market_payload.get("expected_net_advantage_verified") is not False
            or market_payload.get("trade_authorized") is not False
        ):
            raise EntryCoordinatorRejected("paper_market_cost_evidence_mismatch_or_stale")
        expected_snapshot = {
            "equity_usdt": snapshot.payload["risk_equity_usdt"],
            "day_start_equity_usdt": snapshot.payload["day_start_equity_usdt"],
            "peak_equity_usdt": snapshot.payload["peak_equity_usdt"],
            "open_capital_usdt": snapshot.payload["open_capital_usdt"],
            "open_positions": snapshot.payload["open_positions"],
            "consecutive_losses": snapshot.payload.get("consecutive_losses", 0),
        }
        expected_request = {
            "proposed_notional": row["order_notional_usdt"],
            "reference_price": row["reference_price"],
            "fee_bps_round_trip": Decimal(market_payload["fee_assumption_bps"]) * 2,
            "spread_bps": cost_receipt["spread_bps"],
            "slippage_bps": Decimal(market_payload["slippage_assumption_bps"]),
            "expected_gross_edge_bps": (
                payload_net_advantage * Decimal(10_000) / notional
                + fee_bps * 2
                + payload_spread
                + slippage_bps
            ),
            "min_net_edge_bps": Decimal(0),
        }
        try:
            receipt = get_verified_reservation(
                risk_engine,
                request_id=request_id,
                signal_id=request_id,
                profile_version=int(row["risk_profile_version"]),
                intent_sha256=str(row["intent_sha256"]),
                paper_snapshot_sha256=snapshot_digest,
                expected_request_fields=expected_request,
                expected_account_snapshot=expected_snapshot,
            )
        except RiskStoreRejected as exc:
            raise EntryCoordinatorRejected("independent_risk_reservation_unverified") from exc
        if receipt.amount_usdt != Decimal(row["order_notional_usdt"]):
            raise EntryCoordinatorRejected("risk_reservation_amount_mismatch")
        if row["to_state"] == "RISK_RESERVED":
            return _record(session, request_id)
        current = _latest_event(session, request_id)
        session.execute(
            text(
                "INSERT INTO paper_entry_intent_events(event_id,request_id,sequence,from_state,"
                "to_state,reason_code,evidence_reference) "
                "VALUES(:event,:id,:sequence,'PREPARED','RISK_RESERVED',"
                "'risk_store_verified',:evidence)"
            ),
            {
                "event": str(uuid4()),
                "id": request_id,
                "sequence": current.sequence + 1,
                "evidence": (
                    f"{receipt.audit_event_id}:{receipt.reservation_event_id}:"
                    f"{receipt.request_sha256}:{snapshot_digest}"
                ),
            },
        )
        return _record(session, request_id)


def _append_verified_reservation(
    engine: Engine, *, request_id: str, evidence: str
) -> EntryIntentRecord:
    with Session(engine) as session, session.begin():
        intent_row = session.execute(
            text("SELECT request_id FROM paper_entry_intents WHERE request_id=:id FOR UPDATE"),
            {"id": request_id},
        ).one_or_none()
        if intent_row is None:
            raise EntryCoordinatorRejected("entry_intent_not_found")
        current = _latest_event(session, request_id)
        if current.to_state != "PREPARED":
            raise EntryCoordinatorRejected("stale_entry_intent_state")
        session.execute(
            text(
                "INSERT INTO paper_entry_intent_events(event_id,request_id,sequence,from_state,"
                "to_state,"
                "reason_code,evidence_reference) "
                "VALUES(:event,:id,:sequence,'PREPARED','RISK_RESERVED','risk_store_verified',:evidence)"
            ),
            {
                "event": str(uuid4()),
                "id": request_id,
                "sequence": current.sequence + 1,
                "evidence": evidence,
            },
        )
        return _record(session, request_id)


def list_recovery_candidates(engine: Engine) -> tuple[RecoveryCandidate, ...]:
    """List nonterminal intents; every result remains blocked for manual recovery."""
    _require_postgresql(engine)
    with Session(engine) as session:
        rows: list[str] = list(
            session.execute(
                text(
                    "SELECT i.request_id FROM paper_entry_intents i "
                    "JOIN paper_entry_intent_events e ON e.request_id=i.request_id "
                    "WHERE e.sequence=(SELECT max(e2.sequence) FROM paper_entry_intent_events e2 "
                    "WHERE e2.request_id=i.request_id) "
                    "AND e.to_state NOT IN ('COMPLETED','REJECTED') ORDER BY i.created_at"
                )
            )
            .scalars()
            .all()
        )
        candidates = []
        for request_id in rows:
            candidates.append(
                RecoveryCandidate(
                    intent=_record(session, str(request_id)),
                    latest_event=_latest_event(session, str(request_id)),
                )
            )
    return tuple(candidates)


def assess_cross_store_evidence(
    *,
    request_id: str,
    risk: RiskReservationEvidence | None,
    ledger: PaperLedgerEvidence | None,
) -> RecoveryAssessment:
    """Use the existing conservative classifier; evidence never authorizes work."""
    return assess_entry_recovery(request_id=request_id, risk=risk, ledger=ledger)
