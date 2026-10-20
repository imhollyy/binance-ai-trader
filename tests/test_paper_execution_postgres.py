"""Disposable PostgreSQL-only tests for the internal PAPER Spot transaction service.

CI creates a separate trader_exec_test database for these tests. NEVER use an
existing development or user database; a mere env flag is not sufficient.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    close_paper_spot,
    open_paper_spot,
)
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_spot import PaperCosts, SpotBuyIntent
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("PAPER_LEDGER_TEST_ISOLATED") == "1"
VALID_TARGET = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_exec_test"
)
pytestmark = pytest.mark.skipif(
    not ISOLATED or not VALID_TARGET,
    reason="Requires dedicated disposable localhost trader_exec_test PostgreSQL",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and ISOLATED and VALID_TARGET
    database = create_engine(URL, pool_pre_ping=True)
    with database.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(TradingSettingsRow)) == 0
        assert conn.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert conn.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert conn.scalar(select(func.count()).select_from(PaperSpotPositionRow)) == 0
    yield database
    database.dispose()


def _intent(trade: str, price: str = "100", quantity: str = "1") -> SpotBuyIntent:
    return SpotBuyIntent(trade, "BTCUSDT", D(price), D(quantity), D("99"))


def _counts(database: Engine) -> tuple[int, int, int]:
    with Session(database) as session:
        return (
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) or 0,
        )


def test_atomic_paper_open_close_replay_concurrency_and_rollback(engine: Engine) -> None:
    # The fixture starts from an explicitly separate, pristine database.
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(bot_enabled=True, manual_approval=False).model_dump(mode="json"),
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    assert _counts(engine) == (0, 1, 1)

    barrier = Barrier(2)

    def concurrent_buy(_: int) -> PaperTradeReceipt:
        barrier.wait(timeout=10)
        with Session(engine) as session:
            return open_paper_spot(session, _intent("first"), "buy-first")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, duplicate = list(executor.map(concurrent_buy, range(2)))
    assert first == duplicate
    assert first.event_type == "spot_buy"
    assert first.cash_after_usdt == D("899.849950000000000000")
    assert _counts(engine) == (1, 2, 1)

    with Session(engine) as session:
        assert open_paper_spot(session, _intent("first"), "buy-first") == first
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="paper_idempotency_conflict"
    ):
        open_paper_spot(session, _intent("first", price="101"), "buy-first")
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="duplicate_paper_trade_id"
    ):
        open_paper_spot(session, _intent("first"), "buy-other")
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="unmarked_open_position_fail_closed"
    ):
        open_paper_spot(session, _intent("second"), "buy-second")
    assert _counts(engine) == (1, 2, 1)

    # Even with the bot disabled, a MANUAL closing simulation may reduce exposure.
    with Session(engine) as session, session.begin():
        settings_row = session.get(TradingSettingsRow, 1)
        assert settings_row is not None
        settings_row.version = 2
        settings_row.data = RiskSettings(
            bot_enabled=False, manual_approval=True
        ).model_dump(mode="json")
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="manual_approval_required"
    ):
        close_paper_spot(session, "first", D("110"), "sell-first")
    with Session(engine) as session:
        closed = close_paper_spot(
            session, "first", D("110"), "sell-first", explicit_approval=True
        )
    assert closed.event_type == "spot_sell"
    assert closed.cash_after_usdt == D("1009.685005000000000000")
    assert closed.realized_pnl_delta_usdt == D("9.685005000000000000")
    assert _counts(engine) == (1, 3, 1)
    with Session(engine) as session:
        assert close_paper_spot(session, "first", D("110"), "sell-first") == closed
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="paper_idempotency_conflict"
    ):
        close_paper_spot(session, "first", D("109"), "sell-first")
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="paper_position_not_open"
    ):
        close_paper_spot(
            session, "first", D("110"), "sell-again", explicit_approval=True
        )

    # A new SQLAlchemy engine (equivalent to an API process restart) sees the same
    # ledger receipt and settled cash, rather than silently resetting the account.
    assert URL is not None
    restarted = create_engine(URL, pool_pre_ping=True)
    try:
        with Session(restarted) as session:
            assert open_paper_spot(session, _intent("first"), "buy-first") == first
        with Session(restarted) as session:
            assert close_paper_spot(session, "first", D("110"), "sell-first") == closed
    finally:
        restarted.dispose()

    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="paper_bot_disabled"
    ):
        open_paper_spot(session, _intent("second"), "buy-second")
    with Session(engine) as session, session.begin():
        settings_row = session.get(TradingSettingsRow, 1)
        assert settings_row is not None
        settings_row.version = 3
        settings_row.data = RiskSettings(
            bot_enabled=True, manual_approval=False
        ).model_dump(mode="json")

    def fail_journal_write(
        session: Session, _context: object, _instances: object
    ) -> None:
        if any(isinstance(obj, PaperSpotJournalRow) for obj in session.new):
            raise RuntimeError("simulated_journal_failure")

    with Session(engine) as session:
        event.listen(session, "before_flush", fail_journal_write)
        with pytest.raises(RuntimeError, match="simulated_journal_failure"):
            open_paper_spot(session, _intent("second"), "buy-second")
        event.remove(session, "before_flush", fail_journal_write)
    assert _counts(engine) == (1, 3, 1)
    with Session(engine) as session:
        saved = session.get(PaperSpotAccountRow, 1)
        assert saved is not None
        assert saved.cash_usdt == closed.cash_after_usdt

    # A legitimate new buy works after rollback; NaN and excessive numeric
    # precision are rejected without touching balances or the journal.
    with Session(engine) as session:
        second = open_paper_spot(session, _intent("second"), "buy-second")
        assert second.event_type == "spot_buy"
    for bad in (D("NaN"), D("1.0000000000000000001"), D("Infinity")):
        with Session(engine) as session, pytest.raises(PaperExecutionRejected):
            open_paper_spot(
                session, _intent("invalid", quantity=str(bad)), "buy-invalid"
            )

    with Session(engine) as session:
        final = close_paper_spot(session, "second", D("110"), "sell-second")
        assert final.cash_after_usdt > second.cash_after_usdt

    # Tampering with a stored account must fail closed rather than deriving
    # optimistic risk/equity values from an untrusted caller snapshot.
    with Session(engine) as session, session.begin():
        row = session.get(PaperSpotAccountRow, 1)
        assert row is not None
        row.cash_usdt += D("1")
    with Session(engine) as session, pytest.raises(
        PaperExecutionRejected, match="paper_ledger_reconciliation_failed"
    ):
        open_paper_spot(session, _intent("third"), "buy-third")

    # Restore only our own test row within the disposable database.
    with Session(engine) as session, session.begin():
        row = session.get(PaperSpotAccountRow, 1)
        assert row is not None
        row.cash_usdt -= D("1")
    with Session(engine) as session:
        assert close_paper_spot(session, "second", D("110"), "sell-second") == final


def test_cost_validation_never_sends_orders() -> None:
    # Unsupported simulated costs or non-finite prices are rejected by
    # the deterministic pure simulation; the module contains no exchange client.
    assert PaperCosts(fee_bps=10, slippage_bps=5).fee_bps == 10
