"""Entry denial must also hold at the shared PAPER ledger transaction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from binance_ai_trader.paper_entry_gate import require_atomic_riskguard_authorization
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_spot import SpotBuyIntent


def test_separate_riskguard_and_ledger_cannot_authorize_a_persistent_entry() -> None:
    with pytest.raises(PaperExecutionRejected, match="riskguard_paper_atomicity_unavailable"):
        require_atomic_riskguard_authorization()


def test_core_open_denies_even_without_adapter_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import binance_ai_trader.paper_execution as execution

    # Fake the row-locked snapshot without making any actual database writes.
    # The shared ledger entry point must still reject before simulate_spot_buy.
    monkeypatch.setattr(execution, "_locked_state", lambda _session: (None, None, [], []))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with (
            Session(engine) as session,
            pytest.raises(PaperExecutionRejected, match="riskguard_paper_atomicity_unavailable"),
        ):
            execution.open_paper_spot(
                session,
                SpotBuyIntent(
                    "trade-1", "BTCUSDT", Decimal("100"), Decimal("1"), Decimal("90")
                ),
                "core-direct-entry-1",
            )
    finally:
        engine.dispose()


def test_test_only_exemption_requires_exact_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+psycopg://fixture:fixture@127.0.0.1:5432/trader_exec_test"
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            monkeypatch.setenv("TEST_POSTGRES_URL", url)
            monkeypatch.delenv("PAPER_LEDGER_TEST_ISOLATED", raising=False)
            with pytest.raises(
                PaperExecutionRejected, match="riskguard_paper_atomicity_unavailable"
            ):
                require_atomic_riskguard_authorization(isolated_ledger_test_session=session)
            monkeypatch.setenv("PAPER_LEDGER_TEST_ISOLATED", "1")
            require_atomic_riskguard_authorization(isolated_ledger_test_session=session)
            monkeypatch.setenv(
                "TEST_POSTGRES_URL",
                "postgresql+psycopg://fixture:fixture@127.0.0.1:5432/trader_owner",
            )
            with pytest.raises(
                PaperExecutionRejected, match="riskguard_paper_atomicity_unavailable"
            ):
                require_atomic_riskguard_authorization(isolated_ledger_test_session=session)
    finally:
        engine.dispose()
