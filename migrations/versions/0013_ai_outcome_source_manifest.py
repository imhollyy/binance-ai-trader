"""Persist source endpoint, candle digest, and retrieval time per outcome."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_ai_source_manifest"
down_revision: str | None = "0012_ai_candle_guard"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "ai_research_forecast_outcomes",
        sa.Column("source_url", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "ai_research_forecast_outcomes",
        sa.Column("source_candle_sha256", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "ai_research_forecast_outcomes",
        sa.Column("source_retrieved_at_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.alter_column(
        "ai_research_forecast_outcomes",
        "source_url",
        server_default=None,
    )
    op.alter_column(
        "ai_research_forecast_outcomes",
        "source_candle_sha256",
        server_default=None,
    )
    op.alter_column(
        "ai_research_forecast_outcomes",
        "source_retrieved_at_ms",
        server_default=None,
    )
    op.execute(
        """
        CREATE FUNCTION ai_research_outcome_source_guard() RETURNS trigger AS $$
        BEGIN
          IF NEW.source_url <> 'https://data-api.binance.vision/api/v3/klines'
             OR NEW.source_candle_sha256 !~ '^[0-9a-f]{64}$'
             OR NEW.source_retrieved_at_ms <= 0
             OR NEW.source_retrieved_at_ms > NEW.evaluated_at_ms THEN
            RAISE EXCEPTION 'outcome source metadata is invalid';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_source_manifest_guard "
        "BEFORE INSERT ON ai_research_forecast_outcomes "
        "FOR EACH ROW EXECUTE FUNCTION ai_research_outcome_source_guard()"
    )


def downgrade() -> None:
    has_outcomes = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_outcomes)"))
        .scalar_one()
    )
    if has_outcomes:
        raise RuntimeError("Refusing to remove source manifests from historical outcomes")
    op.execute("DROP TRIGGER ai_outcome_source_manifest_guard ON ai_research_forecast_outcomes")
    op.execute("DROP FUNCTION ai_research_outcome_source_guard()")
    op.drop_column("ai_research_forecast_outcomes", "source_retrieved_at_ms")
    op.drop_column("ai_research_forecast_outcomes", "source_candle_sha256")
    op.drop_column("ai_research_forecast_outcomes", "source_url")
