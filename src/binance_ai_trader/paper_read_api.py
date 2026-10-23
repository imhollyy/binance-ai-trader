"""Read-only local PAPER Spot ledger projection; no mutation or exchange access."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
)


class PaperSpotAccountView(BaseModel):
    mode: str
    market_type: str
    settings_version: int
    initial_capital_usdt: Decimal
    cash_usdt: Decimal
    realized_pnl_usdt: Decimal
    paid_fees_usdt: Decimal
    created_at: datetime


class PaperSpotPositionView(BaseModel):
    trade_id: str
    symbol: str
    status: str
    quantity: Decimal
    entry_fill_price: Decimal
    entry_notional_usdt: Decimal
    entry_fee_usdt: Decimal
    stop_price: Decimal
    estimated_stop_loss_usdt: Decimal
    exit_fill_price: Decimal | None
    exit_fee_usdt: Decimal | None
    net_proceeds_usdt: Decimal | None
    realized_pnl_usdt: Decimal | None
    gross_realized_pnl_usdt: Decimal | None
    opened_at: datetime
    closed_at: datetime | None


class PaperSpotJournalEvent(BaseModel):
    id: int
    event_type: str
    position_id: int | None
    cash_delta_usdt: Decimal
    balance_after_usdt: Decimal
    fee_usdt: Decimal
    realized_pnl_delta_usdt: Decimal
    quantity_delta: Decimal
    created_at: datetime
    observed_quote: PaperSpotQuoteView | None


class PaperSpotQuoteView(BaseModel):
    source: str
    symbol: str
    interval: str
    observed_at_ms: int
    reference_price: Decimal
    fee_bps: int
    slippage_bps: int
    spread_bps: int | None


class PaperSpotLedgerView(BaseModel):
    mode: str = "paper"
    market_type: str = "spot"
    source: str = "persisted_paper_spot"
    initialized: bool
    account: PaperSpotAccountView | None
    total_positions: int
    open_positions: int
    total_events: int
    positions: list[PaperSpotPositionView]
    events: list[PaperSpotJournalEvent]


def register_paper_spot_read_route(
    app: FastAPI, require_admin: Callable[..., None], db_engine: Engine | None
) -> None:
    """Expose one bounded, token-protected GET, never initializing an account."""

    @app.get("/api/v1/paper/spot", dependencies=[Depends(require_admin)])
    def paper_spot_snapshot(
        response: Response,
        positions_limit: int = Query(default=30, ge=1, le=100),
        events_limit: int = Query(default=30, ge=1, le=100),
    ) -> PaperSpotLedgerView:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        try:
            with Session(db_engine) as session:
                # All counts, rows and account values come from one committed
                # PostgreSQL snapshot, not inconsistent independent requests.
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )

                account = session.get(PaperSpotAccountRow, 1)
                if account is None:
                    return PaperSpotLedgerView(
                        initialized=False,
                        account=None,
                        total_positions=0,
                        open_positions=0,
                        total_events=0,
                        positions=[],
                        events=[],
                    )

                positions = session.scalars(
                    select(PaperSpotPositionRow)
                    .where(PaperSpotPositionRow.account_id == account.id)
                    .order_by(PaperSpotPositionRow.id.desc())
                    .limit(positions_limit)
                ).all()
                events = session.scalars(
                    select(PaperSpotJournalRow)
                    .where(PaperSpotJournalRow.account_id == account.id)
                    .order_by(PaperSpotJournalRow.id.desc())
                    .limit(events_limit)
                ).all()
                quotes = (
                    session.scalars(
                        select(PaperSpotObservedQuoteRow).where(
                            PaperSpotObservedQuoteRow.journal_id.in_([row.id for row in events])
                        )
                    ).all()
                    if events
                    else []
                )
                quote_by_journal = {quote.journal_id: quote for quote in quotes}
                total_positions = (
                    session.scalar(
                        select(func.count())
                        .select_from(PaperSpotPositionRow)
                        .where(PaperSpotPositionRow.account_id == account.id)
                    )
                    or 0
                )
                open_positions = (
                    session.scalar(
                        select(func.count())
                        .select_from(PaperSpotPositionRow)
                        .where(
                            PaperSpotPositionRow.account_id == account.id,
                            PaperSpotPositionRow.status == "open",
                        )
                    )
                    or 0
                )
                total_events = (
                    session.scalar(
                        select(func.count())
                        .select_from(PaperSpotJournalRow)
                        .where(PaperSpotJournalRow.account_id == account.id)
                    )
                    or 0
                )
                return PaperSpotLedgerView(
                    initialized=True,
                    account=PaperSpotAccountView(
                        mode=account.mode,
                        market_type=account.market_type,
                        settings_version=account.settings_version,
                        initial_capital_usdt=account.initial_capital_usdt,
                        cash_usdt=account.cash_usdt,
                        realized_pnl_usdt=account.realized_pnl_usdt,
                        paid_fees_usdt=account.paid_fees_usdt,
                        created_at=account.created_at,
                    ),
                    total_positions=total_positions,
                    open_positions=open_positions,
                    total_events=total_events,
                    positions=[
                        PaperSpotPositionView(
                            trade_id=row.trade_id,
                            symbol=row.symbol,
                            status=row.status,
                            quantity=row.quantity,
                            entry_fill_price=row.entry_fill_price,
                            entry_notional_usdt=row.entry_notional_usdt,
                            entry_fee_usdt=row.entry_fee_usdt,
                            stop_price=row.stop_price,
                            estimated_stop_loss_usdt=row.estimated_stop_loss_usdt,
                            exit_fill_price=row.exit_fill_price,
                            exit_fee_usdt=row.exit_fee_usdt,
                            net_proceeds_usdt=row.net_proceeds_usdt,
                            realized_pnl_usdt=row.realized_pnl_usdt,
                            gross_realized_pnl_usdt=(
                                row.realized_pnl_usdt + row.entry_fee_usdt + row.exit_fee_usdt
                                if row.realized_pnl_usdt is not None
                                and row.exit_fee_usdt is not None
                                else None
                            ),
                            opened_at=row.opened_at,
                            closed_at=row.closed_at,
                        )
                        for row in positions
                    ],
                    events=[
                        PaperSpotJournalEvent(
                            id=row.id,
                            event_type=row.event_type,
                            position_id=row.position_id,
                            cash_delta_usdt=row.cash_delta_usdt,
                            balance_after_usdt=row.balance_after_usdt,
                            fee_usdt=row.fee_usdt,
                            realized_pnl_delta_usdt=row.realized_pnl_delta_usdt,
                            quantity_delta=row.quantity_delta,
                            created_at=row.created_at,
                            observed_quote=(
                                PaperSpotQuoteView(
                                    source=quote_by_journal[row.id].source,
                                    symbol=quote_by_journal[row.id].symbol,
                                    interval=quote_by_journal[row.id].interval,
                                    observed_at_ms=quote_by_journal[row.id].received_at_ms,
                                    reference_price=quote_by_journal[row.id].reference_price,
                                    fee_bps=quote_by_journal[row.id].fee_bps,
                                    slippage_bps=quote_by_journal[row.id].slippage_bps,
                                    spread_bps=quote_by_journal[row.id].spread_bps,
                                )
                                if row.id in quote_by_journal
                                else None
                            ),
                        )
                        for row in events
                    ],
                )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
