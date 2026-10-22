"""Operator-only opt-in PAPER Spot POST. Never an exchange order endpoint."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.manual_paper_execution import (
    ManualPaperRequest,
    run_manual_paper_once,
)
from binance_ai_trader.market_data import MarketDataError, MarketRateLimited
from binance_ai_trader.paper_execution import PaperExecutionRejected


class ManualPaperReceiptView(BaseModel):
    mode: str = "paper"
    result: str = "paper_recorded"
    action: str
    trade_id: str
    idempotency_key: str
    event_id: str
    reference_price: Decimal
    observed_at_ms: int
    cash_after_usdt: Decimal
    fee_usdt: Decimal
    realized_pnl_delta_usdt: Decimal
    exchange_order_sent: bool = False
    autonomous_worker_started: bool = False


def register_manual_paper_route(
    app: FastAPI, require_operator: Callable[..., None], engine: Engine | None
) -> None:
    """Register only from verified isolated staging bootstrap, never local API."""

    @app.post(
        "/api/v1/paper/manual",
        response_model=ManualPaperReceiptView,
        dependencies=[Depends(require_operator)],
    )
    def manually_simulate(request: ManualPaperRequest) -> ManualPaperReceiptView:
        if engine is None:
            raise HTTPException(503, "PAPER database unavailable")
        try:
            result = run_manual_paper_once(engine, request)
        except MarketRateLimited as exc:
            raise HTTPException(429, "Public price source is rate limited") from exc
        except (MarketDataError, ValueError) as exc:
            raise HTTPException(503, "Verified public PAPER reference unavailable") from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "PAPER ledger temporarily unavailable") from exc
        except PaperExecutionRejected as exc:
            raise HTTPException(409, "PAPER manual request rejected by safeguards") from exc
        receipt = result.receipt
        return ManualPaperReceiptView(
            action=request.action,
            trade_id=receipt.trade_id,
            idempotency_key=receipt.idempotency_key,
            event_id=receipt.event_id,
            reference_price=result.reference_price,
            observed_at_ms=result.observed_at_ms,
            cash_after_usdt=receipt.cash_after_usdt,
            fee_usdt=receipt.fee_usdt,
            realized_pnl_delta_usdt=receipt.realized_pnl_delta_usdt,
        )
