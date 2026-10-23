"""Persist observation-worker generation and process attestation metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023_bt027_observer_fencing"
down_revision: str | None = "0022_bt027_entry_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_worker_runtime",
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("paper_worker_runtime", sa.Column("manager_id", sa.String(36)))
    op.add_column("paper_worker_runtime", sa.Column("manager_pid", sa.Integer()))
    op.add_column("paper_worker_runtime", sa.Column("manager_start_token", sa.String(64)))
    op.add_column("paper_worker_runtime", sa.Column("process_pid", sa.Integer()))
    op.add_column("paper_worker_runtime", sa.Column("process_start_token", sa.String(64)))
    op.add_column(
        "paper_worker_runtime",
        sa.Column("execution_mode", sa.String(24), nullable=False, server_default="INERT"),
    )
    op.drop_constraint("ck_worker_runtime_state", "paper_worker_runtime", type_="check")
    op.create_check_constraint(
        "ck_worker_runtime_state",
        "paper_worker_runtime",
        "state IN ('STOPPED','STARTING','RUNNING','STOP_REQUESTED','FAILED')",
    )
    op.create_check_constraint(
        "ck_worker_runtime_generation",
        "paper_worker_runtime",
        "generation >= 0 AND execution_mode IN ('INERT','OBSERVATION_ONLY')",
    )
    op.create_check_constraint(
        "ck_worker_runtime_process_identity",
        "paper_worker_runtime",
        "(manager_id IS NULL AND manager_pid IS NULL AND manager_start_token IS NULL "
        "AND process_pid IS NULL AND process_start_token IS NULL) OR "
        "(generation >= 1 AND manager_id IS NOT NULL AND manager_pid > 0 "
        "AND manager_start_token IS NOT NULL AND execution_mode='OBSERVATION_ONLY' "
        "AND ((process_pid IS NULL AND process_start_token IS NULL) OR "
        "(process_pid > 0 AND process_start_token IS NOT NULL)))",
    )
    op.add_column("paper_worker_supervisor_events", sa.Column("worker_generation", sa.BigInteger()))
    op.drop_constraint(
        "ck_worker_supervisor_event_type", "paper_worker_supervisor_events", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_supervisor_event_type",
        "paper_worker_supervisor_events",
        "event_type IN ('WORKER_CLAIMED','HEARTBEAT','STOP_REQUESTED','WORKER_STOPPED',"
        "'OBSERVER_START_REQUESTED','OBSERVER_STARTED','OBSERVER_HEARTBEAT',"
        "'OBSERVER_STOP_ACKNOWLEDGED','OBSERVER_FAILED','OBSERVER_MANAGER_LOST')",
    )
    op.create_check_constraint(
        "ck_worker_supervisor_generation",
        "paper_worker_supervisor_events",
        "worker_generation IS NULL OR worker_generation >= 1",
    )
    op.drop_constraint(
        "ck_worker_supervisor_actor", "paper_worker_supervisor_events", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_supervisor_actor",
        "paper_worker_supervisor_events",
        "(event_type IN ('STOP_REQUESTED','OBSERVER_START_REQUESTED') AND "
        "actor_sha256 IS NOT NULL AND length(actor_sha256)=64 AND idempotency_key IS NOT NULL) OR "
        "(event_type NOT IN ('STOP_REQUESTED','OBSERVER_START_REQUESTED') AND "
        "actor_sha256 IS NULL AND idempotency_key IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    events = bind.execute(
        sa.text(
            "SELECT count(*) FROM paper_worker_supervisor_events "
            "WHERE worker_generation IS NOT NULL OR event_type LIKE 'OBSERVER_%'"
        )
    ).scalar_one()
    runtime = bind.execute(
        sa.text("SELECT count(*) FROM paper_worker_runtime WHERE generation > 0")
    ).scalar_one()
    if events or runtime:
        raise RuntimeError("Refusing to discard persistent observer fencing history")
    op.drop_constraint(
        "ck_worker_supervisor_actor", "paper_worker_supervisor_events", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_supervisor_actor",
        "paper_worker_supervisor_events",
        "(event_type IN ('STOP_REQUESTED') AND actor_sha256 IS NOT NULL AND "
        "length(actor_sha256)=64 AND idempotency_key IS NOT NULL) OR "
        "(event_type <> 'STOP_REQUESTED' AND actor_sha256 IS NULL AND idempotency_key IS NULL)",
    )
    op.drop_constraint(
        "ck_worker_supervisor_generation", "paper_worker_supervisor_events", type_="check"
    )
    op.drop_constraint(
        "ck_worker_supervisor_event_type", "paper_worker_supervisor_events", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_supervisor_event_type",
        "paper_worker_supervisor_events",
        "event_type IN ('WORKER_CLAIMED','HEARTBEAT','STOP_REQUESTED','WORKER_STOPPED')",
    )
    op.drop_column("paper_worker_supervisor_events", "worker_generation")
    op.drop_constraint("ck_worker_runtime_process_identity", "paper_worker_runtime", type_="check")
    op.drop_constraint("ck_worker_runtime_generation", "paper_worker_runtime", type_="check")
    op.drop_constraint("ck_worker_runtime_state", "paper_worker_runtime", type_="check")
    op.create_check_constraint(
        "ck_worker_runtime_state",
        "paper_worker_runtime",
        "state IN ('STOPPED','RUNNING','STOP_REQUESTED')",
    )
    for column in (
        "execution_mode",
        "process_start_token",
        "process_pid",
        "manager_start_token",
        "manager_pid",
        "manager_id",
        "generation",
    ):
        op.drop_column("paper_worker_runtime", column)
