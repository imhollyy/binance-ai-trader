# ruff: noqa: E501
"""Persistent prospective research recorder for an explicitly isolated DB only."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from binance_ai_trader.ai_trainable_model import ResearchPrediction

_HASH = re.compile(r"^[0-9a-f]{64}$")


class ResearchStoreRejected(ValueError):
    """Research storage rejects an unsafe target, prediction or duplicate."""


@dataclass(frozen=True, slots=True)
class StoredResearchPrediction:
    signal_id: str
    received_at_ms: int
    outcome_due_at_ms: int


def _isolated(engine: Engine) -> None:
    test_store = (
        engine.url.database == "trader_ai_bt023_test"
        and os.environ.get("TRADEPILOT_BT023_TEST_DB") == "1"
    )
    research_store = (
        engine.url.database == "trader_ai_research"
        and os.environ.get("TRADEPILOT_RESEARCH_DB") == "1"
    )
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("127.0.0.1", "localhost")
        or not (test_store or research_store)
    ):
        raise ResearchStoreRejected("dedicated_local_bt023_research_database_required")


def append_prediction(engine: Engine, prediction: ResearchPrediction) -> StoredResearchPrediction:
    """Append model output. PostgreSQL clocks it; this service has no order/risk access."""
    _isolated(engine)
    p = prediction
    if (
        type(p) is not ResearchPrediction
        or p.observed_close_at_ms is None
        or p.outcome_due_at_ms is None
        or p.action.value not in ("BUY", "SELL", "HOLD", "NO_TRADE")
        or p.model_version == "unknown"
        or p.artifact_sha256 is None
        or p.feature_sha256 is None
        or _HASH.fullmatch(p.artifact_sha256) is None
        or _HASH.fullmatch(p.feature_sha256) is None
    ):
        raise ResearchStoreRejected("prediction_is_not_receiptable")
    if p.probability is None:
        probabilities = None
    else:
        probabilities = (p.probability.win, p.probability.loss, p.probability.flat)
        if sum(probabilities) != 1:
            raise ResearchStoreRejected("invalid_probability_vector")
    query = text("""INSERT INTO ai_research_prediction_log
        (signal_id,model_version,artifact_sha256,feature_schema_version,feature_sha256,
         symbol,interval,action,reason_code,observed_close_at_ms,outcome_due_at_ms,
         prob_win,prob_loss,prob_flat)
        VALUES (:signal,:model,:artifact,'1.0',:features,:symbol,:interval,:action,:reason,
                :observed,:due,:win,:loss,:flat) RETURNING received_at_ms""")
    try:
        with Session(engine) as session, session.begin():
            received = int(
                session.execute(
                    query,
                    {
                        "signal": p.signal_id,
                        "model": p.model_version,
                        "artifact": p.artifact_sha256,
                        "features": p.feature_sha256,
                        "symbol": p.symbol,
                        "interval": p.interval,
                        "action": p.action.value,
                        "reason": p.reason_code,
                        "observed": p.observed_close_at_ms,
                        "due": p.outcome_due_at_ms,
                        "win": probabilities[0] if probabilities else None,
                        "loss": probabilities[1] if probabilities else None,
                        "flat": probabilities[2] if probabilities else None,
                    },
                ).scalar_one()
            )
            return StoredResearchPrediction(p.signal_id, received, p.outcome_due_at_ms)
    except IntegrityError as exc:
        raise ResearchStoreRejected("duplicate_or_nonprospective_prediction") from exc


def pending_predictions(engine: Engine) -> tuple[tuple[str, int, int], ...]:
    """Read unresolved predictions whose outcome time has not yet matured."""
    _isolated(engine)
    with Session(engine) as session:
        rows = session.execute(
            text("""SELECT p.signal_id,p.received_at_ms,p.outcome_due_at_ms
            FROM ai_research_prediction_log p LEFT JOIN ai_research_prediction_outcomes o USING(signal_id)
            WHERE o.signal_id IS NULL ORDER BY p.id""")
        ).all()
    return tuple((str(r[0]), int(r[1]), int(r[2])) for r in rows)


def append_outcome(engine: Engine, *, signal_id: str, outcome: str, source_sha256: str) -> int:
    """Append an outcome only after maturity; DB trigger attests the server time."""
    _isolated(engine)
    if outcome not in ("BUY", "SELL", "HOLD") or _HASH.fullmatch(source_sha256) is None:
        raise ResearchStoreRejected("invalid_outcome")
    try:
        with Session(engine) as session, session.begin():
            evaluated = int(
                session.execute(
                    text("""INSERT INTO ai_research_prediction_outcomes
                (signal_id,observed_outcome,source_sha256) VALUES (:signal,:outcome,:hash)
                RETURNING evaluated_at_ms"""),
                    {"signal": signal_id, "outcome": outcome, "hash": source_sha256},
                ).scalar_one()
            )
            return evaluated
    except IntegrityError as exc:
        raise ResearchStoreRejected("duplicate_or_unknown_prediction") from exc
