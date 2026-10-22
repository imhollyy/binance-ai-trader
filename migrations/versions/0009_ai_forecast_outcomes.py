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
        sa.Column("flat_band_bps", sa.Numeric(12, 6), nullable=False),
        sa.Column("evaluated_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("outcome IN ('WIN', 'LOSS', 'FLAT')", name="ck_ai_outcome_label"),
        sa.CheckConstraint("entry_close > 0 AND target_close > 0", name="ck_ai_outcome_prices"),
        sa.CheckConstraint("flat_band_bps BETWEEN 0 AND 200", name="ck_ai_outcome_flat_band"),
    )
    op.execute(
        """
        CREATE FUNCTION ai_research_outcome_guard() RETURNS trigger AS $$
        DECLARE
          receipt_row ai_research_forecast_receipts%ROWTYPE;
          source_close numeric;
          target_close numeric;
          return_bps numeric;
          expected_outcome text;
          period_ms bigint;
          candle_count bigint;
          invalid_candle_count bigint;
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'ai_research_forecast_outcomes is append only';
          END IF;
          SELECT * INTO STRICT receipt_row
            FROM ai_research_forecast_receipts WHERE id = NEW.receipt_id;
          period_ms := CASE receipt_row.interval
            WHEN '1m' THEN 60000 WHEN '5m' THEN 300000 WHEN '15m' THEN 900000
            ELSE 0 END;
          IF period_ms = 0 THEN
            RAISE EXCEPTION 'unsupported outcome interval';
          END IF;
          SELECT COUNT(*), COUNT(*) FILTER (WHERE
              open_time_ms % period_ms <> 0
              OR close_time_ms <> open_time_ms + period_ms - 1
              OR open <= 0 OR high < low OR low <= 0 OR close <= 0 OR volume < 0
              OR open < low OR open > high OR close < low OR close > high)
            INTO candle_count, invalid_candle_count
            FROM spot_candles
            WHERE symbol = receipt_row.symbol AND interval = receipt_row.interval
              AND close_time_ms BETWEEN receipt_row.observed_close_at_ms
                                    AND receipt_row.outcome_due_at_ms;
          IF candle_count <> (receipt_row.outcome_due_at_ms
                              - receipt_row.observed_close_at_ms) / period_ms + 1
             OR invalid_candle_count <> 0 THEN
            RAISE EXCEPTION 'outcome requires complete valid closed candle history';
          END IF;
          SELECT close INTO STRICT source_close FROM spot_candles
            WHERE symbol = receipt_row.symbol AND interval = receipt_row.interval
              AND close_time_ms = receipt_row.observed_close_at_ms;
          SELECT close INTO STRICT target_close FROM spot_candles
            WHERE symbol = receipt_row.symbol AND interval = receipt_row.interval
              AND close_time_ms = receipt_row.outcome_due_at_ms;
          IF NEW.entry_close <> source_close OR NEW.target_close <> target_close
             OR NEW.flat_band_bps < 0 OR NEW.flat_band_bps > 200 THEN
            RAISE EXCEPTION 'outcome prices or threshold do not match closed candles';
          END IF;
          return_bps := (target_close / source_close - 1) * 10000;
          expected_outcome := CASE
            WHEN return_bps > NEW.flat_band_bps THEN 'WIN'
            WHEN return_bps < -NEW.flat_band_bps THEN 'LOSS'
            ELSE 'FLAT' END;
          IF NEW.outcome <> expected_outcome THEN
            RAISE EXCEPTION 'outcome label does not match closed candle calculation';
          END IF;
          NEW.evaluated_at_ms := FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint;
          IF NEW.evaluated_at_ms < receipt_row.outcome_due_at_ms THEN
            RAISE EXCEPTION 'outcome is not due';
          END IF;
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
