"""Authenticated read-only PAPER dashboard: no exchange connections or writes."""

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
    PaperWorkerGuardRow,
    TradingSettingsRow,
)


class PaperOverviewAccount(BaseModel):
    initial_capital_usdt: Decimal
    cash_usdt: Decimal
    realized_pnl_usdt: Decimal
    paid_fees_usdt: Decimal


class PaperOverviewPosition(BaseModel):
    symbol: str
    status: str
    quantity: Decimal
    entry_fill_price: Decimal
    stop_price: Decimal
    opened_at: datetime


class PaperOverviewQuote(BaseModel):
    source: str
    symbol: str
    interval: str
    observed_at_ms: int
    reference_price: Decimal
    fee_bps: int
    slippage_bps: int
    spread_bps: int | None


class PaperOverviewEvent(BaseModel):
    id: int
    event_type: str
    created_at: datetime
    cash_delta_usdt: Decimal
    realized_pnl_delta_usdt: Decimal
    fee_usdt: Decimal
    quote: PaperOverviewQuote | None


class PaperWorkerLastKnown(BaseModel):
    """Stored attempt metadata is NEVER a verified live process heartbeat."""

    requested_enabled: bool | None
    process_verified_running: bool = False
    last_status: str | None
    last_attempt_at_ms: int | None
    next_allowed_at_ms: int | None
    consecutive_failures: int | None


class PaperDashboardView(BaseModel):
    mode: str = "paper"
    source: str = "persisted_paper_spot"
    initialized: bool
    account: PaperOverviewAccount | None
    total_positions: int
    open_positions: int
    total_events: int
    positions: list[PaperOverviewPosition]
    events: list[PaperOverviewEvent]
    worker: PaperWorkerLastKnown


def register_paper_dashboard_route(
    app: FastAPI, require_admin: Callable[..., None], db_engine: Engine | None
) -> None:
    """One bounded immutable database snapshot behind existing local auth."""

    @app.get("/api/v1/paper/dashboard", dependencies=[Depends(require_admin)])
    def paper_dashboard(
        response: Response,
        limit: int = Query(default=10, ge=1, le=30),
    ) -> PaperDashboardView:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        try:
            with Session(db_engine) as session:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                settings = session.get(TradingSettingsRow, 1)
                guard = session.get(PaperWorkerGuardRow, 1)
                worker = PaperWorkerLastKnown(
                    requested_enabled=(
                        settings.data.get("bot_enabled")
                        if settings is not None and type(settings.data.get("bot_enabled")) is bool
                        else None
                    ),
                    last_status=guard.last_status if guard else None,
                    last_attempt_at_ms=guard.last_attempt_at_ms if guard else None,
                    next_allowed_at_ms=guard.next_allowed_at_ms if guard else None,
                    consecutive_failures=guard.consecutive_failures if guard else None,
                )
                account = session.get(PaperSpotAccountRow, 1)
                if account is None:
                    return PaperDashboardView(
                        initialized=False,
                        account=None,
                        total_positions=0,
                        open_positions=0,
                        total_events=0,
                        positions=[],
                        events=[],
                        worker=worker,
                    )

                positions = session.scalars(
                    select(PaperSpotPositionRow)
                    .where(PaperSpotPositionRow.account_id == account.id)
                    .order_by(PaperSpotPositionRow.id.desc())
                    .limit(limit)
                ).all()
                events = session.scalars(
                    select(PaperSpotJournalRow)
                    .where(PaperSpotJournalRow.account_id == account.id)
                    .order_by(PaperSpotJournalRow.id.desc())
                    .limit(limit)
                ).all()
                quotes = (
                    session.scalars(
                        select(PaperSpotObservedQuoteRow).where(
                            PaperSpotObservedQuoteRow.journal_id.in_([e.id for e in events])
                        )
                    ).all()
                    if events
                    else []
                )
                by_journal = {q.journal_id: q for q in quotes}
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
                return PaperDashboardView(
                    initialized=True,
                    account=PaperOverviewAccount(
                        initial_capital_usdt=account.initial_capital_usdt,
                        cash_usdt=account.cash_usdt,
                        realized_pnl_usdt=account.realized_pnl_usdt,
                        paid_fees_usdt=account.paid_fees_usdt,
                    ),
                    total_positions=total_positions,
                    open_positions=open_positions,
                    total_events=total_events,
                    positions=[
                        PaperOverviewPosition(
                            symbol=p.symbol,
                            status=p.status,
                            quantity=p.quantity,
                            entry_fill_price=p.entry_fill_price,
                            stop_price=p.stop_price,
                            opened_at=p.opened_at,
                        )
                        for p in positions
                    ],
                    events=[
                        PaperOverviewEvent(
                            id=e.id,
                            event_type=e.event_type,
                            created_at=e.created_at,
                            cash_delta_usdt=e.cash_delta_usdt,
                            realized_pnl_delta_usdt=e.realized_pnl_delta_usdt,
                            fee_usdt=e.fee_usdt,
                            quote=(
                                PaperOverviewQuote(
                                    source=by_journal[e.id].source,
                                    symbol=by_journal[e.id].symbol,
                                    interval=by_journal[e.id].interval,
                                    observed_at_ms=by_journal[e.id].received_at_ms,
                                    reference_price=by_journal[e.id].reference_price,
                                    fee_bps=by_journal[e.id].fee_bps,
                                    slippage_bps=by_journal[e.id].slippage_bps,
                                    spread_bps=by_journal[e.id].spread_bps,
                                )
                                if e.id in by_journal
                                else None
                            ),
                        )
                        for e in events
                    ],
                    worker=worker,
                )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
