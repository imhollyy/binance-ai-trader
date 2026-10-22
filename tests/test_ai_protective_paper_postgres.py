"""BT-018 isolated, mock-only protective PAPER stop/TP net settlement E2E.

Only ephemeral localhost trader_protective_close_test is ever used. Neither
the real Binance market nor a user's PAPER account receives a request.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_paper_bot_controls_test import (
    initialize_disposable_bot_control,
    update_disposable_bot_control,
)
from binance_ai_trader.ai_position_protection import StopBehavior
from binance_ai_trader.ai_protective_paper_test import simulate_isolated_protective_close_once
from binance_ai_trader.models import (
    PaperBotControlRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    open_paper_spot,
)
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.paper_spot import PaperCosts, SpotBuyIntent
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("PROTECTIVE_CLOSE_TEST_ISOLATED") == "1"
SAFE = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("localhost", "127.0.0.1")
    and make_url(URL).database == "trader_protective_close_test"
)
pytestmark = pytest.mark.skipif(
    not (SAFE and ISOLATED),
    reason="Fresh disposable localhost trader_protective_close_test required",
)
OPERATOR = "e" * 64
COSTS = PaperCosts(fee_bps=10, slippage_bps=5, spread_bps=4)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and SAFE and ISOLATED
    db = create_engine(URL, pool_pre_ping=True)
    with Session(db) as session:
        for model in (
            PaperSpotAccountRow,
            PaperSpotPositionRow,
            PaperSpotJournalRow,
            PaperSpotObservedQuoteRow,
            PaperBotControlRow,
            SpotCandleRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    yield db
    db.dispose()


def _row(opened: int, *, close: str = "100") -> SpotCandleRow:
    return SpotCandleRow(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=opened,
        close_time_ms=opened + 59_999,
        open=D("100"),
        high=D("112"),
        low=D("90"),
        close=D(close),
        volume=D("2"),
    )


def _observer(
    db: Engine, clock: int, *, ticker: str, code: int = 200
) -> PublicSpotObservationClient:
    with Session(db) as session:
        rows = list(session.scalars(select(SpotCandleRow).order_by(SpotCandleRow.open_time_ms)))
        candles: list[list[object]] = [
            [
                row.open_time_ms,
                str(row.open),
                str(row.high),
                str(row.low),
                str(row.close),
                str(row.volume),
                row.close_time_ms,
                "0",
                1,
                "0",
                "0",
                "0",
            ]
            for row in rows
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert "X-MBX-APIKEY" not in request.headers
        if request.url.path == "/api/v3/klines":
            return httpx.Response(200, json=candles)
        assert request.url.path == "/api/v3/ticker/price"
        return httpx.Response(
            code,
            json={"symbol": "BTCUSDT", "price": ticker},
            headers={"Retry-After": "30"},
        )

    return PublicSpotObservationClient(
        transport=httpx.MockTransport(handler), clock_ms=lambda: clock
    )


def _close(
    db: Engine,
    clock: int,
    *,
    trade: str = "protect_1",
    ticker: str = "98.5",
    revision: int = 3,
    approval: bool = True,
    tp: Decimal | None = None,
    costs: PaperCosts = COSTS,
    status: int = 200,
) -> PaperTradeReceipt:
    return simulate_isolated_protective_close_once(
        db,
        _observer(db, clock, ticker=ticker, code=status),
        trade_id=trade,
        expected_control_revision=revision,
        explicit_test_exit_approval=approval,
        take_profit_price=tp,
        costs=costs,
    )


def _count(db: Engine) -> tuple[int, int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotObservedQuoteRow)) or 0,
        )


def test_mock_stop_then_take_profit_with_locked_control_fee_pnl_and_audit(
    engine: Engine,
) -> None:
    start = (time.time_ns() // 60_000_000_000) * 60_000
    clock = start + 100
    with Session(engine) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=1,
                data=RiskSettings(
                    bot_enabled=True,
                    manual_approval=False,
                    risk_per_trade_pct=2,
                ).model_dump(mode="json"),
            )
        )
        session.add_all([_row(start - (60 - i) * 60_000) for i in range(60)])
        session.add(
            StagingSessionRow(
                token_hash=OPERATOR,
                csrf_hash="c" * 64,
                role="operator",
                issued_at_ms=clock - 5_000,
                expires_at_ms=clock + 1_000_000,
                revoked=False,
            )
        )
    with Session(engine) as session:
        ensure_paper_spot_account(session)
    initial = initialize_disposable_bot_control(engine, now_ms=clock)
    assert initial.state is BotState.STOPPED
    paused = update_disposable_bot_control(
        engine,
        actor_session_hash=OPERATOR,
        expected_revision=1,
        next_state=BotState.PAUSED,
        stop_behavior=StopBehavior.MONITOR_ONLY,
        confirm_reduce_only_review=False,
        now_ms=clock,
    )
    assert paused.revision == 2
    with Session(engine) as session:
        bought = open_paper_spot(
            session,
            SpotBuyIntent("protect_1", "BTCUSDT", D("104"), D("0.1"), D("99")),
            "protected-fixture-buy-one",
            costs=COSTS,
        )
    assert bought.event_type == "spot_buy"
    assert _count(engine) == (1, 2, 0)
    with pytest.raises(PaperExecutionRejected, match="protective_test_approval_or_source_required"):
        _close(engine, clock, approval=False)
    with pytest.raises(PaperExecutionRejected, match="protective_operator_state_denied"):
        _close(engine, clock, ticker="98.5", revision=2)
    with pytest.raises(PaperExecutionRejected, match="protective_public_observation_denied"):
        _close(engine, clock, status=429)
    with pytest.raises(PaperExecutionRejected, match="protective_public_observation_denied"):
        _close(engine, clock, ticker="200")
    assert _count(engine) == (1, 2, 0)

    configured = update_disposable_bot_control(
        engine,
        actor_session_hash=OPERATOR,
        expected_revision=2,
        next_state=BotState.PAUSED,
        stop_behavior=StopBehavior.REVIEW_REDUCE_ONLY,
        confirm_reduce_only_review=True,
        now_ms=clock,
    )
    assert configured.revision == 3
    with pytest.raises(PaperExecutionRejected, match="protective_operator_state_denied"):
        _close(engine, clock, revision=2)
    with pytest.raises(PaperExecutionRejected, match="protective_trigger_not_present"):
        _close(engine, clock, ticker="102")
    with pytest.raises(PaperExecutionRejected, match="protective_costs_invalid"):
        _close(engine, clock, costs=PaperCosts(-1, 5))
    assert _count(engine) == (1, 2, 0)

    # Audit failure must roll back the already staged SELL, PnL and fees.
    def break_audit(session: Session, *_: object) -> None:
        if any(isinstance(item, PaperSpotObservedQuoteRow) for item in session.new):
            raise RuntimeError("deliberate_protective_audit_rollback")

    event.listen(Session, "before_flush", break_audit)
    try:
        with pytest.raises(RuntimeError, match="deliberate_protective_audit_rollback"):
            _close(engine, clock)
    finally:
        event.remove(Session, "before_flush", break_audit)
    assert _count(engine) == (1, 2, 0)

    barrier = Barrier(3)

    def race(_: int) -> PaperTradeReceipt | str:
        barrier.wait(timeout=15)
        try:
            return _close(engine, clock)
        except PaperExecutionRejected as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        attempts = list(pool.map(race, range(3)))
    receipts = [entry for entry in attempts if isinstance(entry, PaperTradeReceipt)]
    assert receipts
    assert all(entry == receipts[0] for entry in receipts)
    stop = receipts[0]
    assert stop.event_type == "spot_sell"
    assert stop.realized_pnl_delta_usdt < 0
    assert _count(engine) == (1, 3, 1)
    with pytest.raises(PaperExecutionRejected, match="protective_position_not_open"):
        _close(engine, clock)

    with Session(engine) as session:
        account = session.get(PaperSpotAccountRow, 1)
        position = session.scalar(select(PaperSpotPositionRow))
        quote = session.scalar(select(PaperSpotObservedQuoteRow))
        assert account is not None and position is not None and quote is not None
        assert position.status == "closed"
        assert account.cash_usdt == stop.cash_after_usdt
        assert account.realized_pnl_usdt == stop.realized_pnl_delta_usdt
        assert position.exit_fee_usdt is not None
        assert account.paid_fees_usdt == position.entry_fee_usdt + position.exit_fee_usdt
        assert quote.reference_price == D("98.5")
        assert quote.fee_bps == 10
        assert quote.slippage_bps == 5
        assert quote.spread_bps == 4
        assert position.stop_price == D("99")
        assert position.exit_fill_price is not None
        assert position.exit_fill_price < position.stop_price  # gap plus adverse costs

    # Synthetic next mark: after five new fully closed candles, the price is
    # above a separately specified TARGET. Same protected engine, no live API.
    with Session(engine) as session, session.begin():
        session.add_all([_row(start + i * 60_000, close="108") for i in range(5)])
    later = start + 300_100
    with Session(engine) as session:
        second = open_paper_spot(
            session,
            SpotBuyIntent("protect_2", "BTCUSDT", D("105"), D("0.1"), D("100")),
            "protected-fixture-buy-two",
            costs=COSTS,
        )
    assert second.event_type == "spot_buy"
    with pytest.raises(PaperExecutionRejected, match="protective_trigger_not_present"):
        _close(engine, later, trade="protect_2", ticker="108", tp=D("109"))
    profit = _close(engine, later, trade="protect_2", ticker="110", tp=D("109"))
    assert profit.event_type == "spot_sell"
    assert profit.realized_pnl_delta_usdt > 0
    assert _count(engine) == (2, 5, 2)
    with Session(engine) as session:
        account = session.get(PaperSpotAccountRow, 1)
        assert account is not None
        expected_pnl = stop.realized_pnl_delta_usdt + profit.realized_pnl_delta_usdt
        assert account.cash_usdt == D("1000") + expected_pnl
        assert account.realized_pnl_usdt == expected_pnl


def test_non_disposable_target_is_denied_before_any_network() -> None:
    fake = create_engine("postgresql+psycopg://localhost/trader_owner")
    try:
        with pytest.raises(PaperExecutionRejected, match="disposable_protective_paper_only"):
            simulate_isolated_protective_close_once(
                fake,
                object(),  # type: ignore[arg-type]
                trade_id="not_open",
                expected_control_revision=3,
                explicit_test_exit_approval=True,
                costs=COSTS,
            )
    finally:
        fake.dispose()
