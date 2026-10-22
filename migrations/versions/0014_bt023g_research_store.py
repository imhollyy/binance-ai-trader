# ruff: noqa: E501
"""Isolated BT-023G prospectively clocked prediction and outcome ledgers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_bt023g_research_store"
down_revision: str | None = "0013_ai_source_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_research_prediction_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("signal_id", sa.String(128), nullable=False, unique=True),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("feature_schema_version", sa.String(16), nullable=False),
        sa.Column("feature_sha256", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("interval", sa.String(5), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("observed_close_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("outcome_due_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("prob_win", sa.Numeric(38, 30), nullable=True),
        sa.Column("prob_loss", sa.Numeric(38, 30), nullable=True),
        sa.Column("prob_flat", sa.Numeric(38, 30), nullable=True),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT') AND interval IN ('1m','5m','15m')",
            name="ck_prediction_scope",
        ),
        sa.CheckConstraint(
            "action IN ('BUY','SELL','HOLD','NO_TRADE')", name="ck_prediction_action"
        ),
        sa.CheckConstraint(
            "observed_close_at_ms < received_at_ms AND received_at_ms < outcome_due_at_ms",
            name="ck_prediction_causal",
        ),
        sa.CheckConstraint(
            "length(artifact_sha256)=64 AND length(feature_sha256)=64", name="ck_prediction_hashes"
        ),
        sa.CheckConstraint(
            "(prob_win IS NULL AND prob_loss IS NULL AND prob_flat IS NULL) OR "
            "(prob_win BETWEEN 0 AND 1 AND prob_loss BETWEEN 0 AND 1 AND prob_flat BETWEEN 0 AND 1 "
            "AND prob_win + prob_loss + prob_flat = 1)",
            name="ck_prediction_probabilities",
        ),
    )
    op.create_index("ix_prediction_pending", "ai_research_prediction_log", ["outcome_due_at_ms"])
    op.create_table(
        "ai_research_prediction_outcomes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "signal_id",
            sa.String(128),
            sa.ForeignKey("ai_research_prediction_log.signal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("observed_outcome", sa.String(16), nullable=False),
        sa.Column("evaluated_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "observed_outcome IN ('BUY','SELL','HOLD')", name="ck_prediction_outcome"
        ),
    )
    op.execute("""
    CREATE FUNCTION bt023_clock_prediction() RETURNS trigger AS $$
    BEGIN
      NEW.received_at_ms := FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint;
      IF NEW.received_at_ms <= NEW.observed_close_at_ms OR NEW.received_at_ms >= NEW.outcome_due_at_ms THEN
        RAISE EXCEPTION 'prediction must be received before its outcome time';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER bt023_prediction_clock BEFORE INSERT ON ai_research_prediction_log
      FOR EACH ROW EXECUTE FUNCTION bt023_clock_prediction();
    CREATE FUNCTION bt023_clock_outcome() RETURNS trigger AS $$
    DECLARE due_at bigint;
    BEGIN
      SELECT outcome_due_at_ms INTO due_at FROM ai_research_prediction_log WHERE signal_id=NEW.signal_id;
      NEW.evaluated_at_ms := FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint;
      IF due_at IS NULL OR NEW.evaluated_at_ms < due_at THEN RAISE EXCEPTION 'outcome is not mature'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER bt023_outcome_clock BEFORE INSERT ON ai_research_prediction_outcomes
      FOR EACH ROW EXECUTE FUNCTION bt023_clock_outcome();
    CREATE FUNCTION bt023_prediction_append_only() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'research prediction ledger is append only'; END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER bt023_prediction_immutable BEFORE UPDATE OR DELETE ON ai_research_prediction_log
      FOR EACH ROW EXECUTE FUNCTION bt023_prediction_append_only();
    CREATE TRIGGER bt023_outcome_immutable BEFORE UPDATE OR DELETE ON ai_research_prediction_outcomes
      FOR EACH ROW EXECUTE FUNCTION bt023_prediction_append_only();
    """)


def downgrade() -> None:
    raise RuntimeError("BT-023G prediction history is append-only and must not be dropped")
