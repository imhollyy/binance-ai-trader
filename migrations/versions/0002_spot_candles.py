"""Store only validated, closed public Spot OHLCV history.

Revision ID: 0002_spot_candles
Revises: 0001_paper_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_spot_candles"
down_revision: str | None = "0001_paper_settings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "spot_candles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("interval", sa.String(length=5), nullable=False),
        sa.Column("open_time_ms", sa.BigInteger(), nullable=False),
        sa.Column("close_time_ms", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("high", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("low", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("close", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("volume", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.UniqueConstraint(
            "symbol",
            "interval",
            "open_time_ms",
            name="uq_spot_candles_symbol_interval_open",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_spot_candles_nonnegative_volume"),
        sa.CheckConstraint("high >= low", name="ck_spot_candles_price_bounds"),
        sa.CheckConstraint("open > 0 AND close > 0", name="ck_spot_candles_positive_prices"),
    )


def downgrade() -> None:
    op.drop_table("spot_candles")
