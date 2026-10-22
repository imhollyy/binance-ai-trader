"""BT-016: inert, revisioned PAPER bot controls and append-only operator audit.

Revision ID: 0007_paper_bot_controls
Revises: 0006_staging_sessions

No default RUNNING rows, workers, exchange calls, capital or journal updates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_paper_bot_controls"
down_revision: str | None = "0006_staging_sessions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "paper_bot_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("stop_behavior", sa.String(32), nullable=False),
        sa.Column("operator_confirmed_reduce_only", sa.Boolean(), nullable=False),
        sa.Column("modified_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_paper_bot_control_singleton"),
        sa.CheckConstraint(
            "revision >= 1 AND settings_version >= 1 AND modified_at_ms >= 0",
            name="ck_paper_bot_control_versions",
        ),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'PAUSED', 'STOPPED', 'EMERGENCY_FLATTEN')",
            name="ck_paper_bot_control_state",
        ),
        sa.CheckConstraint(
            "stop_behavior IN ('MONITOR_ONLY', 'REVIEW_REDUCE_ONLY')",
            name="ck_paper_bot_stop_behavior",
        ),
        sa.CheckConstraint(
            "stop_behavior = 'REVIEW_REDUCE_ONLY' "
            "OR operator_confirmed_reduce_only = false",
            name="ck_paper_bot_approval_consistency",
        ),
    )
    op.create_table(
        "paper_bot_control_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, unique=True),
        sa.Column("previous_state", sa.String(24), nullable=False),
        sa.Column("new_state", sa.String(24), nullable=False),
        sa.Column("stop_behavior", sa.String(32), nullable=False),
        sa.Column("operator_confirmed_reduce_only", sa.Boolean(), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("actor_session_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "revision >= 2 AND settings_version >= 1 AND recorded_at_ms >= 0",
            name="ck_paper_bot_event_versions",
        ),
        sa.CheckConstraint(
            "length(actor_session_hash) = 64", name="ck_paper_bot_event_actor_digest"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION reject_paper_bot_control_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'paper_bot_control_events is append only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER paper_bot_control_events_no_update_delete "
        "BEFORE UPDATE OR DELETE ON paper_bot_control_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_paper_bot_control_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER paper_bot_control_events_no_truncate "
        "BEFORE TRUNCATE ON paper_bot_control_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_paper_bot_control_event_mutation()"
    )


def downgrade() -> None:
    db = op.get_bind()
    for table in ("paper_bot_controls", "paper_bot_control_events"):
        if db.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError("Refusing to discard PAPER operator controls or audit")
    op.drop_table("paper_bot_control_events")
    op.drop_table("paper_bot_controls")
    op.execute("DROP FUNCTION reject_paper_bot_control_event_mutation()")
