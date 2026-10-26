"""Authenticated read-only view of the isolated prospective AI research store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_research_store import ResearchStoreRejected, _isolated


class ResearchPredictionView(BaseModel):
    signal_id: str
    model_version: str
    artifact_sha256: str
    feature_schema_version: str
    feature_sha256: str
    symbol: str
    interval: str
    action: str
    reason_code: str
    observed_close_at_ms: int
    received_at_ms: int
    outcome_due_at_ms: int
    probabilities: dict[str, Decimal] | None
    outcome: str | None
    evaluated_at_ms: int | None
    outcome_source_sha256: str | None
    approval_status: str = "RESEARCH_ONLY_NOT_APPROVED"


class ResearchReadResponse(BaseModel):
    available: bool
    source: str
    status: str
    server_time: datetime
    predictions: list[ResearchPredictionView]
    metrics_available: bool = False
    metrics_reason: str = "no_persisted_evaluation_metrics"


def register_ai_research_read_route(
    app: FastAPI, require_admin: Callable[..., None], research_engine: Engine | None
) -> None:
    @app.get(
        "/api/v1/ai/research/predictions",
        response_model=ResearchReadResponse,
        dependencies=[Depends(require_admin)],
    )
    def read_predictions(
        response: Response,
        limit: int = Query(default=30, ge=1, le=100),
    ) -> ResearchReadResponse:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if research_engine is None:
            return ResearchReadResponse(
                available=False,
                source="isolated_ai_research_store",
                status="unavailable",
                server_time=datetime.now(UTC),
                predictions=[],
                metrics_reason="research_store_not_configured",
            )
        try:
            _isolated(research_engine)
            with Session(research_engine) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                server_time: datetime = session.execute(
                    text("SELECT clock_timestamp()")
                ).scalar_one()
                rows = (
                    session.execute(
                        text(
                            "SELECT p.signal_id,p.model_version,p.artifact_sha256,"
                            "p.feature_schema_version,p.feature_sha256,p.symbol,p.interval,"
                            "p.action,p.reason_code,p.observed_close_at_ms,p.received_at_ms,"
                            "p.outcome_due_at_ms,p.prob_win,p.prob_loss,p.prob_flat,"
                            "o.observed_outcome,o.evaluated_at_ms,o.source_sha256 "
                            "FROM ai_research_prediction_log p LEFT JOIN "
                            "ai_research_prediction_outcomes o USING(signal_id) "
                            "ORDER BY p.received_at_ms DESC,p.id DESC LIMIT :limit"
                        ),
                        {"limit": limit},
                    )
                    .mappings()
                    .all()
                )
            result = [
                ResearchPredictionView(
                    signal_id=str(row["signal_id"]),
                    model_version=str(row["model_version"]),
                    artifact_sha256=str(row["artifact_sha256"]),
                    feature_schema_version=str(row["feature_schema_version"]),
                    feature_sha256=str(row["feature_sha256"]),
                    symbol=str(row["symbol"]),
                    interval=str(row["interval"]),
                    action=str(row["action"]),
                    reason_code=str(row["reason_code"]),
                    observed_close_at_ms=int(row["observed_close_at_ms"]),
                    received_at_ms=int(row["received_at_ms"]),
                    outcome_due_at_ms=int(row["outcome_due_at_ms"]),
                    probabilities=(
                        {
                            "win": Decimal(row["prob_win"]),
                            "loss": Decimal(row["prob_loss"]),
                            "flat": Decimal(row["prob_flat"]),
                        }
                        if row["prob_win"] is not None
                        else None
                    ),
                    outcome=str(row["observed_outcome"]) if row["observed_outcome"] else None,
                    evaluated_at_ms=(
                        int(row["evaluated_at_ms"]) if row["evaluated_at_ms"] is not None else None
                    ),
                    outcome_source_sha256=(
                        str(row["source_sha256"]) if row["source_sha256"] is not None else None
                    ),
                )
                for row in rows
            ]
        except (ResearchStoreRejected, SQLAlchemyError) as exc:
            raise HTTPException(status_code=503, detail="AI research data unavailable") from exc
        return ResearchReadResponse(
            available=True,
            source="isolated_ai_research_store",
            status="available",
            server_time=server_time,
            predictions=result,
        )
