"""Add opt-in HTTPS staging session and shared brute-force throttle only.

Revision ID: 0006_staging_sessions
Revises: 0005_paper_worker_guard
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_staging_sessions"
down_revision: str | None = "0005_paper_worker_guard"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "staging_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("issued_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("role IN ('viewer', 'operator')", name="ck_stage_session_role"),
        sa.CheckConstraint("expires_at_ms > issued_at_ms", name="ck_stage_session_expiry"),
    )
    op.create_index("ix_staging_session_expiry", "staging_sessions", ["expires_at_ms"])
    op.create_table(
        "staging_login_gate",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("locked_until_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_stage_login_singleton"),
        sa.CheckConstraint(
            "failures BETWEEN 0 AND 5 AND locked_until_ms >= 0",
            name="ck_stage_login_throttle",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO staging_login_gate(id, failures, locked_until_ms) VALUES (1, 0, 0)"
        )
    )
    # No sessions/worker are launched. PAPER balances and journal are untouched.


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM staging_sessions)"))
        .scalar_one()
    ):
        raise RuntimeError("Refusing to discard security session/revocation records")
    op.drop_table("staging_login_gate")
    op.drop_index("ix_staging_session_expiry", table_name="staging_sessions")
    op.drop_table("staging_sessions")
