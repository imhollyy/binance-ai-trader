"""Fail-closed gate for persistent PAPER position entries.

The independent BT-024 RiskGuard store and the PAPER ledger are separate
PostgreSQL databases. There is no shared transaction or recovery protocol that
can atomically commit a reservation and a ledger entry, so production entry
adapters must remain disabled until such an orchestrator exists.
"""

from __future__ import annotations

from binance_ai_trader.paper_execution import PaperExecutionRejected


def require_atomic_riskguard_authorization() -> None:
    """Reject an entry until RiskGuard reservation and PAPER commit are atomic."""
    raise PaperExecutionRejected("riskguard_paper_atomicity_unavailable")
