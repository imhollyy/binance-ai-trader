"""Add a persistent, paper-only Spot account, positions, and immutable journal.

Revision ID: 0003_paper_spot_ledger
Revises: 0002_spot_candles
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_paper_spot_ledger"
down_revision: str | None = "0002_spot_candles"
branch_labels: str | None = None
depends_on: str | None = None

MONEY = sa.Numeric(precision=38, scale=18)


def upgrade() -> None:
    op.create_table(
        "paper_spot_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("market_type", sa.String(10), nullable=False, server_default="spot"),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("initial_capital_usdt", MONEY, nullable=False),
        sa.Column("cash_usdt", MONEY, nullable=False),
        sa.Column("realized_pnl_usdt", MONEY, nullable=False, server_default="0"),
        sa.Column("paid_fees_usdt", MONEY, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1", name="ck_paper_spot_accounts_singleton"),
        sa.CheckConstraint(
            "mode = 'paper' AND market_type = 'spot'", name="ck_paper_spot_accounts_mode"
        ),
        sa.CheckConstraint("settings_version >= 1", name="ck_paper_spot_accounts_version"),
        sa.CheckConstraint(
            "initial_capital_usdt > 0 AND initial_capital_usdt <= 1000000 "
            "AND initial_capital_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_initial_capital",
        ),
        sa.CheckConstraint(
            "cash_usdt >= 0 AND cash_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_cash",
        ),
        sa.CheckConstraint(
            "realized_pnl_usdt <> 'NaN'::numeric", name="ck_paper_spot_accounts_pnl"
        ),
        sa.CheckConstraint(
            "paid_fees_usdt >= 0 AND paid_fees_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_fees",
        ),
    )
    op.create_table(
        "paper_spot_positions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trade_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("entry_fill_price", MONEY, nullable=False),
        sa.Column("entry_notional_usdt", MONEY, nullable=False),
        sa.Column("entry_fee_usdt", MONEY, nullable=False),
        sa.Column("stop_price", MONEY, nullable=False),
        sa.Column("estimated_stop_loss_usdt", MONEY, nullable=False),
        sa.Column("exit_fill_price", MONEY),
        sa.Column("exit_fee_usdt", MONEY),
        sa.Column("net_proceeds_usdt", MONEY),
        sa.Column("realized_pnl_usdt", MONEY),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("account_id", "trade_id", name="uq_paper_spot_positions_trade"),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_spot_positions_idempotency"
        ),
        sa.CheckConstraint(
            "trade_id ~ '^[A-Za-z0-9_-]{1,64}$'", name="ck_paper_spot_positions_trade_id"
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128", name="ck_paper_spot_positions_idempotency"
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT', 'ETHUSDT')", name="ck_paper_spot_positions_symbol"
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_paper_spot_positions_status"),
        sa.CheckConstraint(
            "quantity > 0 AND entry_fill_price > 0 AND entry_notional_usdt > 0 "
            "AND entry_fee_usdt >= 0 AND stop_price > 0 AND stop_price < entry_fill_price "
            "AND estimated_stop_loss_usdt >= 0 "
            "AND quantity <> 'NaN'::numeric AND entry_fill_price <> 'NaN'::numeric "
            "AND entry_notional_usdt <> 'NaN'::numeric AND entry_fee_usdt <> 'NaN'::numeric "
            "AND stop_price <> 'NaN'::numeric AND estimated_stop_loss_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_positions_entry",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND exit_fill_price IS NULL AND exit_fee_usdt IS NULL "
            "AND net_proceeds_usdt IS NULL AND realized_pnl_usdt IS NULL AND closed_at IS NULL) "
            "OR (status = 'closed' AND exit_fill_price IS NOT NULL AND exit_fill_price > 0 "
            "AND exit_fee_usdt IS NOT NULL AND exit_fee_usdt >= 0 "
            "AND net_proceeds_usdt IS NOT NULL AND net_proceeds_usdt >= 0 "
            "AND realized_pnl_usdt IS NOT NULL AND closed_at IS NOT NULL "
            "AND exit_fill_price <> 'NaN'::numeric AND exit_fee_usdt <> 'NaN'::numeric "
            "AND net_proceeds_usdt <> 'NaN'::numeric AND realized_pnl_usdt <> 'NaN'::numeric)",
            name="ck_paper_spot_positions_lifecycle",
        ),
    )
    op.create_index(
        "ix_paper_spot_positions_account_status",
        "paper_spot_positions",
        ["account_id", "status", "id"],
    )
    op.create_table(
        "paper_spot_journal",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            sa.BigInteger(),
            sa.ForeignKey("paper_spot_positions.id", ondelete="RESTRICT"),
        ),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("cash_delta_usdt", MONEY, nullable=False),
        sa.Column("balance_after_usdt", MONEY, nullable=False),
        sa.Column("fee_usdt", MONEY, nullable=False, server_default="0"),
        sa.Column("realized_pnl_delta_usdt", MONEY, nullable=False, server_default="0"),
        sa.Column("quantity_delta", MONEY, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("account_id", "event_id", name="uq_paper_spot_journal_event"),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_spot_journal_idempotency"
        ),
        sa.CheckConstraint(
            "length(event_id) BETWEEN 1 AND 64", name="ck_paper_spot_journal_event_id"
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128", name="ck_paper_spot_journal_idempotency"
        ),
        sa.CheckConstraint(
            "(event_type = 'account_opened' AND position_id IS NULL AND cash_delta_usdt > 0) "
            "OR (event_type = 'spot_buy' AND position_id IS NOT NULL AND cash_delta_usdt < 0) "
            "OR (event_type = 'spot_sell' AND position_id IS NOT NULL AND cash_delta_usdt > 0)",
            name="ck_paper_spot_journal_event_type",
        ),
        sa.CheckConstraint(
            "balance_after_usdt >= 0 AND fee_usdt >= 0 "
            "AND cash_delta_usdt <> 'NaN'::numeric AND balance_after_usdt <> 'NaN'::numeric "
            "AND fee_usdt <> 'NaN'::numeric AND realized_pnl_delta_usdt <> 'NaN'::numeric "
            "AND quantity_delta <> 'NaN'::numeric",
            name="ck_paper_spot_journal_amounts",
        ),
    )
    op.create_index(
        "ix_paper_spot_journal_account_order", "paper_spot_journal", ["account_id", "id"]
    )
    op.create_index("ix_paper_spot_journal_position", "paper_spot_journal", ["position_id"])
    op.execute(
        """
        CREATE FUNCTION reject_paper_spot_journal_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'paper_spot_journal is append only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER paper_spot_journal_no_update_delete "
        "BEFORE UPDATE OR DELETE ON paper_spot_journal "
        "FOR EACH ROW EXECUTE FUNCTION reject_paper_spot_journal_mutation()"
    )
    op.execute(
        "CREATE TRIGGER paper_spot_journal_no_truncate "
        "BEFORE TRUNCATE ON paper_spot_journal "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_paper_spot_journal_mutation()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("paper_spot_journal", "paper_spot_positions", "paper_spot_accounts"):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError("Refusing to remove a nonempty paper Spot ledger")
    op.drop_table("paper_spot_journal")
    op.drop_table("paper_spot_positions")
    op.drop_table("paper_spot_accounts")
    op.execute("DROP FUNCTION reject_paper_spot_journal_mutation()")
