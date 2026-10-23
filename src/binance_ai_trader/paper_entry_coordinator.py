"""Durable PAPER entry intent journal; deliberately performs no trading writes.

This module stores immutable intent evidence and append-only status events in
the PAPER application database. It does not contact RiskGuard, reserve risk,
mutate the PAPER ledger, or authorize a BUY.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text
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


def create_intent(engine: Engine, intent: PaperEntryIntent) -> EntryIntentRecord:
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
