"""Fail-closed, read-only recovery decisions for independent PAPER and risk stores."""

from __future__ import annotations

import pytest

from binance_ai_trader.paper_entry_recovery import (
    PaperLedgerEvidence,
    RecoveryFinding,
    RecoveryRejected,
    RiskReservationEvidence,
    assess_entry_recovery,
)

REQUEST = "paper_entry_12345"
HASH = "a" * 64
OTHER_HASH = "b" * 64


def _risk(*, released: bool = False, digest: str = HASH) -> RiskReservationEvidence:
    return RiskReservationEvidence(
        request_id=REQUEST,
        intent_sha256=digest,
        reservation_id="reservation_12345",
        profile_version=2,
        released=released,
    )


def _ledger(*, digest: str = HASH, version: int = 2) -> PaperLedgerEvidence:
    return PaperLedgerEvidence(
        request_id=REQUEST,
        intent_sha256=digest,
        journal_event_id="c" * 64,
        profile_version=version,
    )


@pytest.mark.parametrize(
    ("risk", "ledger", "expected"),
    [
        (None, None, RecoveryFinding.NO_EVIDENCE),
        (_risk(), None, RecoveryFinding.RESERVATION_WITHOUT_LEDGER),
        (None, _ledger(), RecoveryFinding.LEDGER_WITHOUT_RESERVATION),
        (_risk(), _ledger(), RecoveryFinding.MATCHED_UNVERIFIED),
        (_risk(digest=OTHER_HASH), _ledger(), RecoveryFinding.INTENT_MISMATCH),
        (_risk(released=True), _ledger(), RecoveryFinding.INTENT_MISMATCH),
        (_risk(), _ledger(version=3), RecoveryFinding.INTENT_MISMATCH),
    ],
)
def test_recovery_cannot_authorize_entry_or_auto_release(
    risk: RiskReservationEvidence | None,
    ledger: PaperLedgerEvidence | None,
    expected: RecoveryFinding,
) -> None:
    assessment = assess_entry_recovery(request_id=REQUEST, risk=risk, ledger=ledger)
    assert assessment.finding == expected
    assert assessment.new_entry_authorized is False
    assert assessment.automatic_reservation_release_allowed is False
    assert assessment.manual_reconciliation_required is True


def test_cross_request_evidence_cannot_be_mixed() -> None:
    ledger = PaperLedgerEvidence("different_1234", HASH, "c" * 64, 2)
    with pytest.raises(RecoveryRejected, match="ledger_evidence_request_mismatch"):
        assess_entry_recovery(request_id=REQUEST, risk=_risk(), ledger=ledger)


def test_invalid_or_ambiguous_identity_is_rejected() -> None:
    with pytest.raises(RecoveryRejected, match="invalid_risk_reservation_evidence"):
        RiskReservationEvidence(REQUEST, "INVALID", "reservation_12345", 2)
    with pytest.raises(RecoveryRejected, match="invalid_paper_ledger_evidence"):
        PaperLedgerEvidence(REQUEST, HASH, "journal_id", 2)
    with pytest.raises(RecoveryRejected, match="invalid_entry_request_id"):
        assess_entry_recovery(request_id="bad id", risk=None, ledger=None)
