"""Fail-closed authorization boundary for persistent PAPER position entries.

RiskGuard reservations and the PAPER ledger currently live in separate
PostgreSQL databases. Until an audited, recoverable coordination protocol exists
a new persistent entry must never commit. A narrow exception supports isolated
disposable *test fixtures only*, not the normal API or any deployed database.
"""

from __future__ import annotations

from os import environ

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from binance_ai_trader.paper_execution import PaperExecutionRejected

# Exact local CI fixture databases; no owner database, preview database or
# generic user-named database may exercise this test-only escape hatch.
_DISPOSABLE_ENTRY_TEST_DBS = frozenset(
    {
        "trader_exec_test",
        "trader_manual_test",
        "trader_observed_test",
        "trader_signal_exec_test",
        "trader_worker_test",
        "trader_scheduler_test",
        "trader_ai_atomic_test",
        "trader_protective_close_test",
        "trader_signal_test",
    }
)


def _disposable_test_only(session: Session) -> bool:
    if environ.get("PAPER_LEDGER_TEST_ISOLATED") != "1":
        return False
    raw_test_url = environ.get("TEST_POSTGRES_URL")
    if not raw_test_url:
        return False
    try:
        expected = make_url(raw_test_url)
        bind = session.get_bind()
        engine = bind if isinstance(bind, Engine) else bind.engine
        actual = engine.url
    except (ArgumentError, ValueError):
        return False
    return (
        actual == expected
        and actual.get_backend_name() == "postgresql"
        and actual.host in ("127.0.0.1", "localhost")
        and actual.database in _DISPOSABLE_ENTRY_TEST_DBS
    )


def require_atomic_riskguard_authorization(
    *, isolated_ledger_test_session: Session | None = None
) -> None:
    """Deny new persistent entries; only exact disposable test fixtures proceed.

    Normal adapters call this without any test session and are always denied.
    The core ledger uses it inside its row-locking transaction so a new caller
    cannot bypass entry safety by directly invoking open_paper_spot().
    """
    if isolated_ledger_test_session is not None and _disposable_test_only(
        isolated_ledger_test_session
    ):
        return
    raise PaperExecutionRejected("riskguard_paper_atomicity_unavailable")
