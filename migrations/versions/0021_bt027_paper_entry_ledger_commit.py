"""Persist PAPER ledger commit receipts for entry coordination."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_bt027_entry_ledger"
down_revision: str | None = "0020_bt027_entry_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_entry_ledger_commits",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("risk_audit_event_id", sa.String(36), nullable=False),
        sa.Column("risk_reservation_event_id", sa.String(64), nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("ledger_event_id", sa.String(64), nullable=False),
        sa.Column("commit_sha256", sa.String(64), nullable=False),
        sa.Column("cash_after_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["paper_entry_intents.request_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["paper_spot_journal.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["paper_spot_positions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("journal_id", name="uq_entry_commit_journal"),
        sa.CheckConstraint(
            "intent_sha256 ~ '^[0-9a-f]{64}$' AND commit_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_entry_commit_hashes",
        ),
    )
    op.execute("""
        CREATE TRIGGER bt027_entry_ledger_commits_no_update_delete
          BEFORE UPDATE OR DELETE ON paper_entry_ledger_commits
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_entry_ledger_commits_no_truncate
          BEFORE TRUNCATE ON paper_entry_ledger_commits
          FOR EACH STATEMENT EXECUTE FUNCTION bt027_paper_entry_immutable();
    """)


def downgrade() -> None:
    raise RuntimeError("PAPER entry commit receipts are append-only audit history")
