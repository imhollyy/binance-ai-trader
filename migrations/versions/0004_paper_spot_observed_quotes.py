"""Audit locally received PUBLIC Spot quotes for PAPER-only synthetic fills.

Revision ID: 0004_paper_spot_observed_quotes
Revises: 0003_paper_spot_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_paper_spot_observed_quotes"
down_revision: str | None = "0003_paper_spot_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "paper_spot_observed_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "journal_id", sa.BigInteger(),
            sa.ForeignKey("paper_spot_journal.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("interval", sa.String(5), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("signal_close_time_ms", sa.BigInteger(), nullable=False),
        sa.Column("requested_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("reference_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_bps", sa.Integer(), nullable=False),
        sa.Column("slippage_bps", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("journal_id", name="uq_paper_spot_observed_quotes_journal"),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT', 'ETHUSDT')", name="ck_paper_observed_symbol"
        ),
        sa.CheckConstraint(
            "interval IN ('1m', '5m', '15m')", name="ck_paper_observed_interval"
        ),
        sa.CheckConstraint(
            "source = 'binance_public_spot_ticker_price'", name="ck_paper_observed_source"
        ),
        sa.CheckConstraint(
            "signal_close_time_ms >= 0 AND requested_at_ms > signal_close_time_ms "
            "AND received_at_ms >= requested_at_ms",
            name="ck_paper_observed_timestamps",
        ),
        sa.CheckConstraint(
            "reference_price > 0 AND reference_price <> 'NaN'::numeric",
            name="ck_paper_observed_price",
        ),
        sa.CheckConstraint(
            "fee_bps BETWEEN 0 AND 100 AND slippage_bps BETWEEN 0 AND 100",
            name="ck_paper_observed_costs",
        ),
    )
    # Reuse the function installed by 0003; never mutate the quote evidence.
    op.execute(
        "CREATE TRIGGER paper_observed_quotes_no_update_delete "
        "BEFORE UPDATE OR DELETE ON paper_spot_observed_quotes "
        "FOR EACH ROW EXECUTE FUNCTION reject_paper_spot_journal_mutation()"
    )
    op.execute(
        "CREATE TRIGGER paper_observed_quotes_no_truncate "
        "BEFORE TRUNCATE ON paper_spot_observed_quotes "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_paper_spot_journal_mutation()"
    )


def downgrade() -> None:
    if op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM paper_spot_observed_quotes)")
    ).scalar_one():
        raise RuntimeError("Refusing to erase observed PAPER quote audit history")
    op.drop_table("paper_spot_observed_quotes")
