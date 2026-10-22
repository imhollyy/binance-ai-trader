"""Keep source candles immutable after they have produced an outcome."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_ai_candle_guard"
down_revision: str | None = "0011_ai_outcome_fixed_band"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION ai_research_protect_evaluated_candles() RETURNS trigger AS $$
        DECLARE
          candle_symbol text;
          candle_interval text;
          candle_close_ms bigint;
        BEGIN
          IF TG_OP = 'TRUNCATE' THEN
            IF EXISTS (SELECT 1 FROM ai_research_forecast_outcomes) THEN
              RAISE EXCEPTION 'spot_candles contains evaluated forecast source data';
            END IF;
            RETURN NULL;
          END IF;
          IF TG_OP = 'UPDATE' THEN
            candle_symbol := OLD.symbol;
            candle_interval := OLD.interval;
            candle_close_ms := OLD.close_time_ms;
            IF EXISTS (
              SELECT 1 FROM ai_research_forecast_receipts r
              JOIN ai_research_forecast_outcomes o ON o.receipt_id = r.id
              WHERE r.symbol = candle_symbol AND r.interval = candle_interval
                AND candle_close_ms BETWEEN r.observed_close_at_ms AND r.outcome_due_at_ms
            ) THEN
              RAISE EXCEPTION 'spot candle is used for an evaluated forecast outcome';
            END IF;
            candle_symbol := NEW.symbol;
            candle_interval := NEW.interval;
            candle_close_ms := NEW.close_time_ms;
            IF EXISTS (
              SELECT 1 FROM ai_research_forecast_receipts r
              JOIN ai_research_forecast_outcomes o ON o.receipt_id = r.id
              WHERE r.symbol = candle_symbol AND r.interval = candle_interval
                AND candle_close_ms BETWEEN r.observed_close_at_ms AND r.outcome_due_at_ms
            ) THEN
              RAISE EXCEPTION 'spot candle is used for an evaluated forecast outcome';
            END IF;
            RETURN NEW;
          END IF;
          candle_symbol := OLD.symbol;
          candle_interval := OLD.interval;
          candle_close_ms := OLD.close_time_ms;
          IF EXISTS (
            SELECT 1 FROM ai_research_forecast_receipts r
            JOIN ai_research_forecast_outcomes o ON o.receipt_id = r.id
            WHERE r.symbol = candle_symbol AND r.interval = candle_interval
              AND candle_close_ms BETWEEN r.observed_close_at_ms AND r.outcome_due_at_ms
          ) THEN
            RAISE EXCEPTION 'spot candle is used for an evaluated forecast outcome';
          END IF;
          RETURN OLD;
        END; $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_source_candles_no_update "
        "BEFORE UPDATE OR DELETE ON spot_candles FOR EACH ROW "
        "EXECUTE FUNCTION ai_research_protect_evaluated_candles()"
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_source_candles_no_truncate "
        "BEFORE TRUNCATE ON spot_candles FOR EACH STATEMENT "
        "EXECUTE FUNCTION ai_research_protect_evaluated_candles()"
    )


def downgrade() -> None:
    has_outcomes = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_outcomes)"))
        .scalar_one()
    )
    if has_outcomes:
        raise RuntimeError(
            "Refusing to remove immutability protection from evaluated source candles"
        )
    op.execute("DROP TRIGGER ai_outcome_source_candles_no_truncate ON spot_candles")
    op.execute("DROP TRIGGER ai_outcome_source_candles_no_update ON spot_candles")
    op.execute("DROP FUNCTION ai_research_protect_evaluated_candles()")
