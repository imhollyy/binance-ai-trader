"""BT-023D: inert append-only FORECAST receipt table; no order or account writes.

Revision ID: 0008_ai_forecast_receipts
Revises: 0007_paper_bot_controls
No forecast is seeded, activated or promoted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ai_forecast_receipts"
down_revision: str | None = "0007_paper_bot_controls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ai_research_forecast_receipts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("signal_id", sa.String(128), nullable=False, unique=True),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("interval", sa.String(5), nullable=False),
        sa.Column("observed_close_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("outcome_due_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("prob_win", sa.Numeric(38, 30), nullable=False),
        sa.Column("prob_loss", sa.Numeric(38, 30), nullable=False),
        sa.Column("prob_flat", sa.Numeric(38, 30), nullable=False),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT', 'ETHUSDT') AND interval IN ('1m', '5m', '15m')",
            name="ck_ai_receipt_spot_scope",
        ),
        sa.CheckConstraint(
            "length(signal_id) BETWEEN 8 AND 128 AND length(model_version) BETWEEN 1 AND 64",
            name="ck_ai_receipt_names",
        ),
        sa.CheckConstraint(
            "observed_close_at_ms > 0 AND "
            "observed_close_at_ms < received_at_ms AND received_at_ms < outcome_due_at_ms",
            name="ck_ai_receipt_causal_order",
        ),
        sa.CheckConstraint(
            "outcome_due_at_ms - observed_close_at_ms BETWEEN 60000 AND 86400000",
            name="ck_ai_receipt_horizon",
        ),
        sa.CheckConstraint(
            "prob_win BETWEEN 0 AND 1 AND prob_loss BETWEEN 0 AND 1 AND "
            "prob_flat BETWEEN 0 AND 1 AND prob_win + prob_loss + prob_flat = 1 "
            "AND prob_win <> 'NaN'::numeric AND prob_loss <> 'NaN'::numeric "
            "AND prob_flat <> 'NaN'::numeric",
            name="ck_ai_receipt_probabilities",
        ),
        sa.CheckConstraint(
            "CASE interval "
            "WHEN '1m' THEN MOD(observed_close_at_ms + 1, 60000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 60000) = 0 "
            "WHEN '5m' THEN MOD(observed_close_at_ms + 1, 300000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 300000) = 0 "
            "WHEN '15m' THEN MOD(observed_close_at_ms + 1, 900000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 900000) = 0 "
            "ELSE FALSE END",
            name="ck_ai_receipt_closed_candle_grid",
        ),
    )
    op.create_index(
        "ix_ai_receipt_symbol_due",
        "ai_research_forecast_receipts",
        ["symbol", "outcome_due_at_ms"],
    )
    op.execute(
        """
        CREATE FUNCTION ai_research_receipt_db_clock() RETURNS trigger AS $$
        BEGIN
            NEW.received_at_ms :=
                FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint;
            IF NEW.received_at_ms <= NEW.observed_close_at_ms
               OR NEW.received_at_ms >= NEW.outcome_due_at_ms THEN
                RAISE EXCEPTION 'forecast must be received after close and before outcome';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER ai_receipt_server_clock "
        "BEFORE INSERT ON ai_research_forecast_receipts "
        "FOR EACH ROW EXECUTE FUNCTION ai_research_receipt_db_clock()"
    )
    op.execute(
        """
        CREATE FUNCTION ai_research_receipt_reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ai_research_forecast_receipts is append only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER ai_receipt_no_update_delete "
        "BEFORE UPDATE OR DELETE ON ai_research_forecast_receipts "
        "FOR EACH ROW EXECUTE FUNCTION ai_research_receipt_reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER ai_receipt_no_truncate "
        "BEFORE TRUNCATE ON ai_research_forecast_receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION ai_research_receipt_reject_mutation()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_history = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_receipts)")
    ).scalar_one()
    if has_history:
        raise RuntimeError("Refusing to drop historical AI research forecast receipts")
    op.drop_index("ix_ai_receipt_symbol_due", table_name="ai_research_forecast_receipts")
    op.drop_table("ai_research_forecast_receipts")
    op.execute("DROP FUNCTION ai_research_receipt_db_clock()")
    op.execute("DROP FUNCTION ai_research_receipt_reject_mutation()")
