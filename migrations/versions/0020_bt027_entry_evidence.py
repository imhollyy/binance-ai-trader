"""Add immutable pre-entry PAPER account and market-cost evidence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020_bt027_entry_evidence"
down_revision: str | None = "0019_bt027_paper_entry_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_entry_account_evidence",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("journal_head_id", sa.BigInteger(), nullable=False),
        sa.Column("journal_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["paper_entry_intents.request_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["journal_head_id"], ["paper_spot_journal.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "account_id = 1 AND settings_version >= 1", name="ck_entry_account_evidence_scope"
        ),
        sa.CheckConstraint(
            "journal_head_id >= 1 AND journal_count >= 1", name="ck_entry_account_evidence_journal"
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="ck_entry_account_evidence_hash"
        ),
    )
    op.create_table(
        "paper_entry_market_evidence",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("interval", sa.String(5), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("reference_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("bid_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("ask_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("spread_bps", sa.Numeric(20, 10), nullable=False),
        sa.Column("fee_assumption_bps", sa.Numeric(20, 10), nullable=False),
        sa.Column("fee_assumption_version", sa.String(64), nullable=False),
        sa.Column("slippage_assumption_bps", sa.Numeric(20, 10), nullable=False),
        sa.Column("slippage_assumption_version", sa.String(64), nullable=False),
        sa.Column("expected_net_advantage_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("cost_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["paper_entry_intents.request_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT') AND interval = '1m'", name="ck_entry_market_scope"
        ),
        sa.CheckConstraint(
            "source = 'binance_public_spot_coherence'", name="ck_entry_market_source"
        ),
        sa.CheckConstraint(
            "observed_at_ms > 0 AND reference_price > 0 AND bid_price > 0 "
            "AND ask_price > bid_price",
            name="ck_entry_market_prices",
        ),
        sa.CheckConstraint(
            "spread_bps > 0 AND fee_assumption_bps > 0 AND slippage_assumption_bps > 0",
            name="ck_entry_market_costs",
        ),
        sa.CheckConstraint(
            "length(fee_assumption_version) BETWEEN 1 AND 64 AND "
            "length(slippage_assumption_version) BETWEEN 1 AND 64",
            name="ck_entry_market_assumption_versions",
        ),
        sa.CheckConstraint(
            "cost_evidence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_entry_market_evidence_hash"
        ),
    )
    op.execute(
        """
        CREATE TRIGGER bt027_entry_account_evidence_no_update_delete
          BEFORE UPDATE OR DELETE ON paper_entry_account_evidence
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_entry_account_evidence_no_truncate
          BEFORE TRUNCATE ON paper_entry_account_evidence
          FOR EACH STATEMENT EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_entry_market_evidence_no_update_delete
          BEFORE UPDATE OR DELETE ON paper_entry_market_evidence
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_entry_market_evidence_no_truncate
          BEFORE TRUNCATE ON paper_entry_market_evidence
          FOR EACH STATEMENT EXECUTE FUNCTION bt027_paper_entry_immutable();
        """
    )


def downgrade() -> None:
    raise RuntimeError("BT-027 pre-entry evidence is append-only and must not be dropped")
