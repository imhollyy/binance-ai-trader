"""BT-023E immutable offline forecast outcomes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_ai_forecast_outcomes"
down_revision: str | None = "0008_ai_forecast_receipts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ai_research_forecast_outcomes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_research_forecast_receipts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column("entry_close", sa.Numeric(38, 18), nullable=False),
        sa.Column("target_close", sa.Numeric(38, 18), nullable=False),
        sa.Column("evaluated_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("outcome IN ('WIN', 'LOSS', 'FLAT')", name="ck_ai_outcome_label"),
        sa.CheckConstraint("entry_close > 0 AND target_close > 0", name="ck_ai_outcome_prices"),
    )
    op.execute(
        """
        CREATE FUNCTION ai_research_outcome_guard() RETURNS trigger AS $$
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'ai_research_forecast_outcomes is append only';
          END IF;
          NEW.evaluated_at_ms := FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_no_mutation BEFORE UPDATE OR DELETE "
        "ON ai_research_forecast_outcomes FOR EACH ROW "
        "EXECUTE FUNCTION ai_research_outcome_guard()"
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_no_truncate BEFORE TRUNCATE "
        "ON ai_research_forecast_outcomes FOR EACH STATEMENT "
        "EXECUTE FUNCTION ai_research_outcome_guard()"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_outcomes)"))
        .scalar_one()
    ):
        raise RuntimeError("Refusing to drop historical AI research forecast outcomes")
    op.drop_table("ai_research_forecast_outcomes")
    op.execute("DROP FUNCTION ai_research_outcome_guard()")
