"""Conservative, read-only evidence reconciliation for future cross-database PAPER entries.

This module never queries a database, releases risk reservations, or authorizes
execution. Caller-supplied records are *unverified* until independently attested
against both append-only stores. Incomplete or conflicting records require
operator-led recovery; absence of an entry is never permission to open one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecoveryRejected(ValueError):
    """Malformed or conflicting cross-store evidence."""


class RecoveryFinding(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    RESERVATION_WITHOUT_LEDGER = "RESERVATION_WITHOUT_LEDGER"
    LEDGER_WITHOUT_RESERVATION = "LEDGER_WITHOUT_RESERVATION"
    INTENT_MISMATCH = "INTENT_MISMATCH"
    MATCHED_UNVERIFIED = "MATCHED_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class RiskReservationEvidence:
    """Opaque metadata fetched from the independent, append-only risk store."""

    request_id: str
    intent_sha256: str
    reservation_id: str
    profile_version: int
    released: bool = False

    def __post_init__(self) -> None:
        if (
            _ID.fullmatch(self.request_id) is None
            or _ID.fullmatch(self.reservation_id) is None
            or _SHA256.fullmatch(self.intent_sha256) is None
            or type(self.profile_version) is not int
            or self.profile_version < 1
            or type(self.released) is not bool
        ):
            raise RecoveryRejected("invalid_risk_reservation_evidence")


@dataclass(frozen=True, slots=True)
class PaperLedgerEvidence:
    """Opaque metadata fetched from the committed, immutable PAPER journal."""

    request_id: str
    intent_sha256: str
    journal_event_id: str
    profile_version: int

    def __post_init__(self) -> None:
        if (
            _ID.fullmatch(self.request_id) is None
            or _SHA256.fullmatch(self.intent_sha256) is None
            or _SHA256.fullmatch(self.journal_event_id) is None
            or type(self.profile_version) is not int
            or self.profile_version < 1
        ):
            raise RecoveryRejected("invalid_paper_ledger_evidence")


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    finding: RecoveryFinding
    new_entry_authorized: bool = False
    automatic_reservation_release_allowed: bool = False
    manual_reconciliation_required: bool = True


def assess_entry_recovery(
    *,
    request_id: str,
    risk: RiskReservationEvidence | None,
    ledger: PaperLedgerEvidence | None,
) -> RecoveryAssessment:
    """Classify records without implying distributed atomicity or trusted proof.

    Even consistent metadata is not a trusted cross-database commit
    certificate. A future coordinator must separately verify journal
    immutability, exact amounts, snapshots, worker fencing and risk approval.
    """
    if _ID.fullmatch(request_id) is None:
        raise RecoveryRejected("invalid_entry_request_id")
    if risk is not None and (type(risk) is not RiskReservationEvidence or risk.request_id != request_id):
        raise RecoveryRejected("risk_evidence_request_mismatch")
    if ledger is not None and (type(ledger) is not PaperLedgerEvidence or ledger.request_id != request_id):
        raise RecoveryRejected("ledger_evidence_request_mismatch")
    if risk is None and ledger is None:
        return RecoveryAssessment(RecoveryFinding.NO_EVIDENCE)
    if risk is None:
        return RecoveryAssessment(RecoveryFinding.LEDGER_WITHOUT_RESERVATION)
    if ledger is None:
        return RecoveryAssessment(RecoveryFinding.RESERVATION_WITHOUT_LEDGER)
    if (
        risk.released
        or risk.intent_sha256 != ledger.intent_sha256
        or risk.profile_version != ledger.profile_version
    ):
        return RecoveryAssessment(RecoveryFinding.INTENT_MISMATCH)
    return RecoveryAssessment(RecoveryFinding.MATCHED_UNVERIFIED)
