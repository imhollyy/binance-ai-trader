"""Block TRUNCATE on append-only BT-023G and BT-024 ledgers."""

from __future__ import annotations

from alembic import op

revision: str = "0016_bt023g_risk_trunc"
down_revision: str | None = "0015_bt024_risk_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, trigger, function in (
        (
            "ai_research_prediction_log",
            "bt023_prediction_no_truncate",
            "bt023_prediction_append_only",
        ),
        (
            "ai_research_prediction_outcomes",
            "bt023_outcome_no_truncate",
            "bt023_prediction_append_only",
        ),
        ("ai_risk_profiles", "bt024_profile_no_truncate", "bt024_append_only"),
        ("ai_risk_reservation_events", "bt024_reservation_no_truncate", "bt024_append_only"),
        ("ai_risk_account_snapshots", "bt024_snapshot_no_truncate", "bt024_append_only"),
        ("ai_risk_audit", "bt024_audit_no_truncate", "bt024_append_only"),
    ):
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
        )


def downgrade() -> None:
    for table, trigger in (
        ("ai_research_prediction_log", "bt023_prediction_no_truncate"),
        ("ai_research_prediction_outcomes", "bt023_outcome_no_truncate"),
        ("ai_risk_profiles", "bt024_profile_no_truncate"),
        ("ai_risk_reservation_events", "bt024_reservation_no_truncate"),
        ("ai_risk_account_snapshots", "bt024_snapshot_no_truncate"),
        ("ai_risk_audit", "bt024_audit_no_truncate"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
