"""Add inert persistent worker identity, lease, heartbeat and stop audit."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_bt027_worker_supervisor"
down_revision: str | None = "0017_paper_spread_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_worker_runtime",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_id", sa.String(36)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("lease_until_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1 AND revision >= 0", name="ck_worker_runtime_singleton"),
        sa.CheckConstraint(
            "state IN ('STOPPED','RUNNING','STOP_REQUESTED')", name="ck_worker_runtime_state"
        ),
        sa.CheckConstraint(
            "heartbeat_at_ms >= 0 AND lease_until_ms >= 0", name="ck_worker_runtime_clock"
        ),
    )
    op.create_table(
        "paper_worker_supervisor_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False, unique=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("worker_id", sa.String(36)),
        sa.Column("actor_sha256", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('WORKER_CLAIMED','HEARTBEAT','STOP_REQUESTED','WORKER_STOPPED')",
            name="ck_worker_supervisor_event_type",
        ),
        sa.CheckConstraint(
            "(event_type IN ('STOP_REQUESTED') AND actor_sha256 IS NOT NULL AND "
            "length(actor_sha256)=64 AND idempotency_key IS NOT NULL) OR "
            "(event_type <> 'STOP_REQUESTED' AND actor_sha256 IS NULL AND idempotency_key IS NULL)",
            name="ck_worker_supervisor_actor",
        ),
    )
    op.execute("""
    CREATE FUNCTION bt027_worker_events_append_only() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'worker supervisor history is append only'; END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER bt027_worker_events_no_update_delete
      BEFORE UPDATE OR DELETE ON paper_worker_supervisor_events
      FOR EACH ROW EXECUTE FUNCTION bt027_worker_events_append_only();
    CREATE TRIGGER bt027_worker_events_no_truncate
      BEFORE TRUNCATE ON paper_worker_supervisor_events
      FOR EACH STATEMENT EXECUTE FUNCTION bt027_worker_events_append_only();
    """)


def downgrade() -> None:
    raise RuntimeError("BT-027 worker supervisor history must not be dropped")
