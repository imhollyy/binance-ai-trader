"""Database tables for paper settings and audit history."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TradingSettingsRow(Base):
    __tablename__ = "trading_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AuditRow(Base):
    __tablename__ = "settings_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    settings_id: Mapped[int] = mapped_column(ForeignKey("trading_settings.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class SpotCandleRow(Base):
    """Closed public Spot candles, deduplicated by their opening millisecond."""

    __tablename__ = "spot_candles"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "interval", "open_time_ms", name="uq_spot_candles_symbol_interval_open"
        ),
        CheckConstraint("volume >= 0", name="ck_spot_candles_nonnegative_volume"),
        CheckConstraint("high >= low", name="ck_spot_candles_price_bounds"),
        CheckConstraint("open > 0 AND close > 0", name="ck_spot_candles_positive_prices"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    open_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    close_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)


class PaperSpotAccountRow(Base):
    """The single local PAPER Spot account; never an exchange balance."""

    __tablename__ = "paper_spot_accounts"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_paper_spot_accounts_singleton"),
        CheckConstraint(
            "mode = 'paper' AND market_type = 'spot'", name="ck_paper_spot_accounts_mode"
        ),
        CheckConstraint("settings_version >= 1", name="ck_paper_spot_accounts_version"),
        CheckConstraint(
            "initial_capital_usdt > 0 AND initial_capital_usdt <= 1000000 "
            "AND initial_capital_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_initial_capital",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "cash_usdt >= 0 AND cash_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_cash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "realized_pnl_usdt <> 'NaN'::numeric", name="ck_paper_spot_accounts_pnl"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "paid_fees_usdt >= 0 AND paid_fees_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_fees",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="paper")
    market_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="spot")
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_capital_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cash_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    paid_fees_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperSpotPositionRow(Base):
    """Reserved schema for a future transaction-safe PAPER Spot lifecycle."""

    __tablename__ = "paper_spot_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_id", name="uq_paper_spot_positions_trade"),
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_spot_positions_idempotency"
        ),
        CheckConstraint(
            "trade_id ~ '^[A-Za-z0-9_-]{1,64}$'", name="ck_paper_spot_positions_trade_id"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_spot_positions_idempotency",
        ),
        CheckConstraint("symbol IN ('BTCUSDT', 'ETHUSDT')", name="ck_paper_spot_positions_symbol"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_paper_spot_positions_status"),
        CheckConstraint(
            "quantity > 0 AND entry_fill_price > 0 AND entry_notional_usdt > 0 "
            "AND entry_fee_usdt >= 0 AND stop_price > 0 AND stop_price < entry_fill_price "
            "AND estimated_stop_loss_usdt >= 0 "
            "AND quantity <> 'NaN'::numeric AND entry_fill_price <> 'NaN'::numeric "
            "AND entry_notional_usdt <> 'NaN'::numeric AND entry_fee_usdt <> 'NaN'::numeric "
            "AND stop_price <> 'NaN'::numeric AND estimated_stop_loss_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_positions_entry",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(status = 'open' AND exit_fill_price IS NULL AND exit_fee_usdt IS NULL "
            "AND net_proceeds_usdt IS NULL AND realized_pnl_usdt IS NULL AND closed_at IS NULL) "
            "OR (status = 'closed' AND exit_fill_price IS NOT NULL AND exit_fill_price > 0 "
            "AND exit_fee_usdt IS NOT NULL AND exit_fee_usdt >= 0 "
            "AND net_proceeds_usdt IS NOT NULL AND net_proceeds_usdt >= 0 "
            "AND realized_pnl_usdt IS NOT NULL AND closed_at IS NOT NULL "
            "AND exit_fill_price <> 'NaN'::numeric AND exit_fee_usdt <> 'NaN'::numeric "
            "AND net_proceeds_usdt <> 'NaN'::numeric AND realized_pnl_usdt <> 'NaN'::numeric)",
            name="ck_paper_spot_positions_lifecycle",
        ).ddl_if(dialect="postgresql"),
        Index("ix_paper_spot_positions_account_status", "account_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_fill_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_notional_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_fee_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    estimated_stop_loss_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exit_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    exit_fee_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    net_proceeds_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    realized_pnl_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperSpotJournalRow(Base):
    """Append-only accounting facts; UPDATE, DELETE and TRUNCATE fail in PostgreSQL."""

    __tablename__ = "paper_spot_journal"
    __table_args__ = (
        UniqueConstraint("account_id", "event_id", name="uq_paper_spot_journal_event"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_paper_spot_journal_idempotency"),
        CheckConstraint("length(event_id) BETWEEN 1 AND 64", name="ck_paper_spot_journal_event_id"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_spot_journal_idempotency",
        ),
        CheckConstraint(
            "(event_type = 'account_opened' AND position_id IS NULL AND cash_delta_usdt > 0) "
            "OR (event_type = 'spot_buy' AND position_id IS NOT NULL AND cash_delta_usdt < 0) "
            "OR (event_type = 'spot_sell' AND position_id IS NOT NULL AND cash_delta_usdt > 0)",
            name="ck_paper_spot_journal_event_type",
        ),
        CheckConstraint(
            "balance_after_usdt >= 0 AND fee_usdt >= 0 "
            "AND cash_delta_usdt <> 'NaN'::numeric AND balance_after_usdt <> 'NaN'::numeric "
            "AND fee_usdt <> 'NaN'::numeric AND realized_pnl_delta_usdt <> 'NaN'::numeric "
            "AND quantity_delta <> 'NaN'::numeric",
            name="ck_paper_spot_journal_amounts",
        ).ddl_if(dialect="postgresql"),
        Index("ix_paper_spot_journal_account_order", "account_id", "id"),
        Index("ix_paper_spot_journal_position", "position_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_spot_positions.id", ondelete="RESTRICT")
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    cash_delta_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    balance_after_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    realized_pnl_delta_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    quantity_delta: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
