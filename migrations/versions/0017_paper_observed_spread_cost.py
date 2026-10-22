"""Record assumed spread separately on PAPER quote audit rows."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_paper_spread_cost"
down_revision: str | None = "0016_bt023g_risk_trunc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_spot_observed_quotes", sa.Column("spread_bps", sa.Integer()))
    op.create_check_constraint(
        "ck_paper_observed_spread",
        "paper_spot_observed_quotes",
        "spread_bps IS NULL OR spread_bps BETWEEN 0 AND 100",
    )


def downgrade() -> None:
    raise RuntimeError(
        "BT-023G/BT-024 append-only TRUNCATE protections must not be removed; "
        "PAPER quote cost audit history must not lose spread metadata"
    )
