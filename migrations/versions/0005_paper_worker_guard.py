"""Persistent, default-inert PAPER worker cooldown and last-run status.

Revision ID: 0005_paper_worker_guard
Revises: 0004_paper_spot_observed_quotes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_paper_worker_guard"
down_revision: str | None = "0004_paper_spot_observed_quotes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "paper_worker_guard",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("next_allowed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_status", sa.String(24), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_paper_worker_singleton"),
        sa.CheckConstraint(
            "next_allowed_at_ms >= 0 AND last_attempt_at_ms >= 0",
            name="ck_paper_worker_clock",
        ),
        sa.CheckConstraint(
            "consecutive_failures BETWEEN 0 AND 10",
            name="ck_paper_worker_failures",
        ),
        sa.CheckConstraint(
            "last_status IN ('ready', 'no_signal', 'paper_recorded', "
            "'rate_limited', 'source_blocked', 'error')",
            name="ck_paper_worker_status",
        ),
    )
    # No seed row: migration MUST NOT start any worker or alter PAPER balances.


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM paper_worker_guard)"))
        .scalar_one()
    ):
        raise RuntimeError("Refusing to discard persisted PAPER worker cooldown")
    op.drop_table("paper_worker_guard")
