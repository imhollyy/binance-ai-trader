"""Validate every forecast outcome INSERT in PostgreSQL itself."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_ai_outcome_insert_guard"
down_revision: str | None = "0009_ai_forecast_outcomes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Validate existence in the trigger instead of an FK so the prior receipt
    # TRUNCATE guard remains observable and the immutable log controls writes.
    op.drop_constraint(
        "ai_research_forecast_outcomes_receipt_id_fkey",
        "ai_research_forecast_outcomes",
        type_="foreignkey",
    )
    op.execute(
        "CREATE TRIGGER ai_outcome_insert_guard "
        "BEFORE INSERT ON ai_research_forecast_outcomes "
        "FOR EACH ROW EXECUTE FUNCTION ai_research_outcome_guard()"
    )


def downgrade() -> None:
    has_outcomes = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_research_forecast_outcomes)"))
        .scalar_one()
    )
    if has_outcomes:
        raise RuntimeError("Refusing to weaken integrity of historical AI forecast outcomes")
    op.execute("DROP TRIGGER ai_outcome_insert_guard ON ai_research_forecast_outcomes")
    op.create_foreign_key(
        "ai_research_forecast_outcomes_receipt_id_fkey",
        "ai_research_forecast_outcomes",
        "ai_research_forecast_receipts",
        ["receipt_id"],
        ["id"],
    )
