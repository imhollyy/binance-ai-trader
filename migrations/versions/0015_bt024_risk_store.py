# ruff: noqa: E501
"""BT-024 isolated profile revisions, runtime control, reservations and audit."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_bt024_risk_store"
down_revision: str | None = "0014_bt023g_research_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_risk_profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("risk_level", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("user_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("risk_level", "profile_version", name="uq_risk_profile_revision"),
        sa.CheckConstraint(
            "risk_level BETWEEN 1 AND 5 AND profile_version > 0", name="ck_risk_profile_version"
        ),
        sa.CheckConstraint("user_confirmed", name="ck_risk_profile_confirmed"),
    )
    op.create_table(
        "ai_risk_runtime",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_profile_version", sa.Integer(), nullable=False),
        sa.Column("active_risk_level", sa.Integer(), nullable=False),
        sa.Column("bot_state", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "id=1 AND bot_state IN ('RUNNING','PAUSED','STOPPED','EMERGENCY_FLATTEN')",
            name="ck_risk_runtime_state",
        ),
    )
    op.create_table(
        "ai_risk_reservation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("reservation_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "event_type IN ('RESERVE','RELEASE') AND amount_usdt > 0",
            name="ck_risk_reservation_event",
        ),
    )
    op.create_table(
        "ai_risk_account_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("snapshot_id", sa.String(128), nullable=False, unique=True),
        sa.Column("equity_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("day_start_equity_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("peak_equity_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("open_capital_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("consecutive_losses", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "equity_usdt > 0 AND day_start_equity_usdt > 0 AND peak_equity_usdt > 0 AND open_capital_usdt >= 0 AND open_positions >= 0 AND consecutive_losses >= 0",
            name="ck_risk_account_snapshot_bounds",
        ),
    )
    op.create_table(
        "ai_risk_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("signal_id", sa.String(128), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("bot_revision", sa.Integer(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.execute("""
    CREATE FUNCTION bt024_db_clock() RETURNS trigger AS $$
    BEGIN NEW.created_at_ms := FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint; RETURN NEW; END;
    $$ LANGUAGE plpgsql;
    CREATE FUNCTION bt024_append_only() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'BT-024 risk history is append only'; END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER bt024_profile_clock BEFORE INSERT ON ai_risk_profiles FOR EACH ROW EXECUTE FUNCTION bt024_db_clock();
    CREATE TRIGGER bt024_reservation_clock BEFORE INSERT ON ai_risk_reservation_events FOR EACH ROW EXECUTE FUNCTION bt024_db_clock();
    CREATE TRIGGER bt024_snapshot_clock BEFORE INSERT ON ai_risk_account_snapshots FOR EACH ROW EXECUTE FUNCTION bt024_db_clock();
    CREATE TRIGGER bt024_audit_clock BEFORE INSERT ON ai_risk_audit FOR EACH ROW EXECUTE FUNCTION bt024_db_clock();
    CREATE TRIGGER bt024_profile_immutable BEFORE UPDATE OR DELETE ON ai_risk_profiles FOR EACH ROW EXECUTE FUNCTION bt024_append_only();
    CREATE TRIGGER bt024_reservation_immutable BEFORE UPDATE OR DELETE ON ai_risk_reservation_events FOR EACH ROW EXECUTE FUNCTION bt024_append_only();
    CREATE TRIGGER bt024_snapshot_immutable BEFORE UPDATE OR DELETE ON ai_risk_account_snapshots FOR EACH ROW EXECUTE FUNCTION bt024_append_only();
    CREATE TRIGGER bt024_audit_immutable BEFORE UPDATE OR DELETE ON ai_risk_audit FOR EACH ROW EXECUTE FUNCTION bt024_append_only();
    """)


def downgrade() -> None:
    raise RuntimeError("BT-024 audit and reservation history must not be dropped")
