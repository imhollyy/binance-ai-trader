"""Pin the evaluator's classification threshold to an ex ante fixed value."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_ai_outcome_fixed_band"
down_revision: str | None = "0010_ai_outcome_insert_guard"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION ai_research_outcome_band_guard() RETURNS trigger AS $$
        BEGIN
          IF NEW.flat_band_bps <> 10 THEN
            RAISE EXCEPTION 'outcome flat band is fixed at 10 bps';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_fixed_band_guard "
        "BEFORE INSERT ON ai_research_forecast_outcomes "
        "FOR EACH ROW EXECUTE FUNCTION ai_research_outcome_band_guard()"
    )


def downgrade() -> None:
    has_outcomes = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_outcomes)"))
        .scalar_one()
    )
    if has_outcomes:
        raise RuntimeError("Refusing to remove fixed classification threshold from history")
    op.execute("DROP TRIGGER ai_outcome_fixed_band_guard ON ai_research_forecast_outcomes")
    op.execute("DROP FUNCTION ai_research_outcome_band_guard()")
