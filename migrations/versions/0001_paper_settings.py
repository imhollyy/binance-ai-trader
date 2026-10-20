"""Create paper risk settings and audit.

Revision ID: 0001_paper_settings
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001_paper_settings"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "trading_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "settings_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("settings_id", sa.Integer(), sa.ForeignKey("trading_settings.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings_audit")
    op.drop_table("trading_settings")
